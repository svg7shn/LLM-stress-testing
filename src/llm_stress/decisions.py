# -*- coding: utf-8 -*-
"""把压测指标翻译成服务决策。

设计原则：所有判定阈值都是模块级常量，报告里原样打印，任何人可复算，
不允许出现"感觉还行"这类无法复现的结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .metrics import (
    ERR_CONNECT,
    ERR_HTTP_4XX,
    ERR_HTTP_5XX,
    ERR_RATE_LIMITED,
    ERR_STREAM_BROKEN,
    ERR_TIMEOUT,
    ERROR_LABELS,
    StageStats,
)
from .scenarios import find_saturation, stage_curve

# ------------------------------------------------------------------ 判定阈值
SLO_DEFAULTS = {
    "ttft_p95_ms": 2000.0,      # 首字 p95 超过 2s，交互式产品体感明显变差
    "total_p95_ms": 30000.0,    # 完整回答 p95 超过 30s
    "success_rate": 0.99,       # 成功率低于 99% 视为不可用
    "drift_ratio": 1.5,         # 长稳后半段 p95 超过前半段 1.5 倍视为性能漂移
}

# 吞吐增幅低于该比例即认为"加并发不再换来吞吐"
SATURATION_MIN_GAIN = 0.15

VERDICT_OK = "可以承载"
VERDICT_LIMITED = "容量受限"
VERDICT_FAIL = "不建议上线"
VERDICT_UNKNOWN = "数据不足"

_ERR_ADVICE = {
    ERR_RATE_LIMITED: "上游对并发做了限流（429）。这不是本机容量问题：要么客户端加退避，要么向服务方提升配额。",
    ERR_HTTP_5XX: "上游 5xx，属于服务端故障或过载保护，需降低并发并重试，或换渠道。",
    ERR_TIMEOUT: "请求超时。先确认是排队过深（降低并发后恢复）还是单请求本身超过 timeout_s。",
    ERR_CONNECT: "连接失败/被重置。检查 Base URL、代理、TLS 证书与网络链路。",
    ERR_HTTP_4XX: "4xx，多为鉴权、模型名或参数问题，与并发无关，先修调用方式再压测。",
    ERR_STREAM_BROKEN: "流式响应中断或 200 却没有任何内容，说明网关截断或后端不稳。",
}


@dataclass(slots=True)
class Decision:
    verdict: str
    recommended_concurrency: int | None
    peak_output_tps: float
    slo: dict[str, float]
    violations: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)
    saturation: dict[str, Any] | None = None
    cost: dict[str, Any] = field(default_factory=dict)
    error_mix: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "recommended_concurrency": self.recommended_concurrency,
            "peak_output_tps": self.peak_output_tps,
            "slo": self.slo,
            "violations": self.violations,
            "findings": self.findings,
            "advice": self.advice,
            "saturation": self.saturation,
            "cost": self.cost,
            "error_mix": self.error_mix,
        }


def _meets(st: StageStats, slo: dict[str, float]) -> list[str]:
    """返回该档不满足的 SLO 条目（空列表 = 全部满足）。"""
    bad: list[str] = []
    ttft_p95 = st.ttft.get("p95")
    if ttft_p95 is None or ttft_p95 > slo["ttft_p95_ms"]:
        bad.append(f"TTFT p95 {ttft_p95} ms > {slo['ttft_p95_ms']:.0f} ms")
    total_p95 = st.total.get("p95")
    if total_p95 is None or total_p95 > slo["total_p95_ms"]:
        bad.append(f"总延迟 p95 {total_p95} ms > {slo['total_p95_ms']:.0f} ms")
    if st.success_rate < slo["success_rate"]:
        bad.append(f"成功率 {st.success_rate * 100:.2f}% < {slo['success_rate'] * 100:.0f}%")
    return bad


def decide(
    stats: list[StageStats],
    *,
    scene_kind: str = "ladder",
    slo: dict[str, float] | None = None,
    drift_info: dict[str, Any] | None = None,
    price_in_per_m: float = 0.0,
    price_out_per_m: float = 0.0,
) -> Decision:
    slo = {**SLO_DEFAULTS, **(slo or {})}
    if not stats:
        return Decision(VERDICT_UNKNOWN, None, 0.0, slo, advice=["没有采集到任何样本。"])

    curve = stage_curve(stats)
    sat = find_saturation(curve, SATURATION_MIN_GAIN)
    errors: dict[str, int] = {}
    for st in stats:
        for k, v in st.errors.items():
            errors[k] = errors.get(k, 0) + int(v)

    # 推荐并发 = 满足全部 SLO 的最大并发档
    ok_stages = [st for st in stats if not _meets(st, slo)]
    recommended = max((st.concurrency for st in ok_stages), default=None)
    violations: list[str] = []
    for st in stats:
        bad = _meets(st, slo)
        if bad:
            violations.append(f"并发 {st.concurrency}：{'；'.join(bad)}")

    # 峰值吞吐只在满足 SLO 的档位里取：失败档的 token 量再高也不代表可用容量
    peak_tps = max((st.output_tps for st in (ok_stages or stats)), default=0.0)
    findings: list[str] = []
    advice: list[str] = []

    if recommended is None:
        verdict = VERDICT_FAIL
        findings.append("没有任何一个并发档同时满足 TTFT/总延迟/成功率三项 SLO。")
        advice.append("先降并发或换渠道复测；若单并发也不达标，问题在链路或上游，不在容量。")
    else:
        verdict = VERDICT_OK
        findings.append(f"在并发 {recommended} 及以下满足全部 SLO。")
        if sat.get("saturation"):
            s = sat["saturation"]
            verdict = VERDICT_LIMITED
            findings.append(
                f"饱和点出现在并发 {s['concurrency']}：{s['reason']}。"
                f"继续加并发只会增加排队与超时。"
            )
            advice.append(f"把并发上限设在 {min(recommended, s['concurrency'])}，超出后排队或限流。")
        elif recommended is not None and sat.get("peak"):
            max_cc = max(st.concurrency for st in stats)
            max_stage = max(stats, key=lambda st: st.concurrency)
            max_bad = _meets(max_stage, slo)
            if max_bad:
                findings.append(
                    f"最高档并发 {max_cc} 已不满足 SLO（{'；'.join(max_bad)}），"
                    f"容量上限落在并发 {recommended} 与 {max_cc} 之间。"
                )
                advice.append(
                    f"先按并发 {recommended} 上线，再以 1~2 的步长向上复测，"
                    f"不要直接跳到 {max_cc}。"
                )
            else:
                advice.append(
                    f"本次最高只测到并发 {max_cc}，"
                    f"吞吐峰值 {sat['peak']['output_tps']:.1f} token/s 尚未见拐点，建议加大并发再测一轮。"
                )

    # 错误归因建议（按数量从多到少，最多 3 条）
    for err, count in sorted(errors.items(), key=lambda kv: -kv[1])[:3]:
        label = ERROR_LABELS.get(err, err)
        findings.append(f"错误分布：{label} × {count}")
        if err in _ERR_ADVICE:
            advice.append(_ERR_ADVICE[err])

    if drift_info and drift_info.get("enough_data"):
        ratio = float(drift_info.get("drift_ratio") or 0.0)
        findings.append(
            f"长稳漂移：后半段 p95 {drift_info.get('back_p95')} ms / 前半段 p95 "
            f"{drift_info.get('front_p95')} ms = {ratio:.2f}（阈值 {slo['drift_ratio']}）"
        )
        if ratio >= slo["drift_ratio"]:
            verdict = VERDICT_LIMITED if verdict == VERDICT_OK else verdict
            advice.append("出现性能漂移：检查连接池是否泄漏、上游是否降级、context 是否持续增长。")

    total_tokens = sum(st.total_tokens for st in stats)
    total_cost = sum(st.cost_usd for st in stats)
    cost = {
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
        "cost_per_mtok_usd": round(total_cost / total_tokens * 1_000_000, 4) if total_tokens and total_cost else 0.0,
        "price_in_per_m": price_in_per_m,
        "price_out_per_m": price_out_per_m,
    }
    if price_in_per_m <= 0 and price_out_per_m <= 0:
        advice.append("未提供单价，成本项为 0；传入 --price-in/--price-out 才能算每次请求花费。")

    if scene_kind == "soak" and not (drift_info and drift_info.get("enough_data")):
        findings.append("长稳样本不足 20 条，漂移结论不成立。")

    return Decision(
        verdict=verdict,
        recommended_concurrency=recommended,
        peak_output_tps=round(peak_tps, 2),
        slo=slo,
        violations=violations,
        findings=findings,
        advice=advice,
        saturation=sat,
        cost=cost,
        error_mix=errors,
    )
