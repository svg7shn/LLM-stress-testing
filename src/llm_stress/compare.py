# -*- coding: utf-8 -*-
"""多模型横评：同一套场景、同一份数据集，逐个模型串行跑一遍再横向比。

为什么不能直接用 run 的 --model a,b
------------------------------------
引擎的调度是"同一档内按请求轮询取模型"（engine.py 的
model = cfg.models[idx % len(cfg.models)]），那是混跑：测出来的是这些模型
合起来的容量，回答不了"哪个模型更适合上线"。横评必须一个模型一条独立序列，
保证并发档位、数据集、超时完全一致，再比同一套指标。

排名口径（全部可复算，不含主观打分）
------------------------------------
1. 先按结论档位：可以承载 > 容量受限 > 不建议上线 > 数据不足；
2. 同档位比推荐并发（越大越能扛）；
3. 再比峰值输出吞吐（token/s）；
4. 最后比推荐档位的 TTFT p95（越小越跟手）。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import mask_key, reports_dir
from .datasets import Sample
from .decisions import VERDICT_FAIL, VERDICT_LIMITED, VERDICT_OK, VERDICT_UNKNOWN
from .metrics import StageStats
from .runner import RunResult, new_run_id, run_scenario
from .scenarios import Scenario

ProgressFn = Callable[[dict[str, Any]], Awaitable[None]]

_VERDICT_RANK: dict[str, int] = {
    VERDICT_OK: 0,
    VERDICT_LIMITED: 1,
    VERDICT_FAIL: 2,
    VERDICT_UNKNOWN: 3,
}


def _slug(name: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z._-]+", "-", (name or "").strip()).strip("-.")
    return slug[:60] or "model"


def _fmt(value: Any, digits: int = 0, suffix: str = "") -> str:
    if value is None:
        return "-"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    if digits <= 0:
        return f"{num:.0f}{suffix}"
    return f"{num:.{digits}f}{suffix}"


@dataclass
class CompareEntry:
    """一个模型的横评结果槽位：要么有 result，要么有 error，两者不会同时有值。"""

    model: str
    info: dict[str, Any] = field(default_factory=dict)
    result: RunResult | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.result is not None and not self.error

    def stage_at_recommended(self) -> StageStats | None:
        """取推荐并发对应的那一档；找不到就退回最后一档，保证指标有出处。"""
        if self.result is None or not self.result.stats:
            return None
        target = int(self.result.decision.recommended_concurrency or 0)
        for st in self.result.stats:
            if st.concurrency == target:
                return st
        return self.result.stats[-1]

    def cost_per_mtok(self) -> float | None:
        """按实际累计 token 与累计成本折算，未配价格时返回 None。"""
        if self.result is None:
            return None
        tokens = sum(int(st.total_tokens or 0) for st in self.result.stats)
        cost = sum(float(st.cost_usd or 0.0) for st in self.result.stats)
        if tokens <= 0 or cost <= 0:
            return None
        return cost / tokens * 1_000_000.0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "model": self.model,
            "type": self.info.get("type", ""),
            "type_label": self.info.get("type_label", ""),
            "family": self.info.get("family", ""),
            "family_label": self.info.get("family_label", ""),
            "error": self.error,
            "cost_per_mtok": self.cost_per_mtok(),
        }
        if self.result is None:
            out.update({
                "verdict": "", "recommended_concurrency": None, "peak_output_tps": None,
                "ttft_p50": None, "ttft_p95": None, "total_p95": None,
                "success_rate": None, "rps": None, "stages": [],
            })
            return out
        st = self.stage_at_recommended()
        d = self.result.decision
        out.update({
            "verdict": d.verdict,
            "recommended_concurrency": d.recommended_concurrency,
            "peak_output_tps": d.peak_output_tps,
            "ttft_p50": (st.ttft.get("p50") if st else None),
            "ttft_p95": (st.ttft.get("p95") if st else None),
            "total_p95": (st.total.get("p95") if st else None),
            "success_rate": (st.success_rate if st else None),
            "rps": (st.rps if st else None),
            "findings": list(d.findings),
            "advice": list(d.advice),
            "stages": [s.to_dict() for s in self.result.stats],
        })
        return out


@dataclass
class CompareResult:
    run_id: str
    scene: str
    entries: list[CompareEntry]
    meta: dict[str, Any] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    cancelled: bool = False

    def ranked(self) -> list[CompareEntry]:
        return sorted(self.entries, key=_rank_key)

    def champion(self) -> CompareEntry | None:
        for entry in self.ranked():
            if entry.result is not None:
                return entry
        return None

    def failed(self) -> list[CompareEntry]:
        return [e for e in self.entries if not e.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scene": self.scene,
            "meta": self.meta,
            "cancelled": self.cancelled,
            "ranking": [e.model for e in self.ranked()],
            "entries": [e.to_dict() for e in self.entries],
            "files": self.files,
        }


def _rank_key(entry: CompareEntry) -> tuple:
    if entry.result is None:
        return (9, 0, 0.0, float("inf"))
    d = entry.result.decision
    st = entry.stage_at_recommended()
    ttft = None
    if st is not None:
        ttft = st.ttft.get("p95")
    return (
        _VERDICT_RANK.get(d.verdict, 9),
        -int(d.recommended_concurrency or 0),
        -float(d.peak_output_tps or 0.0),
        float(ttft) if ttft is not None else float("inf"),
    )


async def _emit(fn: ProgressFn | None, ev: dict[str, Any]) -> None:
    if fn is not None:
        await fn(ev)


def _suggest_command(meta: dict[str, Any], models: list[str]) -> str:
    return (
        f"llm-stress compare --base-url {meta.get('base_url')} --api-key $API_KEY "
        f"--model {','.join(models)} --scene {meta.get('scene')} "
        f"--dataset {meta.get('dataset') or 'mixed'}"
    )


async def run_compare(
    *,
    base_url: str,
    api_key: str,
    protocol: str,
    models: list[str],
    dataset: list[Sample],
    scenario_factory: Callable[[str], Scenario],
    infos: dict[str, dict[str, Any]] | None = None,
    dataset_label: str = "",
    timeout_s: float = 120.0,
    price_in_per_m: float = 0.0,
    price_out_per_m: float = 0.0,
    out_dir: Path | None = None,
    run_id: str | None = None,
    on_progress: ProgressFn | None = None,
    on_stage_done: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    cancel: Any = None,
    command_hint: str = "",
) -> CompareResult:
    """逐模型串行压测，最后产出对比报告。

    串行而不是并发：压测端自己抢带宽会把彼此的结果搅浑，横评的前提是
    每个模型都独享同一条链路。
    """
    run_id = run_id or new_run_id()
    started_at = time.time()
    entries: list[CompareEntry] = []
    cancelled = False

    for index, model in enumerate(models):
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            cancelled = True
            break
        scenario = scenario_factory(model)
        entry = CompareEntry(model=model, info=(infos or {}).get(model, {}))
        entries.append(entry)
        await _emit(on_progress, {
            "kind": "model_start",
            "model": model,
            "index": index,
            "total": len(models),
            "scene": scenario.kind,
            "stages": [s.concurrency for s in scenario.stages],
        })
        try:
            entry.result = await run_scenario(
                base_url=base_url,
                api_key=api_key,
                protocol=protocol,
                models=[model],
                dataset=dataset,
                dataset_label=dataset_label,
                scenario=scenario,
                timeout_s=timeout_s,
                price_in_per_m=price_in_per_m,
                price_out_per_m=price_out_per_m,
                out_dir=out_dir,
                run_id=f"{run_id}-{_slug(model)}",
                on_progress=on_progress,
                on_stage_done=on_stage_done,
                cancel=cancel,
                command_hint=command_hint,
                persist=False,
            )
        except Exception as exc:  # 单个模型失败不拖垮整场横评
            entry.error = f"{type(exc).__name__}: {exc}"
        await _emit(on_progress, {
            "kind": "model_done",
            "model": model,
            "index": index,
            "total": len(models),
            "ok": entry.ok,
            "verdict": (entry.result.decision.verdict if entry.result else ""),
            "recommended_concurrency": (
                entry.result.decision.recommended_concurrency if entry.result else None
            ),
            "peak_output_tps": (entry.result.decision.peak_output_tps if entry.result else None),
            "error": entry.error,
        })

    scene = ""
    for entry in entries:
        if entry.result is not None:
            scene = entry.result.scene
            break
    meta: dict[str, Any] = {
        "scene": scene,
        "base_url": base_url,
        "api_key_masked": mask_key(api_key),
        "protocol": protocol,
        "models": list(models),
        "tested": len(entries),
        "dataset": dataset_label or "-",
        "dataset_size": len(dataset),
        "timeout_s": timeout_s,
        "started_at": started_at,
        "finished_at": time.time(),
        "cancelled": cancelled,
        "command": command_hint or _suggest_command(
            {"base_url": base_url, "scene": scene, "dataset": dataset_label}, models
        ),
    }
    result = CompareResult(run_id=run_id, scene=scene, entries=entries,
                           meta=meta, cancelled=cancelled)
    result.files = write_compare(out_dir or reports_dir(), result)
    return result


def render_markdown(result: CompareResult) -> str:
    """把横评结果写成一份可直接贴进评审的 markdown。"""
    meta = result.meta
    ranked = result.ranked()
    tested = [e for e in ranked if e.result is not None]
    lines: list[str] = []
    lines.append(f"# 多模型横评 {result.run_id}")
    lines.append("")
    lines.append("## 一句话结论")
    champ = result.champion()
    if champ is not None and champ.result is not None:
        st = champ.stage_at_recommended()
        d = champ.result.decision
        ttft_p95 = st.ttft.get("p95") if st else None
        lines.append(
            f"在 {len(tested)} 个模型里 **{champ.model}** 综合排第一："
            f"结论「{d.verdict}」，推荐并发 {d.recommended_concurrency}，"
            f"峰值输出 {_fmt(d.peak_output_tps, 1)} token/s，"
            f"该档 TTFT p95 {_fmt(ttft_p95)} ms。"
        )
    else:
        lines.append("本次没有任何模型跑出可用结果，先看下面的失败原因。")
    lines.append("")
    lines.append("## 对比表")
    lines.append("")
    lines.append("| 排名 | 模型 | 类型 | 厂商 | 结论 | 推荐并发 | 峰值吞吐(token/s) | TTFT p50 | TTFT p95 | 成功率(推荐档) | 成本/百万token |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for rank, entry in enumerate(ranked, 1):
        d = entry.to_dict()
        if entry.result is None:
            lines.append(
                f"| {rank} | {entry.model} | {d.get('type_label') or '-'} | "
                f"{d.get('family_label') or '-'} | 未完成 | - | - | - | - | - | - |"
            )
            continue
        success = d.get("success_rate")
        success_text = _fmt((success or 0) * 100, 1, "%") if success is not None else "-"
        lines.append(
            f"| {rank} | {entry.model} | {d.get('type_label') or '-'} | {d.get('family_label') or '-'} | "
            f"{d.get('verdict')} | {d.get('recommended_concurrency')} | "
            f"{_fmt(d.get('peak_output_tps'), 1)} | {_fmt(d.get('ttft_p50'))} | "
            f"{_fmt(d.get('ttft_p95'))} | {success_text} | "
            f"{_fmt(d.get('cost_per_mtok'), 4)} |"
        )
    lines.append("")
    lines.append("## 排名依据")
    lines.append("")
    lines.append("1. 先按结论档位：可以承载 > 容量受限 > 不建议上线 > 数据不足；")
    lines.append("2. 同档位比推荐并发（能承载更多并发者优先）；")
    lines.append("3. 再比峰值输出吞吐（token/s 高者优先）；")
    lines.append("4. 最后比推荐档位的 TTFT p95（越小越跟手）。")
    lines.append("")
    lines.append("## 测试口径")
    lines.append("")
    lines.append(f"- 场景：{meta.get('scene') or '-'}")
    lines.append(f"- 数据集：{meta.get('dataset')}（{meta.get('dataset_size')} 条）")
    lines.append(f"- 单请求超时：{meta.get('timeout_s')} s")
    lines.append(f"- 协议：{meta.get('protocol')}")
    lines.append(f"- 目标：{meta.get('base_url')}（key {meta.get('api_key_masked')}）")
    lines.append("- 执行方式：逐模型串行，每个模型独立跑完整套并发档位")
    if result.cancelled:
        lines.append("- **本次被中途取消，部分模型未跑完**")
    lines.append("")
    lines.append("## 各模型详情")
    lines.append("")
    for entry in result.entries:
        lines.append(f"### {entry.model}")
        if entry.result is None:
            lines.append("")
            lines.append(f"- 未完成：{entry.error or '无错误信息'}")
            lines.append("")
            continue
        d = entry.result.decision
        lines.append("")
        lines.append(f"- 结论：{d.verdict}（推荐并发 {d.recommended_concurrency}，"
                     f"峰值 {_fmt(d.peak_output_tps, 1)} token/s）")
        for f in d.findings:
            lines.append(f"- 发现：{f}")
        for a in d.advice:
            lines.append(f"- 建议：{a}")
        lines.append("")
    skipped = [e for e in result.entries if e.result is None and e.error]
    if skipped:
        lines.append("## 未完成原因")
        lines.append("")
        for entry in skipped:
            lines.append(f"- {entry.model}：{entry.error}")
        lines.append("")
    lines.append("## 复现命令")
    lines.append("")
    lines.append("```bash")
    lines.append(str(meta.get("command") or ""))
    lines.append("```")
    lines.append("")
    lines.append("> 口径说明：推荐并发与峰值吞吐来自 decisions.decide()，"
                 "成功率与 TTFT 取推荐并发那一档的实测值；成本按实际累计 token 折算，"
                 "未配置单价时显示 -。")
    lines.append("")
    return "\n".join(lines)


def write_compare(out_dir: Path, result: CompareResult) -> dict[str, str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{result.run_id}.compare.md"
    json_path = out_dir / f"{result.run_id}.compare.json"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    json_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"markdown": str(md_path), "json": str(json_path)}
