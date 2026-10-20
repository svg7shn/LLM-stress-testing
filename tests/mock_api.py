# -*- coding: utf-8 -*-
"""本地假上游：不花钱、不联网，用来验证压测链路端到端是否真的在压。

支持 openai / anthropic / gemini 三种流式协议的 SSE 形状，
并可模拟并发上限（超限返回 429），方便验证限流归因是否正确。

启动：
    python tests/mock_api.py --port 8022
    python tests/mock_api.py --port 8022 --concurrency-limit 4 --ttft-ms 80 --token-ms 15 --tokens 40
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="mock upstream", version="1.0.0")

STATE: dict[str, Any] = {
    "in_flight": 0,
    "peak_in_flight": 0,
    "requests": 0,
    "rejected": 0,
    "ttft_ms": 80.0,
    "token_ms": 15.0,
    "tokens": 40,
    "concurrency_limit": 8,
    "error_rate": 0.0,
}

MODEL_IDS = ["mock-llm-fast", "mock-llm-slow"]


def _sse(payload: Any, event: str | None = None) -> str:
    body = f"data: {json.dumps(payload, ensure_ascii=False)}" + "\n\n"
    if event:
        return f"event: {event}\n" + body
    return body


def _enter() -> bool:
    limit = int(STATE["concurrency_limit"])
    STATE["requests"] += 1
    if limit > 0 and STATE["in_flight"] >= limit:
        STATE["rejected"] += 1
        return False
    STATE["in_flight"] += 1
    STATE["peak_in_flight"] = max(STATE["peak_in_flight"], STATE["in_flight"])
    return True


def _leave() -> None:
    STATE["in_flight"] = max(0, STATE["in_flight"] - 1)


def _token_text(i: int) -> str:
    # 固定长度、可预测的假文本，便于核对 token 数与字符数
    return f"tok{i:02d} "


async def _emit_openai(model: str, prompt_tokens: int) -> Any:
    created = int(time.time())
    yield _sse({
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    })
    await asyncio.sleep(STATE["ttft_ms"] / 1000.0)
    n = int(STATE["tokens"])
    for i in range(n):
        yield _sse({
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": _token_text(i)}, "finish_reason": None}],
        })
        await asyncio.sleep(STATE["token_ms"] / 1000.0)
    yield _sse({
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": n,
            "total_tokens": prompt_tokens + n,
        },
    })
    yield "data: [DONE]\n\n"


async def _emit_anthropic(model: str, prompt_tokens: int) -> Any:
    yield _sse({
        "type": "message_start",
        "message": {
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "usage": {"input_tokens": prompt_tokens, "output_tokens": 1},
        },
    }, "message_start")
    await asyncio.sleep(STATE["ttft_ms"] / 1000.0)
    n = int(STATE["tokens"])
    for i in range(n):
        yield _sse({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": _token_text(i)},
        }, "content_block_delta")
        await asyncio.sleep(STATE["token_ms"] / 1000.0)
    yield _sse({"type": "content_block_stop", "index": 0}, "content_block_stop")
    yield _sse({
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": n},
    }, "message_delta")
    yield _sse({"type": "message_stop"}, "message_stop")


async def _emit_gemini(model: str, prompt_tokens: int) -> Any:
    await asyncio.sleep(STATE["ttft_ms"] / 1000.0)
    n = int(STATE["tokens"])
    for i in range(n):
        yield _sse({
            "candidates": [{
                "content": {"parts": [{"text": _token_text(i)}], "role": "model"},
                "index": 0,
            }],
        })
        await asyncio.sleep(STATE["token_ms"] / 1000.0)
    yield _sse({
        "candidates": [{
            "content": {"parts": [{"text": ""}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": n,
            "totalTokenCount": prompt_tokens + n,
        },
    })


def _too_many() -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"error": {"message": "mock: 并发已达上限", "type": "rate_limit_error"}},
        headers={"retry-after": "1"},
    )


@app.get("/v1/models")
@app.get("/v1beta/models")
async def models() -> Any:
    return {"data": [{"id": m, "object": "model"} for m in MODEL_IDS]}


@app.get("/__stats")
async def stats() -> Any:
    return dict(STATE)


@app.post("/__reset")
async def reset() -> Any:
    STATE.update({"in_flight": 0, "peak_in_flight": 0, "requests": 0, "rejected": 0})
    return {"ok": True}


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Any:
    if not _enter():
        return _too_many()
    try:
        payload = await request.json()
        model = str(payload.get("model") or MODEL_IDS[0])
        prompt = json.dumps(payload.get("messages") or [], ensure_ascii=False)
        prompt_tokens = max(1, len(prompt) // 4)
        return StreamingResponse(
            _wrap(_emit_openai(model, prompt_tokens)),
            media_type="text/event-stream",
        )
    except Exception:
        _leave()
        raise


@app.post("/v1/messages")
async def messages(request: Request) -> Any:
    if not _enter():
        return _too_many()
    try:
        payload = await request.json()
        model = str(payload.get("model") or MODEL_IDS[0])
        prompt = json.dumps(payload.get("messages") or [], ensure_ascii=False)
        prompt_tokens = max(1, len(prompt) // 4)
        return StreamingResponse(
            _wrap(_emit_anthropic(model, prompt_tokens)),
            media_type="text/event-stream",
        )
    except Exception:
        _leave()
        raise


@app.post("/v1beta/models/{model}:streamGenerateContent")
async def gemini_stream(model: str, request: Request) -> Any:
    if not _enter():
        return _too_many()
    try:
        payload = await request.json()
        prompt = json.dumps(payload.get("contents") or [], ensure_ascii=False)
        prompt_tokens = max(1, len(prompt) // 4)
        return StreamingResponse(
            _wrap(_emit_gemini(model, prompt_tokens)),
            media_type="text/event-stream",
        )
    except Exception:
        _leave()
        raise


async def _wrap(gen: Any) -> Any:
    try:
        async for chunk in gen:
            yield chunk
    finally:
        _leave()


def main() -> None:
    ap = argparse.ArgumentParser(description="本地假 LLM 上游（压测验证用）")
    ap.add_argument("--port", type=int, default=8022)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--concurrency-limit", type=int, default=8, help="0 表示不限")
    ap.add_argument("--ttft-ms", type=float, default=80.0)
    ap.add_argument("--token-ms", type=float, default=15.0)
    ap.add_argument("--tokens", type=int, default=40)
    ap.add_argument("--error-rate", type=float, default=0.0, help="0~1，按概率返回 500")
    args = ap.parse_args()
    STATE.update({
        "concurrency_limit": args.concurrency_limit,
        "ttft_ms": args.ttft_ms,
        "token_ms": args.token_ms,
        "tokens": args.tokens,
        "error_rate": args.error_rate,
    })
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
