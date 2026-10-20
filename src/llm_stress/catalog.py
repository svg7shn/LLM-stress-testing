# -*- coding: utf-8 -*-
"""模型目录：把上游模型名分类，并尽量一次性拉全。

为什么压测前要先分类
--------------------
中转站（New API / one-api 这类网关）的 /v1/models 往往把对话、向量、图像、
语音、重排混在同一个列表里返回。如果拿这些名字无差别地发 /chat/completions，
非对话模型的失败会把成功率与错误构成一起拉花，最后得出"这个站不行"的结论，
真实原因却是"测错了对象"。

这里做两件事：
  1. classify()：按名字推断类型与厂商家族，纯启发式、可解释，可被显式 --model 覆盖；
  2. discover()：先按协议自带的模型列表地址拉，失败再依次试网关常见路径，
     并同时带三种鉴权头（不同网关认的 header 不同）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from .transport import list_models

TYPE_CHAT = "chat"
TYPE_EMBEDDING = "embedding"
TYPE_IMAGE = "image"
TYPE_AUDIO = "audio"
TYPE_VIDEO = "video"
TYPE_RERANK = "rerank"
TYPE_MODERATION = "moderation"

TYPE_LABELS: dict[str, str] = {
    TYPE_CHAT: "对话",
    TYPE_EMBEDDING: "向量嵌入",
    TYPE_IMAGE: "图像生成",
    TYPE_AUDIO: "语音音频",
    TYPE_VIDEO: "视频生成",
    TYPE_RERANK: "重排序",
    TYPE_MODERATION: "内容审核",
}

# 顺序敏感：先判 rerank 再判 embedding —— bge-reranker 同时含 "bge-" 前缀，
# 先匹配 embedding 会把重排模型误判成向量模型，压测时发对话请求全挂。
_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (TYPE_RERANK, ("rerank",)),
    (TYPE_EMBEDDING, ("embedding", "text-embed", "bge-", "gte-", "e5-", "m3e-", "jina-embed", "-embed")),
    (TYPE_MODERATION, ("moderation", "guard")),
    (TYPE_IMAGE, (
        "dall-e", "dalle", "gpt-image", "midjourney", "mj-", "sdxl", "stable-diffusion",
        "seedream", "flux", "imagen", "cogview", "kolors", "ideogram", "recraft", "wanx", "sd-",
    )),
    (TYPE_AUDIO, (
        "whisper", "tts-", "-tts", "text-to-speech", "speech", "transcribe",
        "realtime", "voice", "audio", "suno", "musicgen",
    )),
    (TYPE_VIDEO, ("sora", "veo", "runway", "kling", "pika", "luma", "hailuo", "t2v", "i2v", "video")),
)

# 家族只用于展示与分组，不参与判定。
_FAMILY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claude", ("claude",)),
    ("gemini", ("gemini", "gemma", "learnlm")),
    ("openai", ("gpt-", "gpt4", "gpt3", "gpt5", "chatgpt", "davinci", "o1-", "o3-", "o4-", "codex")),
    ("deepseek", ("deepseek",)),
    ("qwen", ("qwen", "qwq", "tongyi")),
    ("kimi", ("kimi", "moonshot")),
    ("doubao", ("doubao", "ark-")),
    ("glm", ("glm", "chatglm", "zhipu")),
    ("minimax", ("minimax", "abab")),
    ("llama", ("llama",)),
    ("mistral", ("mistral", "mixtral", "codestral", "pixtral")),
    ("grok", ("grok",)),
    ("hunyuan", ("hunyuan",)),
    ("ernie", ("ernie",)),
    ("spark", ("spark", "xinghuo")),
    ("yi", ("yi-",)),
    ("baichuan", ("baichuan",)),
    ("step", ("step-",)),
)

FAMILY_LABELS: dict[str, str] = {
    "claude": "Anthropic",
    "gemini": "Google",
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "qwen": "阿里通义",
    "kimi": "月之暗面",
    "doubao": "字节豆包",
    "glm": "智谱",
    "minimax": "MiniMax",
    "llama": "Meta",
    "mistral": "Mistral",
    "grok": "xAI",
    "hunyuan": "腾讯混元",
    "ernie": "百度文心",
    "spark": "讯飞星火",
    "yi": "零一万物",
    "baichuan": "百川",
    "step": "阶跃星辰",
    "other": "其他",
}

# 网关常见的模型列表路径，按命中概率排序。
FALLBACK_PATHS: tuple[str, ...] = (
    "/v1/models",
    "/v1beta/models",
    "/models",
    "/v1/responses/models",
)


@dataclass(slots=True)
class ModelInfo:
    name: str
    type: str = TYPE_CHAT
    family: str = "other"

    @property
    def chat(self) -> bool:
        return self.type == TYPE_CHAT

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type)

    @property
    def family_label(self) -> str:
        return FAMILY_LABELS.get(self.family, self.family)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "type_label": self.type_label,
            "family": self.family,
            "family_label": self.family_label,
            "chat": self.chat,
        }


def classify(name: str) -> ModelInfo:
    """按名字推断类型与家族。规则是子串匹配，顺序即优先级。"""
    raw = (name or "").strip()
    low = raw.lower()
    mtype = TYPE_CHAT
    for kind, keys in _TYPE_RULES:
        if any(k in low for k in keys):
            mtype = kind
            break
    family = "other"
    for fam, keys in _FAMILY_RULES:
        if any(k in low for k in keys):
            family = fam
            break
    return ModelInfo(name=raw, type=mtype, family=family)


def chat_models(infos: Iterable[ModelInfo]) -> list[ModelInfo]:
    """只留对话模型，保持原顺序。"""
    return [i for i in infos if i.chat]


def summarize(infos: Iterable[ModelInfo]) -> dict[str, Any]:
    """统计各类型数量，用于发现结果的一行摘要。"""
    items = list(infos)
    by_type: dict[str, int] = {}
    by_family: dict[str, int] = {}
    for info in items:
        by_type[info.type] = by_type.get(info.type, 0) + 1
        by_family[info.family] = by_family.get(info.family, 0) + 1
    return {
        "total": len(items),
        "by_type": by_type,
        "by_family": by_family,
        "chat": sum(1 for i in items if i.chat),
    }


def _root_url(base_url: str) -> str:
    """把用户填的地址削成站根：补协议、去接口后缀、去重斜杠。"""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return ""
    low = url.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        url = "https://" + url
        low = url.lower()
    for suffix in (
        "/v1/chat/completions", "/v1/messages", "/v1/chat", "/chat/completions",
        "/v1/models", "/v1beta/models", "/v1beta", "/v1",
    ):
        if low.endswith(suffix):
            url = url[: -len(suffix)]
            low = url.lower()
            break
    return url.rstrip("/")


def _headers(api_key: str) -> dict[str, str]:
    """三种鉴权头一起带：不同网关分别认 Bearer / x-api-key / x-goog-api-key。

    没有 key 就一个鉴权头都不带——空值拼成 "Bearer " 带尾随空格，httpx 会直接
    判成非法 header 值（LocalProtocolError），连上游返回的状态码都看不到。
    """
    key = (api_key or "").strip()
    heads = {"Accept": "application/json"}
    if key:
        heads["Authorization"] = f"Bearer {key}"
        heads["x-api-key"] = key
        heads["x-goog-api-key"] = key
    return heads


def _clip(text: Any, limit: int = 120) -> str:
    """把上游返回的错误正文压成一行短提示：JSON 全文塞进 UI 会糊成一片。"""
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def _upstream_hint(text: Any) -> str:
    """从上游错误体里挑出给人看的那句话（中转站通常放在 error.message）。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(obj, dict):
        return ""
    err = obj.get("error")
    if isinstance(err, dict):
        return _clip(err.get("message") or err.get("code") or err.get("type") or "")
    if isinstance(err, str):
        return _clip(err)
    if isinstance(obj.get("message"), str):
        return _clip(obj["message"])
    return ""


