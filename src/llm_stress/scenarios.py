# -*- coding: utf-8 -*-
"""压测场景：baseline（基线）/ ladder（阶梯）/ soak（长稳）。

三档场景的判定口径来自压测方法论，不靠形容词：
  - baseline：单并发，用来确定"这台服务最好能做到多快"，后续所有对比都以它为准；
  - ladder：并发 1→2→4→8…，找吞吐曲线由升转平的拐点（saturation_point）；
  - soak：固定并发跑 duration 秒，看延迟是否随时间漂移（漂移率 = 后半段 p95 / 前半段 p95）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .engine import StageSpec

SCENARIO_KINDS = ("baseline", "ladder", "soak")


@dataclass(slots=True)
class Scenario:
    kind: str
    stages: list[StageSpec]
    requests_per_stage: int | None = None
    duration_s: float | None = None
    note: str = ""

    def total_planned(self) -> int | None:
        if any(s.requests is None for s in self.stages):
            return None
        return sum(int(s.requests or 0) for s in self.stages)

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "stages": [
                {"index": s.index, "concurrency": s.concurrency,
                 "requests": s.requests, "duration_s": s.duration_s, "label": s.label}
                for s in self.stages
            ],
            "total_planned": self.total_planned(),
            "note": self.note,
        }


def _concurrency_ladder(start: int, stop: int, step: float) -> list[int]:
    """按倍数递增，去重保序，末项固定补上 stop。"""
    values: list[int] = []
    cur = float(max(1, start))
    while cur < stop and len(values) < 12:
        values.append(int(round(cur)))
        cur *= step
    values.append(int(stop))
    out: list[int] = []
    seen: set[int] = set()
    for v in values:
        v = max(1, v)
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def baseline(requests: int = 20) -> Scenario:
    return Scenario(
        kind="baseline",
        stages=[StageSpec(index=0, concurrency=1, requests=requests, label="基线 并发1")],
        requests_per_stage=requests,
        note="单并发测出基准 TTFT/TPOT，作为后续阶梯的对照线。",
    )


def ladder(
    start: int = 1,
    stop: int = 16,
    step: float = 2.0,
    requests_per_stage: int = 30,
) -> Scenario:
    conc = _concurrency_ladder(start, stop, step)
    stages = [
        StageSpec(index=i, concurrency=c, requests=requests_per_stage,
                  label=f"并发 {c}")
        for i, c in enumerate(conc)
    ]
    return Scenario(
        kind="ladder",
        stages=stages,
        requests_per_stage=requests_per_stage,
        note="逐档加压，用于定位吞吐拐点与延迟抬升的起点。",
    )


def soak(concurrency: int = 4, duration_s: float = 600.0) -> Scenario:
    return Scenario(
        kind="soak",
        stages=[StageSpec(index=0, concurrency=concurrency, duration_s=duration_s,
                          label=f"长稳 并发{concurrency} {int(duration_s)}s")],
        duration_s=duration_s,
        note="固定并发持续跑，观察延迟漂移、错误累积与内存/连接泄漏迹象。",
    )


def build(
    kind: str,
    *,
    requests: int = 20,
    start: int = 1,
    stop: int = 16,
    step: float = 2.0,
    requests_per_stage: int = 30,
    concurrency: int = 4,
    duration_s: float = 600.0,
) -> Scenario:
    if kind == "baseline":
        return baseline(requests)
    if kind == "ladder":
        return ladder(start, stop, step, requests_per_stage)
    if kind == "soak":
        return soak(concurrency, duration_s)
    raise ValueError(f"未知场景 {kind}，可选：{', '.join(SCENARIO_KINDS)}")


# ---------------- 场景结果分析 ----------------

def stage_curve(stats: list[Any]) -> list[dict[str, Any]]:
    """把每档的并发-吞吐-延迟压成一条曲线，用于定位拐点。"""
    rows: list[dict[str, Any]] = []
    for st in stats:
        rows.append({
            "concurrency": st.concurrency,
            "rps": st.rps,
            "output_tps": st.output_tps,
            "ttft_p50": st.ttft.get("p50"),
            "ttft_p95": st.ttft.get("p95"),
            "total_p95": st.total.get("p95"),
            "success_rate": st.success_rate,
        })
    return rows


def _success_ratio(value: Any) -> float:
    """把成功率统一成 0~1：1.0 与 100.0 都读成 100%。"""
    try:
        ratio = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return ratio / 100.0 if ratio > 1.5 else ratio


def find_saturation(curve: list[dict[str, Any]], min_gain: float = 0.15) -> dict[str, Any]:
    """拐点判定：吞吐增幅连续低于 min_gain（相对上一档）的第一档，视为饱和起点。

    只看成功率仍 ≥95% 的档位，失败档位单独报告，避免把"打挂了"当成拐点。
    success_rate 允许两种写法：0~1 的比例（StageStats 原生）或 0~100 的百分数（手写曲线）。
    """
    usable = [r for r in curve if _success_ratio(r.get("success_rate")) >= 0.95]
    sat: dict[str, Any] | None = None
    peak = max(usable, key=lambda r: r.get("output_tps") or 0, default=None)
    for prev, cur in zip(usable, usable[1:]):
        p = prev.get("output_tps") or 0.0
        c = cur.get("output_tps") or 0.0
        if p <= 0:
            continue
        gain = (c - p) / p
        if gain < min_gain:
            sat = {
                "concurrency": cur["concurrency"],
                "reason": f"输出吞吐由 {p:.1f} token/s 增至 {c:.1f} token/s，增幅 {gain * 100:.1f}% 低于 {min_gain * 100:.0f}%",
                "output_tps": c,
            }
            break
    return {
        "saturation": sat,
        "peak": {"concurrency": peak["concurrency"], "output_tps": peak.get("output_tps")} if peak else None,
        "usable_stages": len(usable),
        "total_stages": len(curve),
    }


def drift(curve_samples: list[dict[str, Any]]) -> dict[str, Any]:
    """长稳漂移：按完成时间排序，前半段与后半段各取 p95 总延迟。"""
    rows = [r for r in curve_samples if r.get("ok") and r.get("total_ms")]
    rows.sort(key=lambda r: r.get("finished_at") or 0.0)
    if len(rows) < 20:
        return {"enough_data": False, "samples": len(rows)}
    half = len(rows) // 2
    front = _p95([float(r["total_ms"]) for r in rows[:half]])
    back = _p95([float(r["total_ms"]) for r in rows[half:]])
    ratio = (back / front) if front > 0 else 0.0
    return {
        "enough_data": True,
        "samples": len(rows),
        "front_p95": round(front, 1),
        "back_p95": round(back, 1),
        "drift_ratio": round(ratio, 2),
        "drifting": ratio >= 1.5,
    }


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]
