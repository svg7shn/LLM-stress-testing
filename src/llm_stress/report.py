# -*- coding: utf-8 -*-
"""把一次压测的样本、聚合结果与决策渲染成 Markdown / JSON。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .decisions import Decision
from .metrics import ERROR_LABELS, RequestSample, StageStats

METRIC_GLOSSARY = [
    ("TTFT", "首字延迟：请求发出到第一个内容 token 到达。直接决定打字机效果的体感。"),
    ("TPOT", "每个输出 token 的平均间隔 =（总耗时-TTFT）/（输出 token 数-1），衡量吐字流畅度。"),
    ("输出吞吐", "单位时间内成功请求产生的输出 token 总数，是容量规划的主指标。"),
    ("RPS", "阶段内成功请求数 / 阶段窗口秒数；失败请求不计入。"),
    ("分位数", "线性插值口径（与 numpy.percentile 默认一致），失败请求不进入延迟分布。"),
]

FENCE = chr(96) * 3


def _fmt(v: Any, unit: str = "", digits: int = 1) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{digits}f}{unit}"
    return f"{v}{unit}"


def _pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def render_markdown(
    *,
    run_id: str,
    meta: dict[str, Any],
    stats: list[StageStats],
    decision: Decision,
    drift_info: dict[str, Any] | None = None,
    samples: list[RequestSample] | None = None,
) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"# LLM 压力测试报告 · {meta.get('scene', '-')}")
    add("")
    add(f"- 运行 ID：{run_id}")
    add("- 开始时间：" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta.get("started_at") or time.time())))
    add(f"- 目标地址：{meta.get('base_url', '')}")
    add(f"- API Key：{meta.get('api_key_masked', '')}")
    add(f"- 协议：{meta.get('protocol', '')}")
    add(f"- 模型：{', '.join(meta.get('models') or []) or '—'}")
    add(f"- 数据集：{meta.get('dataset', '')}（{meta.get('dataset_size', 0)} 条样本，覆盖不同输入长度）")
    add(f"- 单请求超时：{meta.get('timeout_s', '')} s")
    add("")

    add("## 一句话结论")
    add("")
    rec = decision.recommended_concurrency
    add(f"**{decision.verdict}**　推荐并发上限：**{rec if rec is not None else '无（无档位达标）'}**"
        f"　峰值输出吞吐：**{decision.peak_output_tps:.1f} token/s**")
    add("")

    if decision.findings:
        add("### 关键发现")
        add("")
        for f in decision.findings:
            add(f"- {f}")
        add("")

    if decision.advice:
        add("### 可执行建议")
        add("")
        for a in decision.advice:
            add(f"- {a}")
        add("")

    add("## 各并发档位指标")
    add("")
    add("| 并发 | 请求 | 成功率 | RPS | 输出吞吐 token/s | TTFT p50 | TTFT p95 | TTFT p99 | TPOT p50 | 总延迟 p95 |")
    add("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for st in stats:
        add("| {c} | {n} | {sr} | {rps} | {otps} | {t50} | {t95} | {t99} | {tp} | {tot} |".format(
            c=st.concurrency,
            n=st.requests,
            sr=_pct(st.success_rate),
            rps=_fmt(st.rps),
            otps=_fmt(st.output_tps),
            t50=_fmt(st.ttft.get("p50"), " ms", 0),
            t95=_fmt(st.ttft.get("p95"), " ms", 0),
            t99=_fmt(st.ttft.get("p99"), " ms", 0),
            tp=_fmt(st.tpot.get("p50"), " ms", 2),
            tot=_fmt(st.total.get("p95"), " ms", 0),
        ))
    add("")

    sat = decision.saturation or {}
    add("## 容量拐点")
    add("")
    if sat.get("saturation"):
        s = sat["saturation"]
        add(f"- 饱和起点：并发 **{s['concurrency']}**")
        add(f"- 判定依据：{s['reason']}")
    else:
        usable = sat.get("usable_stages", 0)
        if usable < 2:
            add(f"- 可用档位只有 {usable} 档（成功率 ≥95%），不足以比较吞吐增幅，无法判断拐点。")
        else:
            add("- 各档之间吞吐增幅都高于饱和阈值：加并发仍在换来吞吐，本次尚未见到拐点。")
    if sat.get("peak"):
        add(f"- 峰值档位：并发 {sat['peak']['concurrency']}，输出吞吐 {sat['peak']['output_tps']:.1f} token/s")
    add(f"- 可用档位：{sat.get('usable_stages', 0)}/{sat.get('total_stages', len(stats))}（成功率 ≥95%）")
    add("")

    if drift_info:
        add("## 长稳漂移")
        add("")
        if drift_info.get("enough_data"):
            add(f"- 样本数：{drift_info['samples']}")
            add(f"- 前半段 p95：{drift_info['front_p95']} ms")
            add(f"- 后半段 p95：{drift_info['back_p95']} ms")
            add(f"- 漂移比：{drift_info['drift_ratio']}（阈值 {decision.slo['drift_ratio']}，"
                f"{'判定为漂移' if drift_info.get('drifting') else '未判定为漂移'}）")
        else:
            add(f"- 样本不足（{drift_info.get('samples', 0)} 条 < 20 条），无法判定漂移。")
        add("")

    if decision.error_mix:
        add("## 错误分布")
        add("")
        add("| 错误类型 | 次数 |")
        add("|---|---:|")
        for k, v in sorted(decision.error_mix.items(), key=lambda kv: -kv[1]):
            add(f"| {ERROR_LABELS.get(k, k)} | {v} |")
        add("")

    if decision.violations:
        add("## SLO 违规明细")
        add("")
        for v in decision.violations:
            add(f"- {v}")
        add("")

    cost = decision.cost
    add("## 成本")
    add("")
    add(f"- 输入/输出单价：{cost.get('price_in_per_m', 0)} / {cost.get('price_out_per_m', 0)} 美元每百万 token")
    add(f"- 总 token：{cost.get('total_tokens', 0)}")
    add(f"- 总花费：{cost.get('total_cost_usd', 0)} 美元")
    add(f"- 每百万 token 成本：{cost.get('cost_per_mtok_usd', 0)} 美元")
    add("")

    add("## 指标口径")
    add("")
    for name, desc in METRIC_GLOSSARY:
        add(f"- **{name}**：{desc}")
    add("")

    add("## 复现命令")
    add("")
    add(FENCE + "bash")
    add(meta.get("command", "llm-stress run --help"))
    add(FENCE)
    add("")

    if samples:
        add("## 附录：原始样本")
        add("")
        add(f"本次共采集 {len(samples)} 条请求样本，已写入 {run_id}.samples.jsonl，可据此复算以上任何数字。")
        add("")

    return "\n".join(lines)


def write_outputs(
    out_dir: Path,
    *,
    run_id: str,
    meta: dict[str, Any],
    stats: list[StageStats],
    decision: Decision,
    drift_info: dict[str, Any] | None = None,
    samples: list[RequestSample] | None = None,
) -> dict[str, str]:
    """落盘报告三件套，返回路径。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    samples = samples or []

    md = render_markdown(run_id=run_id, meta=meta, stats=stats, decision=decision,
                         drift_info=drift_info, samples=samples)
    md_path = out_dir / f"{run_id}.md"
    md_path.write_text(md, encoding="utf-8")

    payload = {
        "run_id": run_id,
        "meta": meta,
        "stages": [st.to_dict() for st in stats],
        "decision": decision.to_dict(),
        "drift": drift_info,
        "sample_count": len(samples),
    }
    json_path = out_dir / f"{run_id}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    samples_path = out_dir / f"{run_id}.samples.jsonl"
    with samples_path.open("w", encoding="utf-8") as fh:
        for s in samples:
            fh.write(json.dumps(s.to_dict(), ensure_ascii=False) + "\n")

    return {"markdown": str(md_path), "json": str(json_path), "samples": str(samples_path)}


def load_run(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