def _path_of(url: str) -> str:
    """把完整 URL 压成路径，错误提示里没必要重复整条地址。"""
    u = str(url or "")
    rest = u.split("://", 1)[-1]
    return "/" + rest.split("/", 1)[-1] if "/" in rest else u


def _clean_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if name.lower().startswith("models/"):
        name = name[len("models/"):]
    return name


def _names_from(payload: Any) -> list[str]:
    """兼容 {"data": [...]}、{"models": [...]} 与裸数组三种形状，并去重保序。"""
    items: list[Any] = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("data", "models", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
        else:
            models = payload.get("models")
            if isinstance(models, dict):
                items = list(models.values())
    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            name = _clean_name(item)
        elif isinstance(item, dict):
            name = ""
            for key in ("id", "name", "model"):
                name = _clean_name(item.get(key))
                if name:
                    break
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    return names


def _pack(names: list[str], path_used: str, error: str, tried: list[str]) -> dict[str, Any]:
    infos = [classify(n) for n in names]
    counts = summarize(infos)
    return {
        "models": [i.to_dict() for i in infos],
        "names": [i.name for i in infos],
        "chat_names": [i.name for i in infos if i.chat],
        "counts": counts,
        "chat_count": counts["chat"],
        "path_used": path_used,
        "error": error,
        "tried": tried,
    }


async def discover(
    base_url: str,
    api_key: str,
    protocol: str = "openai",
    *,
    timeout_s: float = 15.0,
    follow_fallbacks: bool = True,
) -> dict[str, Any]:
    """拉模型列表并分类。

    先走协议自带的模型地址（transport 已按协议拼好 URL 与鉴权头），
    没结果再依次试网关常见路径；返回结构固定，调用方不用关心走了哪条路。
    """
    tried: list[str] = []
    first = await list_models(base_url, api_key, protocol)
    path_used = str(first.get("path_used") or "")
    if path_used:
        tried.append(path_used)
    names = [_clean_name(str(x)) for x in (first.get("models") or [])]
    names = [n for n in names if n]
    error = str(first.get("error") or "")

    root = _root_url(base_url)
    if names or not follow_fallbacks or not root:
        return _pack(names, path_used, error, tried)

    headers = _headers(api_key)
    # 每条路径记 (路径, 状态码, 上游原话)：最后拼成一句人话，而不是罗列四条 JSON
    failures: list[tuple[str, int, str]] = []
    hint = _upstream_hint(error)
    if error:
        code_match = re.search(r"HTTP (\d{3})", error)
        failures.append((_path_of(path_used), int(code_match.group(1)) if code_match else 0, hint))
    # trust_env=False：与 transport.list_models 保持一致，探测模型列表必须直连，
    # 否则系统代理会把请求劫持到别处，误判成"站点没有模型接口"。
    async with httpx.AsyncClient(
        timeout=timeout_s, follow_redirects=True, verify=False, trust_env=False
    ) as client:
        for path in FALLBACK_PATHS:
            url = root + path
            if url in tried:
                continue
            tried.append(url)
            try:
                resp = await client.get(url, headers=headers)
            except Exception as exc:  # 网络层错误：记下来继续试下一个路径
                error = f"{type(exc).__name__}: {exc}"
                continue
            if resp.status_code >= 400:
                upstream_hint = _upstream_hint(resp.text)
                failures.append((path, resp.status_code, upstream_hint))
                hint = hint or upstream_hint
                error = error or f"HTTP {resp.status_code} @ {path}"
                continue
            try:
                payload = resp.json()
            except Exception:
                error = f"响应不是 JSON @ {path}"
                continue
            names = _names_from(payload)
            if names:
                return _pack(names, url, "", tried)
            error = f"模型列表为空 @ {path}"

    # 只报最后一条路径的错容易误导（用户会以为必须访问那个地址），
    # 这里把每条路径的状态码集合起来，再附上上游自己给的那句错误。
    if failures:
        marks = "、".join(f"{p} {code}" if code else p for p, code, _ in failures)
        error = f"模型接口都没返回列表（{marks}）" + (f"：{hint}" if hint else "")
    return _pack([], path_used, error or "未取到模型列表", tried)
