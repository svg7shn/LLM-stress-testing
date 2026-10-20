# -*- coding: utf-8 -*-
"""场景构造与拐点/漂移判定测试。"""
from __future__ import annotations

import pytest

from llm_stress.scenarios import (
    _concurrency_ladder,
    baseline,
    build,
    drift,
    find_saturation,
    ladder,
    soak,
    stage_curve,
)


def test_ladder_doubling():
    assert _concurrency_ladder(1, 16, 2.0) == [1, 2, 4, 8, 16]


def test_ladder_caps_stage_count():
    conc = _concurrency_ladder(1, 4096, 2.0)
    assert len(conc) <= 13          # 最多 12 档 + 补上的 stop
    assert conc[-1] == 4096


def test_baseline_single_stage():
    sc = baseline(requests=20)
    assert sc.kind == "baseline"
    assert len(sc.stages) == 1
    assert sc.stages[0].concurrency == 1
    assert sc.stages[0].requests == 20
    assert sc.total_planned() == 20


def test_ladder_stage_indexes_and_labels():
    sc = ladder(start=1, stop=8, step=2.0, requests_per_stage=30)
    assert [s.concurrency for s in sc.stages] == [1, 2, 4, 8]
    assert [s.index for s in sc.stages] == [0, 1, 2, 3]
    assert all(s.requests == 30 for s in sc.stages)
    assert sc.stages[-1].label == "并发 8"


def test_soak_has_duration_not_requests():
    sc = soak(concurrency=4, duration_s=600)
    assert len(sc.stages) == 1
    assert sc.stages[0].duration_s == 600
    assert sc.stages[0].requests is None
    assert sc.total_planned() is None


def test_build_rejects_unknown_kind():
    with pytest.raises(ValueError):
        build("burst")


def test_stage_curve_shape(make_stats):
    curve = stage_curve([make_stats(concurrency=1), make_stats(concurrency=2)])
    assert [r["concurrency"] for r in curve] == [1, 2]
    assert set(curve[0]) == {"concurrency", "rps", "output_tps", "ttft_p50",
                             "ttft_p95", "total_p95", "success_rate"}


def test_find_saturation_detects_plateau():
    curve = [
        {"concurrency": 1, "output_tps": 10.0, "success_rate": 100.0},
        {"concurrency": 2, "output_tps": 20.0, "success_rate": 100.0},
        {"concurrency": 4, "output_tps": 40.0, "success_rate": 100.0},
        {"concurrency": 8, "output_tps": 42.0, "success_rate": 100.0},
    ]
    sat = find_saturation(curve, min_gain=0.15)
    assert sat["saturation"]["concurrency"] == 8
    assert "增幅" in sat["saturation"]["reason"]
    assert sat["peak"]["concurrency"] == 8


def test_find_saturation_none_when_still_scaling():
    curve = [
        {"concurrency": 1, "output_tps": 10.0, "success_rate": 100.0},
        {"concurrency": 2, "output_tps": 21.0, "success_rate": 100.0},
        {"concurrency": 4, "output_tps": 45.0, "success_rate": 100.0},
    ]
    assert find_saturation(curve)["saturation"] is None


def test_find_saturation_ignores_failed_stages():
    curve = [
        {"concurrency": 1, "output_tps": 10.0, "success_rate": 100.0},
        {"concurrency": 2, "output_tps": 30.0, "success_rate": 80.0},   # 打挂了，不算拐点
        {"concurrency": 4, "output_tps": 40.0, "success_rate": 100.0},
    ]
    sat = find_saturation(curve)
    assert sat["usable_stages"] == 2
    assert sat["saturation"] is None


def test_drift_detects_regression():
    samples = []
    for i in range(20):
        total = 1000.0 if i < 10 else 2500.0
        samples.append({"ok": True, "total_ms": total, "finished_at": float(i)})
    d = drift(samples)
    assert d["enough_data"] is True
    assert d["samples"] == 20
    assert d["drift_ratio"] >= 2.0
    assert d["drifting"] is True


def test_drift_needs_20_samples():
    samples = [{"ok": True, "total_ms": 1000.0, "finished_at": float(i)} for i in range(19)]
    d = drift(samples)
    assert d["enough_data"] is False
    assert d["samples"] == 19
