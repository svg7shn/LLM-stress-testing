# -*- coding: utf-8 -*-
"""决策判定测试：SLO、推荐并发、饱和、成本、错误归因。"""
from __future__ import annotations

from llm_stress.decisions import (
    SLO_DEFAULTS,
    VERDICT_FAIL,
    VERDICT_LIMITED,
    VERDICT_OK,
    decide,
)


def test_ok_when_all_stages_meet_slo(make_stats):
    # 并发翻倍、吞吐也翻倍 → 还没饱和，全部 SLO 达标
    stats = [make_stats(concurrency=1, ttft_p95=400.0, total_p95=4000.0, output_tps=100.0),
             make_stats(concurrency=2, ttft_p95=600.0, total_p95=6000.0, output_tps=200.0)]
    d = decide(stats)
    assert d.verdict == VERDICT_OK
    assert d.recommended_concurrency == 2
    assert d.violations == []


def test_high_ttft_stage_excluded_from_recommendation(make_stats):
    stats = [make_stats(concurrency=1), make_stats(concurrency=2),
             make_stats(concurrency=4, ttft_p95=3000.0)]
    d = decide(stats)
    assert d.recommended_concurrency == 2
    assert any("并发 4" in v for v in d.violations)
    assert any("TTFT p95" in v for v in d.violations)


def test_fail_when_no_stage_meets_slo(make_stats):
    d = decide([make_stats(concurrency=1, ttft_p95=5000.0)])
    assert d.verdict == VERDICT_FAIL
    assert d.recommended_concurrency is None
    assert any("没有" in f for f in d.findings)


def test_low_success_rate_violates_slo(make_stats):
    d = decide([make_stats(concurrency=2, success_rate=0.8, ok=8, requests=10)])
    assert d.recommended_concurrency is None
    assert any("成功率" in v for v in d.violations)


def test_saturation_marks_limited_and_caps_concurrency(make_stats):
    stats = [
        make_stats(concurrency=1, output_tps=100.0),
        make_stats(concurrency=2, output_tps=200.0),
        make_stats(concurrency=4, output_tps=205.0),
    ]
    d = decide(stats)
    assert d.verdict == VERDICT_LIMITED
    assert d.saturation["saturation"]["concurrency"] == 4
    assert any("把并发上限设在" in a for a in d.advice)


def test_error_mix_advice_for_rate_limit(make_stats):
    d = decide([make_stats(concurrency=2, errors={"rate_limited": 5}, rate_limited=5,
                           success_rate=0.5, ok=5)])
    assert d.error_mix == {"rate_limited": 5}
    assert any("被限流（429）" in f for f in d.findings)
    assert any("429" in a for a in d.advice)


def test_cost_computed_from_tokens(make_stats):
    d = decide([make_stats(total_tokens=2000, cost_usd=0.002)],
               price_in_per_m=1.0, price_out_per_m=2.0)
    assert d.cost["total_tokens"] == 2000
    assert d.cost["total_cost_usd"] == 0.002
    assert d.cost["cost_per_mtok_usd"] == 1.0


def test_no_price_triggers_note(make_stats):
    d = decide([make_stats()])
    assert any("未提供单价" in a for a in d.advice)


def test_drift_over_threshold_degrades_verdict(make_stats):
    d = decide([make_stats(concurrency=4)],
               scene_kind="soak",
               drift_info={"enough_data": True, "drift_ratio": 2.0,
                           "front_p95": 1000.0, "back_p95": 2000.0},)
    assert d.verdict in {VERDICT_LIMITED, VERDICT_OK}
    assert any("长稳漂移" in f for f in d.findings)
    assert any("漂移" in a for a in d.advice)


def test_soak_without_enough_samples_says_so(make_stats):
    d = decide([make_stats(concurrency=4)], scene_kind="soak",
               drift_info={"enough_data": False, "samples": 5})
    assert any("样本不足 20 条" in f for f in d.findings)


def test_slo_defaults_are_module_constants():
    assert SLO_DEFAULTS["ttft_p95_ms"] == 2000.0
    assert SLO_DEFAULTS["total_p95_ms"] == 30000.0
    assert SLO_DEFAULTS["success_rate"] == 0.99
    assert SLO_DEFAULTS["drift_ratio"] == 1.5
