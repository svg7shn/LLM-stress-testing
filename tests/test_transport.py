# -*- coding: utf-8 -*-
"""传输层测试：SSE 解析、状态分类、空响应识别、stream_options 兼容重试。

全部走 httpx.MockTransport，不发真实网络请求。
"""
from __future__ import annotations

import json

import httpx
import pytest

from llm_stress.transport import get_protocol, list_models, stream_chat
from llm_stress.metrics import ERR_RATE_LIMITED, ERR_STREAM_BROKEN

SSE_OK = (
    'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}' + "\n\n"
    + 'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"世界"},"finish_reason":null}]}' + "\n\n"
    + 'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
      '"usage":{"prompt_tokens":7,"completion_tokens":5,"total_tokens":12}}' + "\n\n"
    + "data: [DONE]" + "\n\n"
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_stream_parses_usage_and_ttft():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=SSE_OK)

    async with _client(handler) as client:
        out = await stream_chat(client, base_url="http://mock/v1", api_key="k",
                                protocol="openai", model="m", prompt="hi",
                                max_tokens=32, timeout_s=10)
    assert out.ok is True
    assert out.status == 200
    assert out.ttft_ms is not None and out.ttft_ms >= 0
    assert out.in_tokens == 7
    assert out.out_tokens == 5
    assert out.out_tokens_source == "usage"
    assert out.finish_reason == "stop"
    assert out.total_ms >= out.ttft_ms


async def test_rate_limit_classified():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    async with _client(handler) as client:
        out = await stream_chat(client, base_url="http://mock", api_key="k",
                                protocol="openai", model="m", prompt="hi",
                                max_tokens=8, timeout_s=10)
    assert out.ok is False
    assert out.status == 429
    assert out.error_class == ERR_RATE_LIMITED


async def test_empty_stream_flagged_broken():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="data: [DONE]\n\n")

    async with _client(handler) as client:
        out = await stream_chat(client, base_url="http://mock", api_key="k",
                                protocol="openai", model="m", prompt="hi",
                                max_tokens=8, timeout_s=10)
    assert out.ok is False
    assert out.error_class == ERR_STREAM_BROKEN


async def test_retry_without_stream_options_on_400():
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        seen.append(payload)
        if "stream_options" in payload:
            return httpx.Response(400, text="unknown field: stream_options")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=SSE_OK)

    async with _client(handler) as client:
        out = await stream_chat(client, base_url="http://mock", api_key="k",
                                protocol="openai", model="m", prompt="hi",
                                max_tokens=8, timeout_s=10)
    assert len(seen) == 2
    assert "stream_options" in seen[0]
    assert "stream_options" not in seen[1]
    assert out.ok is True
    assert out.retry_count == 1


def test_protocol_urls():
    assert get_protocol("openai").url("http://h", "m") == "http://h/v1/chat/completions"
    assert get_protocol("anthropic").url("https://h/v1", "m") == "https://h/v1/messages"
    g = get_protocol("gemini").url("https://h", "gemini-2.0-flash")
    assert "/v1beta/models/gemini-2.0-flash:streamGenerateContent" in g
    with pytest.raises(ValueError):
        get_protocol("nope")


async def test_list_models_extracts_ids(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/models")
        return httpx.Response(200, json={"data": [{"id": "b-model"}, {"id": "a-model"}]})

    transport = httpx.MockTransport(handler)
    # 先抓住真类：llm_stress.transport.httpx 就是全局 httpx 模块，
    # 直接 patch 后若在 factory 里再引用 httpx.AsyncClient 会调用到自己 → 递归。
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("llm_stress.transport.httpx.AsyncClient", factory)
    res = await list_models("http://mock", "k", "openai")
    assert res["models"] == ["a-model", "b-model"]
    assert res["error"] == ""
