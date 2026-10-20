# -*- coding: utf-8 -*-
"""报告落盘测试：三件套齐全、内容含关键结论、可回读。"""
from __future__ import annotations

import json

from llm_stress.decisions import decide
from llm_stress.report import render_markdown, write_outputs


def test_write_outputs_creates_three_files(tmp_path, make_stats):
    stats = [make_stats(concurrency=1, output_tps=100.0),
             make_stats(concurrency=2, output_tps=180.0)]
    decision = decide(stats)
    files = write_outputs(
        tmp_path, run_id="r1",
        meta={"scene": "ladder", "base_url": "http://x", "api_key_masked": "sk-ab****cd",
              "protocol": "openai", "models": ["m"], "dataset": "mixed",
              "dataset_size": 3, "timeout_s": 120, "started_at": 0.0, "command": "llm-stress run"},
        stats=stats, decision=decision, samples=[])

    assert set(files) == {"markdown", "json", "samples"}
    from pathlib import Path
    for p in files.values():
        assert Path(p).exists()
    assert (tmp_path / "r1.md").read_text(encoding="utf-8").startswith("# LLM 压力测试报告")


def test_markdown_contains_key_sections(make_stats):
    stats = [make_stats(concurrency=2, ttft_p95=900.0, total_p95=8000.0)]
    decision = decide(stats)
    md = render_markdown(run_id="r2", stats=stats, decision=decision,
                         meta={"scene": "baseline", "base_url": "http://x", "models": ["m"],
                               "dataset": "mixed", "dataset_size": 1, "protocol": "openai",
                               "api_key_masked": "sk-****", "timeout_s": 120,
                               "started_at": 0.0, "command": "llm-stress run"})
    for token in ("## 一句话结论", "## 各并发档位指标", "## 容量拐点",
                  "## 指标口径", "## 复现命令", "TTFT p50"):
        assert token in md
    assert decision.verdict in md


def test_json_roundtrip_has_decision(tmp_path, make_stats):
    stats = [make_stats(concurrency=1)]
    decision = decide(stats)
    files = write_outputs(tmp_path, run_id="r3",
                          meta={"scene": "baseline", "base_url": "u", "models": [],
                                "dataset": "mixed", "dataset_size": 0, "protocol": "openai",
                                "api_key_masked": "", "timeout_s": 120,
                                "started_at": 0.0, "command": "c"},
                          stats=stats, decision=decision)
    payload = json.loads((tmp_path / "r3.json").read_text(encoding="utf-8"))
    assert payload["run_id"] == "r3"
    assert payload["decision"]["verdict"] == decision.verdict
    assert payload["stages"][0]["concurrency"] == 1
    assert files["json"].endswith("r3.json")

def test_saturation_section_explains_missing_conclusion(make_stats):
    # 可用档位不足 2 档：不应含糊说"或可用档位不足"，要明确无法判断
    stats = [make_stats(concurrency=1, success_rate=0.5, output_tps=100.0),
             make_stats(concurrency=2, success_rate=0.5, output_tps=180.0)]
    md = render_markdown(run_id="r4", stats=stats, decision=decide(stats),
                         meta={"scene": "ladder", "base_url": "u", "models": ["m"],
                               "dataset": "mixed", "dataset_size": 1, "protocol": "openai",
                               "api_key_masked": "", "timeout_s": 120,
                               "started_at": 0.0, "command": "c"})
    assert "不足以比较吞吐增幅" in md


def test_saturation_section_reports_no_knee_when_still_scaling(make_stats):
    stats = [make_stats(concurrency=1, output_tps=100.0),
             make_stats(concurrency=2, output_tps=200.0)]
    md = render_markdown(run_id="r5", stats=stats, decision=decide(stats),
                         meta={"scene": "ladder", "base_url": "u", "models": ["m"],
                               "dataset": "mixed", "dataset_size": 1, "protocol": "openai",
                               "api_key_masked": "", "timeout_s": 120,
                               "started_at": 0.0, "command": "c"})
    assert "尚未见到拐点" in md

