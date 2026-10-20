# -*- coding: utf-8 -*-
"""协议适配：请求构造 + 流式（SSE）解析。

支持三种主流协议，都走流式：
  openai     POST {root}/v1/chat/completions      (data: {...} / data: [DONE])
  anthropic  POST {root}/v1/messages              (event: content_block_delta)
  gemini     POST {root}/v1beta/models/{m}:streamGenerateContent?alt=sse

三种协议都复用同一个计时口径：
  t0 = 发出请求前；首个"内容 token"到达时记 TTFT；流结束时记 total。
  空响应（200 但一个 token 都没有）判为 stream_broken，不参与延迟分位。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .metrics import (ERR_CONNECT, ERR_HTTP_4XX, ERR_HTTP_5XX, ERR_NONE,
                      ERR_RATE_LIMITED, ERR_STREAM_BROKEN, ERR_TIMEOUT)

# ---------------------------------------------------------------- 结果载体
@dataclass(slots=True)
class StreamOutcome:
    ok: bool
    status: int
    error_class: str = ERR_NONE
    error_detail: str = ""
    ttft_ms: float | None = None
    total_ms: float = 0.0
    in_tokens: int = 0
    out_tokens: int = 0
    out_tokens_source: str = "none"   # usage / estimated / none
    finish_reason: str = ""
    retry_count: int = 0
    model_echo: str = ""


# ---------------------------------------------------------------- 工具
def _root(base_url: str, suffix: str) -> str:
    """把 base_url 规整到 API 根，避免 /v1 重复或遗漏。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("base_url 为空")
    if "://" not in base:
        base = "https://" + base
    for tail in ("/v1beta", "/v1"):
        if base.endswith(tail):
            return base + suffix
    return base + "/v1" + suffix


def _est_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk * 0.65 + (len(text) - cjk) / 4.0) + 1


def _classify_status(status: int) -> str:
    if status == 429:
        return ERR_RATE_LIMITED
    if 400 <= status < 500:
        return ERR_HTTP_4XX
    if status >= 500:
        return ERR_HTTP_5XX
    return ERR_NONE


# ---------------------------------------------------------------- 协议实现
class OpenAIProtocol:
    name = "openai"

    def url(self, base_url: str, model: str) -> str:
        return _root(base_url, "/chat/completions")

    def headers(self, api_key: str) -> dict[str, str]:
        key = (api_key or "").strip()
        heads = {"Content-Type": "application/json"}
        # 空 key 不能拼成 "Bearer "：httpx 会判成非法 header 值，连失败原因都拿不到
        if key:
            heads["Authorization"] = f"Bearer {key}"
        return heads

    def body(self, model: str, prompt: str, max_tokens: int, stream_options: bool = True) -> dict[str, Any]:
        b: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "max_tokens": max_tokens,
        }
        if stream_options:
            b["stream_options"] = {"include_usage": True}
        return b

    def parse(self, data: dict[str, Any]) -> tuple[str, dict[str, int]]:
        """返回 (新增文本, usage 增量)。"""
        text = ""
        for ch in data.get("choices") or []:
            delta = ch.get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str):
                text += piece
        usage = data.get("usage") or {}
        u = {}
        if isinstance(usage, dict):
            if usage.get("prompt_tokens"):
                u["in"] = int(usage["prompt_tokens"])
            if usage.get("completion_tokens"):
                u["out"] = int(usage["completion_tokens"])
        finish = ""
        for ch in data.get("choices") or []:
            if ch.get("finish_reason"):
                finish = str(ch["finish_reason"])
        if finish:
            u["finish"] = finish  # type: ignore[assignment]
        return text, u  # type: ignore[return-value]

    def models_url(self, base_url: str) -> str:
        return _root(base_url, "/models")

    def models_headers(self, api_key: str) -> dict[str, str]:
        return self.headers(api_key)

    def models_extract(self, payload: Any) -> list[str]:
        if isinstance(payload, dict):
            data = payload.get("data") or payload.get("models") or []
            if isinstance(data, list):
                return [str(m.get("id") or m.get("name") or "") for m in data if isinstance(m, dict)]
        return []


