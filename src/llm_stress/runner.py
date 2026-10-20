# -*- coding: utf-8 -*-
"""把 引擎 -> 聚合 -> 决策 -> 报告 串成一次完整压测，供 CLI 与 Web 共用。"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import mask_key, reports_dir
from .datasets import Sample
from .decisions import Decision, decide
from .engine import RunConfig, StageResult, run_stage
from .metrics import RequestSample, StageStats, aggregate
from .report import write_outputs
from .scenarios import Scenario, drift as drift_of

ProgressFn = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class RunResult:
    run_id: str
    scene: str
    stats: list[StageStats]
    samples: list[RequestSample]
    decision: Decision
    drift: dict[str, Any] | None
    meta: dict[str, Any]
    files: dict[str, str] = field(default_factory=dict)
    aborted: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scene": self.scene,
            "verdict": self.decision.verdict,
            "recommended_concurrency": self.decision.recommended_concurrency,
            "peak_output_tps": self.decision.peak_output_tps,
            "stages": [st.to_dict() for st in self.stats],
            "files": self.files,
            "aborted": self.aborted,
        }


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


async def run_scenario(
    *,
    base_url: str,
    api_key: str,
    protocol: str,
    models: list[str],
    dataset: list[Sample],
    scenario: Scenario,
    dataset_label: str = "",
    timeout_s: float = 120.0,
    price_in_per_m: float = 0.0,
    price_out_per_m: float = 0.0,
    out_dir: Path | None = None,
    run_id: str | None = None,
    on_progress: ProgressFn | None = None,
    on_stage_done: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    cancel: asyncio.Event | None = None,
    command_hint: str = "",
    persist: bool = True,
) -> RunResult:
    run_id = run_id or new_run_id()
    started_at = time.time()
    cfg = RunConfig(
        run_id=run_id, base_url=base_url, api_key=api_key, protocol=protocol,
        models=models, dataset=dataset, timeout_s=timeout_s,
        price_in_per_m=price_in_per_m, price_out_per_m=price_out_per_m,
    )
    all_samples: list[RequestSample] = []
    stats: list[StageStats] = []
    aborted = False

    for spec in scenario.stages:
        if cancel is not None and cancel.is_set():
            aborted = True
            break
        result: StageResult = await run_stage(
            cfg, spec, cancel=cancel,
            on_progress=(lambda ev, _spec=spec: _tag_stage(ev, _spec, scenario, on_progress)),
        )
        stage_samples = result.samples
        all_samples.extend(stage_samples)
        st = aggregate(stage_samples, stage_index=spec.index, concurrency=spec.concurrency)
        stats.append(st)
        if on_stage_done is not None:
            await on_stage_done({"kind": "stage_done", "stage": st.to_dict(),
                                 "aborted": result.aborted})
        if result.aborted:
            aborted = True
            break

    drift_info = None
    if scenario.kind == "soak" and all_samples:
        drift_info = drift_of([s.to_dict() for s in all_samples])

    decision = decide(
        stats, scene_kind=scenario.kind, drift_info=drift_info,
        price_in_per_m=price_in_per_m, price_out_per_m=price_out_per_m,
    )
    meta = {
        "scene": scenario.kind,
        "base_url": base_url,
        "api_key_masked": mask_key(api_key),
        "protocol": protocol,
        "models": models,
        "dataset": dataset_label or "-",
        "dataset_size": len(dataset),
        "timeout_s": timeout_s,
        "started_at": started_at,
        "finished_at": time.time(),
        "stages": scenario.describe(),
        "aborted": aborted,
        "command": command_hint or _suggest_command(base_url, models, scenario, dataset_label),
    }
    # persist=False 供横评使用：一次横评要跑 N 个模型，逐个落三件套会把目录刷满，
    # 汇总报告由 compare.write_compare 统一产出。
    files: dict[str, str] = {}
    if persist:
        files = write_outputs(
            out_dir or reports_dir(), run_id=run_id, meta=meta, stats=stats,
            decision=decision, drift_info=drift_info, samples=all_samples,
        )
    return RunResult(run_id=run_id, scene=scenario.kind, stats=stats, samples=all_samples,
                     decision=decision, drift=drift_info, meta=meta, files=files, aborted=aborted)


async def _tag_stage(ev: dict[str, Any], spec, scenario: Scenario, on_progress: ProgressFn | None) -> None:
    if on_progress is None:
        return
    ev = dict(ev)
    ev["scene"] = scenario.kind
    ev["stage_count"] = len(scenario.stages)
    ev["stage_label"] = spec.label
    await on_progress(ev)


def _suggest_command(base_url: str, models: list[str], scenario: Scenario, dataset_label: str) -> str:
    conc = scenario.stages[0].concurrency if scenario.stages else 1
    return (
        f"llm-stress run --base-url {base_url} --api-key $API_KEY "
        f"--model {','.join(models)} --scene {scenario.kind} "
        f"--dataset {dataset_label or 'mixed'} --concurrency {conc}"
    )
