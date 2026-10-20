# -*- coding: utf-8 -*-
"""并发执行引擎：单阶段内启动 N 个 worker，各自串行发请求，天然形成并发上限。

相对旧实现（platform/backend/app/forge/engine.py）的三处硬改动：
  1. 每个 worker 是独立协程，进度上报用无界队列 + 后台消费，绝不在压测路径上 await 任何 IO；
  2. 请求走流式，才能拿到 TTFT / TPOT；
  3. 计数只在无 await 的临界区内递增，不存在"锁内等 UI"导致的自我降速。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from .datasets import Sample
from .metrics import RequestSample
from .transport import stream_chat

ProgressFn = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class StageSpec:
    """一个并发档位。requests 与 duration_s 二选一。"""

    index: int
    concurrency: int
    requests: int | None = None
    duration_s: float | None = None
    label: str = ""

    def planned(self) -> int | None:
        return self.requests


@dataclass(slots=True)
class RunConfig:
    run_id: str
    base_url: str
    api_key: str
    protocol: str
    models: list[str]
    dataset: list[Sample]
    timeout_s: float = 120.0
    price_in_per_m: float = 0.0    # 每百万输入 token 单价（美元），0 = 不计成本
    price_out_per_m: float = 0.0
    abort_on_error_rate: float = 0.9   # 已发出 ≥20 个请求后，错误率超过 90% 即熔断
    max_tokens_override: int | None = None
    emit_interval_s: float = 0.25


@dataclass
class StageResult:
    spec: StageSpec
    samples: list[RequestSample] = field(default_factory=list)
    window_s: float = 0.0
    aborted: bool = False


def _cost(cfg: RunConfig, in_tok: int, out_tok: int) -> float:
    if cfg.price_in_per_m <= 0 and cfg.price_out_per_m <= 0:
        return 0.0
    return in_tok / 1_000_000 * cfg.price_in_per_m + out_tok / 1_000_000 * cfg.price_out_per_m


async def run_stage(
    cfg: RunConfig,
    spec: StageSpec,
    *,
    on_progress: ProgressFn | None = None,
    cancel: asyncio.Event | None = None,
) -> StageResult:
    cancel = cancel or asyncio.Event()
    samples: list[RequestSample] = []
    counter = {"next": 0, "done": 0, "ok": 0, "fail": 0, "abort": False}
    lock = asyncio.Lock()
    recent: list[dict[str, Any]] = []
    started_at = time.time()
    t_start = time.perf_counter()

    events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def emitter() -> None:
        """单独协程消费进度事件：worker 只 put_nowait，永不等待 UI。"""
        while True:
            item = await events.get()
            if item is None:
                return
            if on_progress is not None:
                try:
                    await on_progress(item)
                except Exception:
                    pass

    emitter_task = asyncio.create_task(emitter())
    last_emit = {"t": 0.0}

    def snapshot(idx: int, model: str) -> dict[str, Any]:
        elapsed = time.perf_counter() - t_start
        ttfts = [s.ttft_ms for s in samples if s.ok and s.ttft_ms is not None]
        totals = [s.total_ms for s in samples if s.ok]
        recent_slice = recent[-12:]
        return {
            "kind": "progress",
            "stage_index": spec.index,
            "concurrency": spec.concurrency,
            "planned": spec.planned(),
            "sent": counter["done"],
            "dispatched": counter["next"],
            "ok": counter["ok"],
            "fail": counter["fail"],
            "elapsed_s": round(elapsed, 2),
            "duration_s": spec.duration_s,
            "rps": round(counter["ok"] / elapsed, 2) if elapsed > 0 else 0.0,
            "ttft_p50": round(sorted(ttfts)[len(ttfts) // 2], 1) if ttfts else None,
            "total_p50": round(sorted(totals)[len(totals) // 2], 1) if totals else None,
            "current_model": model,
            "recent": recent_slice,
        }

    def maybe_emit(model: str) -> None:
        now = time.perf_counter()
        if now - last_emit["t"] < cfg.emit_interval_s:
            return
        last_emit["t"] = now
        events.put_nowait(snapshot(-1, model))

    limits = httpx.Limits(max_connections=spec.concurrency + 4,
                          max_keepalive_connections=spec.concurrency + 4,
                          keepalive_expiry=30.0)
    # trust_env=False：压测必须直连目标。若复用系统代理，延迟会被代理链路污染，
    # 且部分代理对内网/回环地址会直接报错（实测 httpx 0.28 + NO_PROXY 含 [::1] 会抛 InvalidURL）。
    async with httpx.AsyncClient(limits=limits, verify=False, follow_redirects=False,
                                  trust_env=False) as client:

        async def worker(wid: int) -> None:
            while True:
                if cancel.is_set():
                    return
                async with lock:
                    if counter["abort"]:
                        return
                    if spec.requests is not None and counter["next"] >= spec.requests:
                        return
                    if spec.duration_s is not None and time.perf_counter() - t_start >= spec.duration_s:
                        return
                    idx = counter["next"]
                    counter["next"] += 1
                sample = cfg.dataset[idx % len(cfg.dataset)]
                model = cfg.models[idx % len(cfg.models)]
                max_tokens = cfg.max_tokens_override or sample.max_tokens

                outcome = await stream_chat(
                    client, base_url=cfg.base_url, api_key=cfg.api_key,
                    protocol=cfg.protocol, model=model, prompt=sample.prompt,
                    max_tokens=max_tokens, timeout_s=cfg.timeout_s,
                )
                finished_at = time.time()
                rs = RequestSample(
                    run_id=cfg.run_id,
                    stage_index=spec.index,
                    concurrency=spec.concurrency,
                    model=model,
                    protocol=cfg.protocol,
                    sample_id=sample.id,
                    started_at=finished_at - outcome.total_ms / 1000.0,
                    finished_at=finished_at,
                    ok=outcome.ok,
                    status=outcome.status,
                    error_class=outcome.error_class,
                    error_detail=outcome.error_detail[:500],
                    ttft_ms=round(outcome.ttft_ms, 1) if outcome.ttft_ms is not None else None,
                    total_ms=round(outcome.total_ms, 1),
                    in_tokens=outcome.in_tokens,
                    out_tokens=outcome.out_tokens,
                    out_tokens_source=outcome.out_tokens_source,
                    finish_reason=outcome.finish_reason,
                    retry_count=outcome.retry_count,
                    cost_usd=_cost(cfg, outcome.in_tokens, outcome.out_tokens) if outcome.ok else 0.0,
                )
                async with lock:
                    samples.append(rs)
                    counter["done"] += 1
                    if rs.ok:
                        counter["ok"] += 1
                    else:
                        counter["fail"] += 1
                    total_done = counter["done"]
                    if total_done >= 20:
                        fail_rate = counter["fail"] / total_done
                        if fail_rate > cfg.abort_on_error_rate:
                            counter["abort"] = True
                recent.append({
                    "t": round(finished_at - started_at, 2),
                    "ok": rs.ok,
                    "ttft_ms": rs.ttft_ms,
                    "total_ms": rs.total_ms,
                })
                if len(recent) > 200:
                    del recent[:100]
                maybe_emit(model)

        workers = [asyncio.create_task(worker(i)) for i in range(max(1, spec.concurrency))]
        try:
            await asyncio.gather(*workers)
        finally:
            events.put_nowait(None)
            try:
                await asyncio.wait_for(emitter_task, timeout=2.0)
            except Exception:
                emitter_task.cancel()

    window = time.perf_counter() - t_start
    return StageResult(spec=spec, samples=samples, window_s=round(window, 3),
                       aborted=counter["abort"])
