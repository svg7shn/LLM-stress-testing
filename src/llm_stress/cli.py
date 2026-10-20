# -*- coding: utf-8 -*-
"""命令行入口：run（单模型压测）、compare（多模型横评）、models（分类看模型）、
report（看历史结果）、datasets / serve / version。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__
from .catalog import classify, discover
from .compare import run_compare
from .config import DEFAULT_DATASET, DEFAULT_PROTOCOL, DEFAULT_SCENE, DEFAULT_TIMEOUT_S, reports_dir
from .datasets import describe, resolve
from .report import load_run
from .runner import run_scenario
from .scenarios import SCENARIO_KINDS, Scenario, build

app = typer.Typer(add_completion=False, help="LLM 压测：把并发、延迟、RPS、TTFT 与吞吐变成可复现的结论。")
console = Console()


def _split_models(raw: str) -> list[str]:
    models = [m.strip() for m in raw.replace("，", ",").split(",") if m.strip()]
    if not models:
        raise typer.BadParameter("至少给一个模型名，例如 --model gpt-4o-mini")
    return models


@app.command("run")
def cmd_run(
    base_url: str = typer.Option(..., "--base-url", "-u", help="接口根地址，例如 https://api.openai.com"),
    api_key: str = typer.Option("", "--api-key", "-k", help="留空则读环境变量 LST_API_KEY"),
    model: str = typer.Option(..., "--model", "-m", help="模型名，多个用逗号分隔"),
    protocol: str = typer.Option(DEFAULT_PROTOCOL, "--protocol", "-p", help="openai / anthropic / gemini"),
    scene: str = typer.Option(DEFAULT_SCENE, "--scene", "-s", help="baseline / ladder / soak"),
    dataset: str = typer.Option(DEFAULT_DATASET, "--dataset", "-d", help="short / medium / long / mixed 或 JSONL 路径"),
    concurrency: int = typer.Option(4, "--concurrency", "-c", help="baseline/soak 的并发；ladder 的终点并发用它"),
    requests: int = typer.Option(20, "--requests", "-n", help="baseline 每档请求数"),
    requests_per_stage: int = typer.Option(30, "--requests-per-stage", help="ladder 每档请求数"),
    start: int = typer.Option(1, "--start", help="ladder 起始并发"),
    stop: int = typer.Option(0, "--stop", help="ladder 终止并发，0 表示用 --concurrency"),
    step: float = typer.Option(2.0, "--step", help="ladder 并发倍数"),
    duration: float = typer.Option(600.0, "--duration", help="soak 持续秒数"),
    timeout: float = typer.Option(DEFAULT_TIMEOUT_S, "--timeout", help="单请求超时秒数"),
    price_in: float = typer.Option(0.0, "--price-in", help="每百万输入 token 价格（美元）"),
    price_out: float = typer.Option(0.0, "--price-out", help="每百万输出 token 价格（美元）"),
    out: Path = typer.Option(None, "--out", help="报告输出目录，默认项目 reports/"),
    command_hint: str = typer.Option("", "--command-hint", help="写进报告的复现命令（Web 端自动填）"),
) -> None:
    """执行一次压测并按场景落盘报告。"""
    key = api_key or os.environ.get("LST_API_KEY", "")
    if not key:
        raise typer.BadParameter("缺少 API Key：用 --api-key 或环境变量 LST_API_KEY")
    models = _split_models(model)
    if scene not in SCENARIO_KINDS:
        raise typer.BadParameter(f"--scene 只能是 {', '.join(SCENARIO_KINDS)}")
    try:
        samples = resolve(dataset)
    except Exception as exc:
        raise typer.BadParameter(f"数据集加载失败：{exc}")

    sc = build(
        scene, requests=requests, start=start,
        stop=stop or concurrency, step=step,
        requests_per_stage=requests_per_stage,
        concurrency=concurrency, duration_s=duration,
    )

    console.print(f"[bold]场景[/] {scene}  [bold]档位[/] {[s.concurrency for s in sc.stages]}  "
                  f"[bold]数据集[/] {dataset}（{len(samples)} 条）  [bold]模型[/] {', '.join(models)}")

    last: dict[str, float | str] = {}

    async def on_progress(ev: dict) -> None:
        if ev.get("planned"):
            done = f"{ev['sent']}/{ev['planned']}"
        else:
            done = f"{ev['sent']} / {ev.get('elapsed_s', 0):.0f}s"
        line = (f"  并发 {ev['concurrency']:>3} | {done:>12} | 成功 {ev['ok']} 失败 {ev['fail']} "
                f"| rps {ev['rps']:.2f} | ttft_p50 {ev.get('ttft_p50') or '-'} ms")
        console.print(line, end="\r")

    async def on_stage_done(ev: dict) -> None:
        st = ev["stage"]
        console.print(" " * 110, end="\r")
        console.print(
            f"[green]✓[/] 并发 {st['concurrency']:>3} | 成功率 {st['success_rate'] * 100:5.1f}% "
            f"| 吞吐 {st['output_tps']:8.1f} token/s | TTFT p95 {st['ttft'].get('p95') or '-'} ms "
            f"| 总延迟 p95 {st['total'].get('p95') or '-'} ms"
        )

    result = asyncio.run(run_scenario(
        base_url=base_url, api_key=key, protocol=protocol, models=models,
        dataset=samples, dataset_label=dataset, scenario=sc, timeout_s=timeout,
        price_in_per_m=price_in, price_out_per_m=price_out,
        out_dir=out, on_progress=on_progress, on_stage_done=on_stage_done,
        command_hint=command_hint,
    ))

    d = result.decision
    console.print()
    console.print(f"[bold]结论[/] {d.verdict}　推荐并发 {d.recommended_concurrency}　"
                  f"峰值吞吐 {d.peak_output_tps:.1f} token/s")
    for f in d.findings:
        console.print(f"  · {f}")
    for a in d.advice:
        console.print(f"  → {a}")
    console.print()
    console.print(f"报告：{result.files['markdown']}")
    console.print(f"数据：{result.files['json']}")
    console.print(f"样本：{result.files['samples']}")


@app.command("compare")
def cmd_compare(
    base_url: str = typer.Option(..., "--base-url", "-u", help="接口根地址"),
    api_key: str = typer.Option("", "--api-key", "-k", help="留空则读环境变量 LST_API_KEY"),
    model: str = typer.Option("", "--model", "-m", help="逗号分隔；留空则自动发现对话模型"),
    all_models: bool = typer.Option(False, "--all-models", help="自动发现目标站的对话模型并逐个横评"),
    limit_models: int = typer.Option(8, "--limit-models", help="自动发现时最多测几个模型"),
    protocol: str = typer.Option(DEFAULT_PROTOCOL, "--protocol", "-p", help="openai / anthropic / gemini"),
    scene: str = typer.Option(DEFAULT_SCENE, "--scene", "-s", help="baseline / ladder / soak"),
    dataset: str = typer.Option(DEFAULT_DATASET, "--dataset", "-d", help="short / medium / long / mixed 或 JSONL 路径"),
    concurrency: int = typer.Option(4, "--concurrency", "-c", help="baseline/soak 的并发；ladder 的终点并发"),
    requests: int = typer.Option(20, "--requests", "-n", help="baseline 每档请求数"),
    requests_per_stage: int = typer.Option(30, "--requests-per-stage", help="ladder 每档请求数"),
    start: int = typer.Option(1, "--start", help="ladder 起始并发"),
    stop: int = typer.Option(0, "--stop", help="ladder 终止并发，0 表示用 --concurrency"),
    step: float = typer.Option(2.0, "--step", help="ladder 并发倍数"),
    duration: float = typer.Option(600.0, "--duration", help="soak 持续秒数"),
    timeout: float = typer.Option(DEFAULT_TIMEOUT_S, "--timeout", help="单请求超时秒数"),
    price_in: float = typer.Option(0.0, "--price-in", help="每百万输入 token 价格（美元）"),
    price_out: float = typer.Option(0.0, "--price-out", help="每百万输出 token 价格（美元）"),
    out: Path = typer.Option(None, "--out", help="报告输出目录，默认项目 reports/"),
) -> None:
    """多个模型跑同一套场景，产出横向对比报告（逐模型串行，互不干扰）。"""
    key = api_key or os.environ.get("LST_API_KEY", "")
    if not key:
        raise typer.BadParameter("缺少 API Key：用 --api-key 或环境变量 LST_API_KEY")
    if scene not in SCENARIO_KINDS:
        raise typer.BadParameter(f"--scene 只能是 {', '.join(SCENARIO_KINDS)}")
    try:
        samples = resolve(dataset)
    except Exception as exc:
        raise typer.BadParameter(f"数据集加载失败：{exc}")

    infos: dict[str, dict] = {}
    if all_models or not model.strip():
        found = asyncio.run(discover(base_url, key, protocol, timeout_s=timeout))
        if not found["names"]:
            console.print(f"[red]没取到模型列表[/] {found['error'] or '未知原因'}")
            raise typer.Exit(code=1)
        models = found["chat_names"][: max(1, limit_models)]
        infos = {m["name"]: m for m in found["models"]}
        if not models:
            console.print("[red]列表里没有对话模型[/]，用 --model 显式指定要测的模型")
            raise typer.Exit(code=1)
        console.print(
            f"[bold]{found['counts']['total']}[/] 个模型，对话 {found['chat_count']} 个；"
            f"本次横评 {len(models)} 个（--limit-models 可调）"
        )
    else:
        models = _split_models(model)
        infos = {m: classify(m).to_dict() for m in models}

    def factory(_model: str) -> Scenario:
        return build(
            scene, requests=requests, start=start, stop=stop or concurrency, step=step,
            requests_per_stage=requests_per_stage, concurrency=concurrency, duration_s=duration,
        )

    preview = factory(models[0])
    planned = preview.total_planned()
    estimate = f"，预计 {planned * len(models)} 次请求" if planned else ""
    console.print(
        f"[bold]场景[/] {scene}  [bold]档位[/] {[s.concurrency for s in preview.stages]}  "
        f"[bold]数据集[/] {dataset}（{len(samples)} 条）{estimate}"
    )

    async def on_progress(ev: dict) -> None:
        kind = ev.get("kind")
        if kind == "model_start":
            console.print(f"\n[bold cyan]▶ {escape(str(ev['model']))}[/] （{ev['index'] + 1}/{ev['total']}）")
            return
        if kind == "model_done":
            name = escape(str(ev.get("model")))
            if ev.get("ok"):
                console.print(
                    f"[green]■ {name}[/] 结论 {ev.get('verdict')}　"
                    f"推荐并发 {ev.get('recommended_concurrency')}　"
                    f"峰值 {ev.get('peak_output_tps')} token/s"
                )
            else:
                console.print(f"[red]■ {name} 失败[/] {ev.get('error')}")
            return
        if ev.get("planned"):
            done = f"{ev['sent']}/{ev['planned']}"
        else:
            done = f"{ev['sent']} / {ev.get('elapsed_s', 0):.0f}s"
        console.print(
            f"  并发 {ev['concurrency']:>3} | {done:>12} | 成功 {ev['ok']} 失败 {ev['fail']} "
            f"| rps {ev['rps']:.2f} | ttft_p50 {ev.get('ttft_p50') or '-'} ms",
            end="\r",
        )

    async def on_stage_done(ev: dict) -> None:
        st = ev["stage"]
        console.print(" " * 110, end="\r")
        console.print(
            f"  ✓ 并发 {st['concurrency']:>3} | 成功率 {st['success_rate'] * 100:5.1f}% "
            f"| 吞吐 {st['output_tps']:8.1f} token/s | TTFT p95 {st['ttft'].get('p95') or '-'} ms"
        )

    result = asyncio.run(run_compare(
        base_url=base_url, api_key=key, protocol=protocol, models=models,
        dataset=samples, dataset_label=dataset, scenario_factory=factory, infos=infos,
        timeout_s=timeout, price_in_per_m=price_in, price_out_per_m=price_out,
        out_dir=out, on_progress=on_progress, on_stage_done=on_stage_done,
    ))

    table = Table(title=f"横评排名 {result.run_id}")
    for col in ("排名", "模型", "结论", "推荐并发", "峰值tok/s", "TTFT p95", "成功率", "成本/Mtok"):
        table.add_column(col, justify="left" if col == "模型" else "right")
    for rank, entry in enumerate(result.ranked(), 1):
        data = entry.to_dict()
        if entry.result is None:
            table.add_row(str(rank), entry.model, "未完成", "-", "-", "-", "-", "-")
            continue
        success = data.get("success_rate")
        cost = data.get("cost_per_mtok")
        table.add_row(
            str(rank),
            entry.model,
            str(data.get("verdict")),
            str(data.get("recommended_concurrency")),
            f"{float(data.get('peak_output_tps') or 0):.1f}",
            f"{data.get('ttft_p95')}" if data.get("ttft_p95") is not None else "-",
            f"{success * 100:.1f}%" if success is not None else "-",
            f"{cost:.4f}" if cost else "-",
        )
    console.print()
    console.print(table)
    console.print(f"报告：{result.files['markdown']}")
    console.print(f"数据：{result.files['json']}")


@app.command("models")
def cmd_models(
    base_url: str = typer.Option(..., "--base-url", "-u"),
    api_key: str = typer.Option("", "--api-key", "-k"),
    protocol: str = typer.Option(DEFAULT_PROTOCOL, "--protocol", "-p"),
    chat_only: bool = typer.Option(False, "--chat-only", help="只列可以压测的对话模型"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
    timeout: float = typer.Option(15.0, "--timeout", help="拉取模型列表的超时秒数"),
) -> None:
    """列出目标地址可用的模型，并标注类型与厂商。"""
    key = api_key or os.environ.get("LST_API_KEY", "")
    found = asyncio.run(discover(base_url, key, protocol, timeout_s=timeout))
    if as_json:
        console.print_json(data=found)
        if not found["names"]:
            raise typer.Exit(code=1)
        return
    if not found["names"]:
        console.print(
            f"[red]失败[/] {found['error'] or '没取到模型'}"
            f"（试过：{', '.join(found['tried']) or '-'}）"
        )
        raise typer.Exit(code=1)
    counts = found["counts"]
    console.print(
        f"[bold]{counts['total']}[/] 个模型，其中对话 {counts['chat']} 个，路径 {found['path_used']}"
    )
    table = Table()
    table.add_column("模型")
    table.add_column("类型")
    table.add_column("厂商")
    for item in found["models"]:
        if chat_only and not item["chat"]:
            continue
        table.add_row(item["name"], item["type_label"], item["family_label"])
    console.print(table)
    if chat_only:
        console.print(f"（--chat-only：已过滤 {counts['total'] - counts['chat']} 个非对话模型）")


@app.command("report")
def cmd_report(
    file: Path = typer.Argument(..., help="result.json 或 report.md 路径"),
    top: int = typer.Option(10, "--top", help="只列前 N 档"),
) -> None:
    """查看一次历史压测的核心结论。"""
    path = Path(file)
    if path.suffix.lower() == ".md":
        console.print(path.read_text(encoding="utf-8"))
        return
    data = load_run(path)
    d = data.get("decision", {})
    console.print(f"[bold]{data.get('run_id')}[/] 场景 {data.get('meta', {}).get('scene')}")
    console.print(f"结论：{d.get('verdict')}　推荐并发 {d.get('recommended_concurrency')}　"
                  f"峰值吞吐 {d.get('peak_output_tps')} token/s")
    table = Table(title="各档指标")
    for col in ("并发", "请求", "成功率", "RPS", "吞吐token/s", "TTFT p95", "总延迟 p95"):
        table.add_column(col, justify="right")
    for st in data.get("stages", [])[:top]:
        table.add_row(
            str(st["concurrency"]), str(st["requests"]),
            f"{st['success_rate'] * 100:.1f}%", f"{st['rps']:.2f}",
            f"{st['output_tps']:.1f}",
            f"{st['ttft'].get('p95') or '-'}", f"{st['total'].get('p95') or '-'}",
        )
    console.print(table)


@app.command("datasets")
def cmd_datasets() -> None:
    """列出内置数据集。"""
    for item in describe():
        console.print(f"[bold]{item['name']}[/]  {item['samples']} 条  "
                      f"平均 {item['avg_input_chars']} 字  ≈{item['est_input_tokens']} token")


@app.command("serve")
def cmd_serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8021, "--port"),
    open_browser: bool = typer.Option(False, "--open", help="启动后自动打开浏览器"),
) -> None:
    """启动 Web 界面（FastAPI + 内置静态页）。"""
    import uvicorn

    url = f"http://{host}:{port}"
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    console.print(f"[bold]Web 界面[/] {url}    关闭本窗口即停止服务")
    uvicorn.run("llm_stress.web.server:app", host=host, port=port, reload=False)


@app.command("version")
def cmd_version() -> None:
    console.print(f"llm-stress {__version__}")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    app()
