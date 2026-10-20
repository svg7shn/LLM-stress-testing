# -*- coding: utf-8 -*-
"""多模型横评测试：串行隔离、排名口径、报告落盘、单模型失败不中断。"""
from __future__ import annotations

import json
from pathlib import Path

from llm_stress.compare import run_compare
from llm_stress.datasets import resolve
from llm_stress.scenarios import baseline


def _factory(_model: str):
    return baseline(requests=3)


async def test_compare_two_models(mock_base, tmp_path):
    result = await run_compare(
        base_url=mock_base,
        api_key="test-key",
        protocol="openai",
        models=["mock-llm-fast", "mock-llm-slow"],
        dataset=resolve("short"),
        scenario_factory=_factory,
        out_dir=tmp_path,
        run_id="cmp-two",
        timeout_s=30.0,
    )

    assert result.cancelled is False
    assert [e.model for e in result.entries] == ["mock-llm-fast", "mock-llm-slow"]
    assert all(e.ok for e in result.entries)
    # 逐模型串行：每个模型各自一条独立结果，而不是混在一个 RunResult 里
    for entry in result.entries:
        assert entry.result is not None
        assert len(entry.result.stats) == 1
        assert entry.result.stats[0].concurrency == 1
        assert entry.result.meta["models"] == [entry.model]
        # 横评不写单项三件套
        assert entry.result.files == {}

    ranked = result.ranked()
    assert len(ranked) == 2
    assert result.champion() is ranked[0]
    payload = result.champion().to_dict()
    assert payload["model"]
    assert payload["verdict"]
    assert payload["recommended_concurrency"] is not None
    assert payload["ttft_p95"] is not None

    md_path = Path(result.files["markdown"])
    json_path = Path(result.files["json"])
    assert md_path.exists() and json_path.exists()
    text = md_path.read_text(encoding="utf-8")
    for keyword in ("横评", "排名", "TTFT", "复现"):
        assert keyword in text
    for model in ("mock-llm-fast", "mock-llm-slow"):
        assert model in text

    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["run_id"] == "cmp-two"
    assert saved["ranking"] == [e.model for e in ranked]
    assert len(saved["entries"]) == 2


async def test_compare_survives_one_model_failure(mock_base, tmp_path, monkeypatch):
    from llm_stress import compare as compare_mod

    real = compare_mod.run_scenario

    async def fake(**kwargs):
        if kwargs.get("models") == ["boom-model"]:
            raise RuntimeError("模拟上游炸了")
        return await real(**kwargs)

    monkeypatch.setattr(compare_mod, "run_scenario", fake)

    result = await run_compare(
        base_url=mock_base,
        api_key="test-key",
        protocol="openai",
        models=["mock-llm-fast", "boom-model"],
        dataset=resolve("short"),
        scenario_factory=_factory,
        out_dir=tmp_path,
        run_id="cmp-partial",
        timeout_s=30.0,
    )

    assert len(result.entries) == 2
    failed = [e for e in result.entries if not e.ok]
    assert len(failed) == 1
    assert failed[0].model == "boom-model"
    assert "模拟上游炸了" in failed[0].error
    assert result.champion() is not None
    text = Path(result.files["markdown"]).read_text(encoding="utf-8")
    assert "boom-model" in text


def test_rank_key_orders_by_verdict_then_capacity(make_stats):
    """排名口径：结论档位 > 推荐并发 > 峰值吞吐 > TTFT p95，失败模型排最后。"""
    from types import SimpleNamespace

    from llm_stress.compare import CompareEntry, _rank_key
    from llm_stress.decisions import VERDICT_FAIL, VERDICT_LIMITED, VERDICT_OK

    def entry(name, verdict, conc, tps, ttft_p95):
        stage = make_stats(concurrency=conc, ttft_p95=ttft_p95)
        result = SimpleNamespace(
            stats=[stage],
            decision=SimpleNamespace(
                verdict=verdict, recommended_concurrency=conc, peak_output_tps=tps
            ),
        )
        return CompareEntry(model=name, result=result)

    # 同档位、同推荐并发、同吞吐：靠 TTFT p95 分出先后（第四级口径）
    same_conc_slow = entry("a-ok-slow", VERDICT_OK, 8, 100.0, 900.0)
    same_conc_fast = entry("b-ok-fast", VERDICT_OK, 8, 100.0, 300.0)
    limited = entry("c-limited", VERDICT_LIMITED, 32, 500.0, 200.0)
    failed = entry("d-fail", VERDICT_FAIL, 64, 900.0, 100.0)
    errored = CompareEntry(model="e-error", error="boom")

    ordered = [
        e.model
        for e in sorted([failed, limited, same_conc_slow, same_conc_fast, errored], key=_rank_key)
    ]
    assert ordered == ["b-ok-fast", "a-ok-slow", "c-limited", "d-fail", "e-error"]
