# -*- coding: utf-8 -*-
"""指标口径测试：分位数、聚合、TPOT、失败样本不污染延迟分布。"""
from __future__ import annotations

from llm_stress.metrics import aggregate, percentile


def test_percentile_empty_and_single():
    assert percentile([], 50) is None
    assert percentile([7.0], 95) == 7.0


def test_percentile_linear_interpolation():
    # pos = (n-1)*q/100，与 numpy.percentile 默认口径一致
    assert percentile([0.0, 10.0], 50) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 0) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0


def test_sample_derived_metrics(make_sample):
    s = make_sample(ttft_ms=200.0, total_ms=1000.0, out_tokens=9)
    assert s.tpot_ms == 100.0          # (1000-200)/(9-1)
    assert s.output_tps == 9.0         # 9 token / 1.0 s
    assert s.window_s == 1.0


def test_tpot_none_when_not_ok_or_no_tokens(make_sample):
    assert make_sample(ok=False, total_ms=1000.0).tpot_ms is None
    assert make_sample(out_tokens=0).tpot_ms is None


def test_aggregate_success_rate_and_error_mix(make_sample):
    samples = [
        make_sample(started_at=0.0, total_ms=1000.0, ttft_ms=100.0, out_tokens=10),
        make_sample(started_at=0.5, total_ms=1000.0, ttft_ms=300.0, out_tokens=10),
        make_sample(started_at=1.0, total_ms=1000.0, ttft_ms=500.0, out_tokens=10),
        make_sample(started_at=1.5, ok=False, status=429, error_class="rate_limited",
                    total_ms=200.0, out_tokens=0),
    ]
    st = aggregate(samples, stage_index=0, concurrency=4)
    assert st.requests == 4
    assert st.ok == 3
    assert st.failed == 1
    assert st.success_rate == 0.75
    assert st.rate_limited == 1
    assert st.errors == {"rate_limited": 1}
    assert st.statuses == {"200": 3, "429": 1}
    # 窗口 = 最早开始(0.0) ~ 最晚结束(1.0+1.0=2.0s)；末条 429 在 1.5~1.7s 内结束
    assert st.window_s == 2.0
    assert st.rps == 1.5              # 3 ok / 2.0s
    assert st.offered_rps == 2.0      # 4 / 2.0s
    assert st.output_tps == 15.0      # 30 token / 2.0s


def test_failed_samples_do_not_enter_latency_distribution(make_sample):
    samples = [
        make_sample(started_at=0.0, ttft_ms=100.0, total_ms=1000.0),
        make_sample(started_at=0.1, ttft_ms=120.0, total_ms=1100.0),
        make_sample(started_at=0.2, ok=False, status=500, error_class="http_5xx",
                    total_ms=60000.0, ttft_ms=None),
    ]
    st = aggregate(samples, stage_index=0, concurrency=3)
    assert st.ttft["p99"] < 200.0
    assert st.total["p99"] < 2000.0


def test_aggregate_empty_returns_zeroes():
    st = aggregate([], stage_index=2, concurrency=8)
    assert st.requests == 0
    assert st.success_rate == 0.0
    assert st.ttft["p95"] is None
    assert st.concurrency == 8 and st.stage_index == 2
