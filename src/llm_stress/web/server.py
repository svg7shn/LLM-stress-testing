# -*- coding: utf-8 -*-
"""Web 界面后端：一个进程内跑压测，SSE 推进度，报告落盘可回看。

依赖只有 fastapi + uvicorn（已在 requirements 中），不引入前端构建链。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..catalog import classify, discover
from ..compare import run_compare
from ..config import DEFAULT_DATASET, DEFAULT_PROTOCOL, DEFAULT_SCENE, reports_dir
from ..datasets import BUILTIN_NAMES, describe, resolve
from ..report import load_run
from ..runner import run_scenario
from ..scenarios import SCENARIO_KINDS, build

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="LLM Stress Testing", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class RunRequest(BaseModel):
    base_url: str
    api_key: str
    models: list[str] = Field(min_length=1)
    protocol: str = DEFAULT_PROTOCOL
    scene: str = DEFAULT_SCENE
    dataset: str = DEFAULT_DATASET
    concurrency: int = Field(4, ge=1, le=256)
    requests: int = Field(20, ge=1, le=5000)
    requests_per_stage: int = Field(30, ge=1, le=5000)
    start: int = Field(1, ge=1, le=256)
    step: float = Field(2.0, gt=1.0, le=8.0)
    duration_s: float = Field(600.0, ge=10.0, le=86400.0)
    timeout_s: float = Field(120.0, gt=0, le=600)
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0


class ModelsRequest(BaseModel):
    base_url: str
    api_key: str = ""
    protocol: str = DEFAULT_PROTOCOL


class CompareRequest(BaseModel):
    base_url: str
    api_key: str = ""
    models: list[str] = Field(default_factory=list)
    all_models: bool = True
    limit_models: int = Field(8, ge=1, le=32)
    protocol: str = DEFAULT_PROTOCOL
    scene: str = DEFAULT_SCENE
    dataset: str = DEFAULT_DATASET
    concurrency: int = Field(4, ge=1, le=256)
    requests: int = Field(20, ge=1, le=5000)
    requests_per_stage: int = Field(30, ge=1, le=5000)
    start: int = Field(1, ge=1, le=256)
    step: float = Field(2.0, gt=1.0, le=8.0)
    duration_s: float = Field(600.0, ge=10.0, le=86400.0)
    timeout_s: float = Field(120.0, gt=0, le=600)
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0


@dataclass
class TaskState:
    task_id: str
    status: str = "pending"      # pending / running / done / cancelled / error
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    result: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None
    drift: dict[str, Any] | None = None
    error: str = ""
    created_at: float = field(default_factory=time.time)


TASKS: dict[str, TaskState] = {}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/meta")
async def api_meta() -> JSONResponse:
    return JSONResponse({
        "scenes": list(SCENARIO_KINDS),
        "datasets": list(BUILTIN_NAMES),
        "dataset_info": describe(),
        "protocols": ["openai", "anthropic", "gemini"],
    })


@app.post("/api/models")
async def api_models(req: ModelsRequest) -> JSONResponse:
    # discover 返回 models（带类型/厂商的明细）、names、chat_names 与探测路径；
    # 键名与旧的 list_models 保持一致，前端可以平滑升级。
    info = await discover(req.base_url, req.api_key, req.protocol)
    return JSONResponse(info)


@app.post("/api/run")
async def api_run(req: RunRequest) -> JSONResponse:
    if req.scene not in SCENARIO_KINDS:
        raise HTTPException(400, f"未知场景 {req.scene}")
    try:
        dataset = resolve(req.dataset)
    except Exception as exc:
        raise HTTPException(400, f"数据集加载失败：{exc}")

    scene = build(
        req.scene, requests=req.requests, start=req.start, stop=req.concurrency,
        step=req.step, requests_per_stage=req.requests_per_stage,
        concurrency=req.concurrency, duration_s=req.duration_s,
    )
    task_id = uuid.uuid4().hex[:10]
    state = TaskState(task_id=task_id)
    TASKS[task_id] = state

    async def job() -> None:
        state.status = "running"
        try:
            async def on_progress(ev: dict) -> None:
                state.queue.put_nowait(ev)

            async def on_stage_done(ev: dict) -> None:
                state.queue.put_nowait(ev)

            result = await run_scenario(
                base_url=req.base_url, api_key=req.api_key, protocol=req.protocol,
                models=req.models, dataset=dataset, dataset_label=req.dataset, scenario=scene,
                timeout_s=req.timeout_s, price_in_per_m=req.price_in_per_m,
                price_out_per_m=req.price_out_per_m, on_progress=on_progress,
                on_stage_done=on_stage_done, cancel=state.cancel,
                command_hint=_hint(req, scene.kind),
            )
            state.result = result.summary()
            state.decision = result.decision.to_dict()
            state.meta = result.meta
            state.drift = result.drift
            state.status = "cancelled" if state.cancel.is_set() else "done"
            state.queue.put_nowait({
                "kind": "done", "summary": state.result, "status": state.status,
                "decision": state.decision, "meta": state.meta, "drift": state.drift,
            })
        except Exception as exc:  # 任务异常必须回传给前端，否则界面会一直转圈
            state.status = "error"
            state.error = f"{type(exc).__name__}: {exc}"
            state.queue.put_nowait({"kind": "error", "error": state.error})
        finally:
            state.queue.put_nowait(None)

    asyncio.create_task(job())
    return JSONResponse({"task_id": task_id, "stages": [s.concurrency for s in scene.stages]})


@app.post("/api/compare")
async def api_compare(req: CompareRequest) -> JSONResponse:
    if req.scene not in SCENARIO_KINDS:
        raise HTTPException(400, f"未知场景 {req.scene}")
    try:
        dataset = resolve(req.dataset)
    except Exception as exc:
        raise HTTPException(400, f"数据集加载失败：{exc}")

    def make_scene(_model: str):
        return build(
            req.scene, requests=req.requests, start=req.start, stop=req.concurrency,
            step=req.step, requests_per_stage=req.requests_per_stage,
            concurrency=req.concurrency, duration_s=req.duration_s,
        )

    task_id = uuid.uuid4().hex[:10]
    state = TaskState(task_id=task_id)
    TASKS[task_id] = state

    async def job() -> None:
        state.status = "running"
        try:
            models = [m.strip() for m in req.models if m and m.strip()]
            infos: dict[str, Any] = {}
            if req.all_models or not models:
                # 自动发现：只取对话模型，embedding/rerank 这些压不出 TTFT 意义
                found = await discover(req.base_url, req.api_key, req.protocol)
                for item in found.get("models") or []:
                    name = str(item.get("name") or "")
                    if name:
                        infos[name] = item
                picked = [str(x) for x in (found.get("chat_names") or [])]
                if req.all_models:
                    models = picked[: req.limit_models]
                elif not models:
                    models = picked[: req.limit_models]
            if not models:
                raise RuntimeError("没有可横评的对话模型：先拉取模型列表，或显式填写模型名")
            for name in models:  # 手工填的模型也要有类型/厂商
                if name not in infos:
                    infos[name] = classify(name).to_dict()

            async def on_progress(ev: dict) -> None:
                state.queue.put_nowait(ev)

            async def on_stage_done(ev: dict) -> None:
                state.queue.put_nowait(ev)

            result = await run_compare(
                base_url=req.base_url, api_key=req.api_key, protocol=req.protocol,
                models=models, dataset=dataset, dataset_label=req.dataset,
                scenario_factory=make_scene, infos=infos,
                timeout_s=req.timeout_s, price_in_per_m=req.price_in_per_m,
                price_out_per_m=req.price_out_per_m, on_progress=on_progress,
                on_stage_done=on_stage_done, cancel=state.cancel,
                command_hint=_hint_compare(req, req.scene, models),
            )
            payload = result.to_dict()
            state.result = payload
            state.status = "cancelled" if state.cancel.is_set() else "done"
            state.queue.put_nowait({
                "kind": "done", "mode": "compare", "status": state.status,
                "compare": payload,
            })
        except Exception as exc:
            state.status = "error"
            state.error = f"{type(exc).__name__}: {exc}"
            state.queue.put_nowait({"kind": "error", "error": state.error})
        finally:
            state.queue.put_nowait(None)

    asyncio.create_task(job())
    return JSONResponse({
        "task_id": task_id, "mode": "compare",
        "stages": [s.concurrency for s in make_scene("").stages],
    })


def _hint(req: RunRequest, scene: str) -> str:
    return (f"llm-stress run --base-url {req.base_url} --protocol {req.protocol} "
            f"--model {','.join(req.models)} --scene {scene} --dataset {req.dataset} "
            f"--concurrency {req.concurrency} --requests-per-stage {req.requests_per_stage} "
            f"--timeout {req.timeout_s}")


def _hint_compare(req: CompareRequest, scene: str, models: list[str]) -> str:
    return (f"llm-stress compare --base-url {req.base_url} --protocol {req.protocol} "
            f"--model {','.join(models)} --scene {scene} --dataset {req.dataset} "
            f"--requests-per-stage {req.requests_per_stage} --timeout {req.timeout_s}")


def _compare_head(data: dict[str, Any]) -> dict[str, Any]:
    """横评 JSON 的榜首条目：拿不到就返回空字典，让列表页显示 '-'。"""
    ranking = data.get("ranking") or []
    entries = data.get("entries") or []
    if not ranking:
        return {}
    for entry in entries:
        if entry.get("model") == ranking[0]:
            return entry
    return {}


@app.get("/api/run/{task_id}/events")
async def api_events(task_id: str) -> StreamingResponse:
    state = TASKS.get(task_id)
    if state is None:
        raise HTTPException(404, "任务不存在")

    async def gen():
        yield "retry: 3000\n\n"
        while True:
            try:
                item = await asyncio.wait_for(state.queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is None:
                break
            yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/run/{task_id}/cancel")
async def api_cancel(task_id: str) -> JSONResponse:
    state = TASKS.get(task_id)
    if state is None:
        raise HTTPException(404, "任务不存在")
    state.cancel.set()
    return JSONResponse({"cancelled": True})


@app.get("/api/run/{task_id}")
async def api_task(task_id: str) -> JSONResponse:
    state = TASKS.get(task_id)
    if state is None:
        raise HTTPException(404, "任务不存在")
    return JSONResponse({"task_id": task_id, "status": state.status,
                         "error": state.error, "result": state.result,
                         "decision": state.decision, "meta": state.meta,
                         "drift": state.drift})


@app.get("/api/reports")
async def api_reports() -> JSONResponse:
    root = reports_dir()
    items = []
    for p in sorted(root.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:100]:
        try:
            data = load_run(p)
        except Exception:
            continue
        if p.name.endswith(".compare.json"):
            head = _compare_head(data)
            items.append({
                "kind": "compare",
                "run_id": data.get("run_id"),
                "scene": data.get("scene"),
                "verdict": head.get("verdict"),
                "recommended_concurrency": head.get("recommended_concurrency"),
                "peak_output_tps": head.get("peak_output_tps"),
                "models": len(data.get("entries") or []),
                "champion": head.get("model"),
                "time": (data.get("meta") or {}).get("started_at"),
                "file": p.name,
            })
            continue
        d = data.get("decision", {}) or {}
        items.append({
            "kind": "run",
            "run_id": data.get("run_id"),
            "scene": (data.get("meta") or {}).get("scene"),
            "verdict": d.get("verdict"),
            "recommended_concurrency": d.get("recommended_concurrency"),
            "peak_output_tps": d.get("peak_output_tps"),
            "time": (data.get("meta") or {}).get("started_at"),
            "file": p.name,
        })
    return JSONResponse({"items": items})


@app.get("/api/reports/{name}")
async def api_report_detail(name: str) -> JSONResponse:
    root = reports_dir()
    if not name.endswith(".json") or "/" in name or "\\" in name:
        raise HTTPException(400, "非法文件名")
    path = root / name
    if not path.is_file():
        raise HTTPException(404, "报告不存在")
    data = load_run(path)
    md_path = path.with_suffix(".md")
    data["markdown"] = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
    return JSONResponse(data)


@app.get("/api/reports/{name}/markdown", response_class=PlainTextResponse)
async def api_report_md(name: str) -> PlainTextResponse:
    root = reports_dir()
    path = root / name
    if not path.is_file():
        raise HTTPException(404, "报告不存在")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/markdown")
