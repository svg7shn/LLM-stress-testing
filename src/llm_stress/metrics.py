# -*- coding: utf-8 -*-
"""指标采集与聚合。

口径（全项目唯一真源，报告里逐条复述，避免第二次解释偏差）：
  ttft_ms     请求发出 -> 首个内容 token 到达的毫秒数；空响应记 None
  total_ms    请求发出 -> 流结束（或报错）的毫秒数
  tpot_ms     (total_ms - ttft_ms) / max(out_tokens - 1, 1)：单个输出 token 的平均间隔
  output_tps  out_tokens / (total_ms / 1000)：该请求端到端的生成速度
  rps         阶段内"成功请求数 / 阶段窗口秒数"（不是发送数）
  output_tps  阶段内"成功请求 output token 总和 / 阶段窗口秒数"
  分位数      线性插值，与 numpy.percentile 默认口径一致；不要用近邻取序，
              否则小样本的 p50/p99 会系统性偏低（原 platform/forge 实现的问题）

失败请求不进入延迟分位数（否则超时会把 p99 拉到 timeout 上限，掩盖真实分布），
但进入成功率、状态码分布与错误分类统计。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------- 错误分类
ERR_NONE = "none"
ERR_TIMEOUT = "timeout"
ERR_CONNECT = "connect"
ERR_HTTP_4XX = "http_4xx"
ERR_RATE_LIMITED = "rate_limited"   # 429 / 限流模型特有的 5xx 提示
ERR_HTTP_5XX = "http_5xx"
ERR_STREAM_BROKEN = "stream_broken"  # 200 但流中途断裂 / 无任何内容
ERR_CANCELLED = "cancelled"

ERROR_LABELS = {
    ERR_NONE: "成功",
    ERR_TIMEOUT: "超时",
    ERR_CONNECT: "连接失败",
    ERR_HTTP_4XX: "4xx（鉴权/参数/模型不存在）",
    ERR_RATE_LIMITED: "被限流（429）",
    ERR_HTTP_5XX: "5xx（上游故障）",
    ERR_STREAM_BROKEN: "流中断/空响应",
    ERR_CANCELLED: "已取消",
}


def percentile(values: list[float], q: float) -> float | None:
    """线性插值分位数。q 取 0~100。空列表返回 None。"""
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    pos = (len(ordered) - 1) * (q / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(ordered[int(pos)])
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo))


# ---------------------------------------------------------------- 单请求样本
@dataclass(slots=True)
class RequestSample:
    """一次 HTTP 请求的完整事实。落盘后可复算全部聚合指标。"""

    run_id: str
    stage_index: int
    concurrency: int
    model: str
    protocol: str
    sample_id: str
    started_at: float          # epoch 秒（wall clock），用于跨进程对齐
    finished_at: float
    ok: bool
    status: int                # 0 表示没拿到 HTTP 状态（连接/超时）
    error_class: str = ERR_NONE
    error_detail: str = ""
    ttft_ms: float | None = None
    total_ms: float = 0.0
    in_tokens: int = 0
    out_tokens: int = 0
    out_tokens_source: str = "none"   # usage / counted / estimated / none
    finish_reason: str = ""
    retry_count: int = 0
    cost_usd: float = 0.0

    @property
    def window_s(self) -> float:
        return max(self.finished_at - self.started_at, 1e-6)

    @property
    def tpot_ms(self) -> float | None:
        if not self.ok or self.ttft_ms is None or self.out_tokens <= 0:
            return None
        decode_ms = self.total_ms - self.ttft_ms
        if decode_ms <= 0:
            return None
        return decode_ms / max(self.out_tokens - 1, 1)

    @property
    def output_tps(self) -> float | None:
        if not self.ok or self.out_tokens <= 0 or self.total_ms <= 0:
            return None
        return self.out_tokens / (self.total_ms / 1000.0)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tpot_ms"] = self.tpot_ms
        d["output_tps"] = self.output_tps
        return d


# ---------------------------------------------------------------- 阶段聚合
@dataclass(slots=True)
class StageStats:
    stage_index: int
    concurrency: int
    requests: int
    ok: int
    failed: int
    success_rate: float
    window_s: float
    rps: float                 # 成功请求 / 秒
    offered_rps: float         # 全部请求 / 秒
    output_tps: float          # 成功请求 output token / 秒
    input_tps: float
    ttft: dict[str, float | None] = field(default_factory=dict)
    total: dict[str, float | None] = field(default_factory=dict)
    tpot: dict[str, float | None] = field(default_factory=dict)
    output_tps_p50: float | None = None
    statuses: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    rate_limited: int = 0
    avg_out_tokens: float = 0.0
    total_tokens: int = 0
    cost_usd: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


QUANTILES = (50, 90, 95, 99)


def _qmap(values: list[float]) -> dict[str, float | None]:
    return {f"p{q}": percentile(values, q) for q in QUANTILES}


def aggregate(samples: list[RequestSample], *, stage_index: int, concurrency: int) -> StageStats:
    """把一个阶段内全部样本压成一组指标。样本为空时返回零值。"""
    if not samples:
        return StageStats(stage_index=stage_index, concurrency=concurrency, requests=0, ok=0,
                          failed=0, success_rate=0.0, window_s=0.0, rps=0.0, offered_rps=0.0,
                          output_tps=0.0, input_tps=0.0, ttft=_qmap([]), total=_qmap([]),
                          tpot=_qmap([]))

    started = min(s.started_at for s in samples)
    finished = max(s.finished_at for s in samples)
    window = max(finished - started, 1e-6)
    ok_samples = [s for s in samples if s.ok]
    n_ok = len(ok_samples)

    ttft_vals = [s.ttft_ms for s in ok_samples if s.ttft_ms is not None]
    total_vals = [s.total_ms for s in ok_samples]
    tpot_vals = [t for t in (s.tpot_ms for s in ok_samples) if t is not None]
    otps_vals = [t for t in (s.output_tps for s in ok_samples) if t is not None]

    statuses: dict[str, int] = {}
    errors: dict[str, int] = {}
    for s in samples:
        statuses[str(s.status)] = statuses.get(str(s.status), 0) + 1
        if not s.ok:
            errors[s.error_class] = errors.get(s.error_class, 0) + 1

    out_tokens = sum(s.out_tokens for s in ok_samples)
    in_tokens = sum(s.in_tokens for s in ok_samples)
    rate_limited = sum(1 for s in samples if s.error_class == ERR_RATE_LIMITED)

    return StageStats(
        stage_index=stage_index,
        concurrency=concurrency,
        requests=len(samples),
        ok=n_ok,
        failed=len(samples) - n_ok,
        success_rate=round(n_ok / len(samples), 4),
        window_s=round(window, 3),
        rps=round(n_ok / window, 2),
        offered_rps=round(len(samples) / window, 2),
        output_tps=round(out_tokens / window, 2),
        input_tps=round(in_tokens / window, 2),
        ttft={k: (round(v, 1) if v is not None else None) for k, v in _qmap(ttft_vals).items()},
        total={k: (round(v, 1) if v is not None else None) for k, v in _qmap(total_vals).items()},
        tpot={k: (round(v, 2) if v is not None else None) for k, v in _qmap(tpot_vals).items()},
        output_tps_p50=round(percentile(otps_vals, 50), 2) if otps_vals else None,
        statuses=dict(sorted(statuses.items())),
        errors=errors,
        rate_limited=rate_limited,
        avg_out_tokens=round(out_tokens / n_ok, 1) if n_ok else 0.0,
        total_tokens=out_tokens + in_tokens,
        cost_usd=round(sum(s.cost_usd for s in samples), 6),
        started_at=started,
        finished_at=finished,
    )