class AnthropicProtocol:
    name = "anthropic"

    def url(self, base_url: str, model: str) -> str:
        return _root(base_url, "/messages")

    def headers(self, api_key: str) -> dict[str, str]:
        key = (api_key or "").strip()
        heads = {"anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        if key:
            heads["x-api-key"] = key
        return heads

    def body(self, model: str, prompt: str, max_tokens: int, stream_options: bool = True) -> dict[str, Any]:
        return {
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }

    def parse(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        etype = data.get("type")
        u: dict[str, Any] = {}
        if etype == "content_block_delta":
            d = data.get("delta") or {}
            text = d.get("text") or ""
            if text:
                return str(text), u
            return "", u
        if etype == "message_start":
            usage = (data.get("message") or {}).get("usage") or {}
            if usage.get("input_tokens"):
                u["in"] = int(usage["input_tokens"])
            return "", u
        if etype == "message_delta":
            usage = data.get("usage") or {}
            if usage.get("output_tokens"):
                u["out"] = int(usage["output_tokens"])
            stop = (data.get("delta") or {}).get("stop_reason")
            if stop:
                u["finish"] = str(stop)
            return "", u
        return "", u

    def models_url(self, base_url: str) -> str:
        return _root(base_url, "/models")

    def models_headers(self, api_key: str) -> dict[str, str]:
        return self.headers(api_key)

    def models_extract(self, payload: Any) -> list[str]:
        if isinstance(payload, dict):
            data = payload.get("data") or []
            if isinstance(data, list):
                return [str(m.get("id") or "") for m in data if isinstance(m, dict)]
        return []


class GeminiProtocol:
    name = "gemini"

    def _root(self, base_url: str) -> str:
        base = (base_url or "").strip().rstrip("/")
        if "://" not in base:
            base = "https://" + base
        for tail in ("/v1beta", "/v1"):
            if base.endswith(tail):
                return base
        return base + "/v1beta"

    def url(self, base_url: str, model: str) -> str:
        return f"{self._root(base_url)}/models/{model}:streamGenerateContent?alt=sse"

    def headers(self, api_key: str) -> dict[str, str]:
        key = (api_key or "").strip()
        heads = {"Content-Type": "application/json"}
        if key:
            heads["x-goog-api-key"] = key
        return heads

    def body(self, model: str, prompt: str, max_tokens: int, stream_options: bool = True) -> dict[str, Any]:
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }

    def parse(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        text = ""
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                if isinstance(part.get("text"), str):
                    text += part["text"]
        u: dict[str, Any] = {}
        meta = data.get("usageMetadata") or {}
        if meta.get("promptTokenCount"):
            u["in"] = int(meta["promptTokenCount"])
        if meta.get("candidatesTokenCount"):
            u["out"] = int(meta["candidatesTokenCount"])
        for cand in data.get("candidates") or []:
            fr = cand.get("finishReason")
            if fr:
                u["finish"] = str(fr)
        return text, u

    def models_url(self, base_url: str) -> str:
        return f"{self._root(base_url)}/models"

    def models_headers(self, api_key: str) -> dict[str, str]:
        return self.headers(api_key)

    def models_extract(self, payload: Any) -> list[str]:
        if isinstance(payload, dict):
            data = payload.get("models") or []
            return [str(m.get("name") or "").split("/")[-1] for m in data if isinstance(m, dict)]
        return []


PROTOCOLS = {p.name: p for p in (OpenAIProtocol(), AnthropicProtocol(), GeminiProtocol())}


def get_protocol(name: str):
    if name not in PROTOCOLS:
        raise ValueError(f"不支持的协议 {name!r}，可选：{', '.join(PROTOCOLS)}")
    return PROTOCOLS[name]


# ---------------------------------------------------------------- 流式请求
async def stream_chat(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    protocol: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout_s: float,
) -> StreamOutcome:
    proto = get_protocol(protocol)
    url = proto.url(base_url, model)
    headers = proto.headers(api_key)

    async def attempt(use_stream_options: bool) -> StreamOutcome:
        payload = proto.body(model, prompt, max_tokens, use_stream_options)
        ttft: float | None = None
        chars = 0
        out_usage = 0
        in_usage = 0
        finish = ""
        t0 = time.perf_counter()
        try:
            async with client.stream(
                "POST", url, headers=headers, json=payload,
                timeout=httpx.Timeout(timeout_s, connect=min(15.0, timeout_s)),
            ) as resp:
                status = resp.status_code
                if status != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")[:600]
                    return StreamOutcome(
                        ok=False, status=status, error_class=_classify_status(status),
                        error_detail=body, total_ms=(time.perf_counter() - t0) * 1000,
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    piece, usage = proto.parse(data)
                    if piece:
                        if ttft is None:
                            ttft = (time.perf_counter() - t0) * 1000
                        chars += len(piece)
                    if usage.get("in"):
                        in_usage = int(usage["in"])
                    if usage.get("out"):
                        out_usage = int(usage["out"])
                    if usage.get("finish"):
                        finish = str(usage["finish"])
                total_ms = (time.perf_counter() - t0) * 1000
                if ttft is None or chars == 0:
                    return StreamOutcome(
                        ok=False, status=status, error_class=ERR_STREAM_BROKEN,
                        error_detail="200 但没有任何内容 token", ttft_ms=ttft, total_ms=total_ms,
                    )
                return StreamOutcome(
                    ok=True, status=status, ttft_ms=ttft, total_ms=total_ms,
                    in_tokens=in_usage or _est_tokens(prompt),
                    out_tokens=out_usage or max(1, int(chars * 0.65)),
                    out_tokens_source="usage" if out_usage else "estimated",
                    finish_reason=finish,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return StreamOutcome(ok=False, status=0, error_class=ERR_CONNECT,
                                 error_detail=f"{type(exc).__name__}: {exc}",
                                 total_ms=(time.perf_counter() - t0) * 1000)
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            return StreamOutcome(ok=False, status=0, error_class=ERR_TIMEOUT,
                                 error_detail=f"{type(exc).__name__}: {exc}",
                                 ttft_ms=ttft, total_ms=(time.perf_counter() - t0) * 1000)
        except Exception as exc:
            return StreamOutcome(ok=False, status=0, error_class=ERR_CONNECT,
                                 error_detail=f"{type(exc).__name__}: {exc}",
                                 ttft_ms=ttft, total_ms=(time.perf_counter() - t0) * 1000)

    first = await attempt(True)
    # 仅一种自动重试：上游不认识 stream_options（拿不到 usage）时，去掉该字段重投一次
    if first.status == 400 and "stream_options" in first.error_detail:
        second = await attempt(False)
        second.retry_count = first.retry_count + 1
        return second
    return first


async def list_models(base_url: str, api_key: str, protocol: str = "openai") -> dict[str, Any]:
    proto = get_protocol(protocol)
    url = proto.models_url(base_url)
    # 同上：探测模型列表也要直连，不能被系统代理劫持
    try:
        async with httpx.AsyncClient(timeout=15.0, verify=False, trust_env=False) as client:
            resp = await client.get(url, headers=proto.models_headers(api_key))
            if resp.status_code >= 400:
                return {"models": [], "path_used": url,
                        "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            names = [n for n in proto.models_extract(resp.json()) if n]
            return {"models": sorted(set(names)), "path_used": url, "error": ""}
    except Exception as exc:
        return {"models": [], "path_used": url, "error": f"{type(exc).__name__}: {exc}"}
