# -*- coding: utf-8 -*-
"""模型目录测试：分类规则、对话过滤、模型列表发现。

分类是启发式，这些用例锁定的是"规则顺序"这类容易被后人改坏的地方，
而不是替某个具体厂商背书。
"""
from __future__ import annotations

import pytest

from llm_stress.catalog import (
    TYPE_AUDIO,
    TYPE_CHAT,
    TYPE_EMBEDDING,
    TYPE_IMAGE,
    TYPE_MODERATION,
    TYPE_RERANK,
    TYPE_VIDEO,
    ModelInfo,
    _names_from,
    _root_url,
    chat_models,
    classify,
    discover,
    summarize,
)


@pytest.mark.parametrize(
    "name, expected_type, expected_family",
    [
        ("claude-sonnet-4-6", TYPE_CHAT, "claude"),
        ("gpt-4o-mini", TYPE_CHAT, "openai"),
        ("o3-mini", TYPE_CHAT, "openai"),
        ("gemini-2.5-pro", TYPE_CHAT, "gemini"),
        ("deepseek-chat", TYPE_CHAT, "deepseek"),
        ("qwen-max", TYPE_CHAT, "qwen"),
        ("kimi-k2", TYPE_CHAT, "kimi"),
        ("glm-4-plus", TYPE_CHAT, "glm"),
        ("llama-3.1-70b-instruct", TYPE_CHAT, "llama"),
        ("grok-4", TYPE_CHAT, "grok"),
        ("step-2-16k", TYPE_CHAT, "step"),
        ("text-embedding-3-large", TYPE_EMBEDDING, "other"),
        ("bge-m3", TYPE_EMBEDDING, "other"),
        ("bge-reranker-v2-m3", TYPE_RERANK, "other"),
        ("dall-e-3", TYPE_IMAGE, "other"),
        ("gpt-image-1", TYPE_IMAGE, "openai"),
        ("whisper-1", TYPE_AUDIO, "other"),
        ("gpt-4o-realtime-preview", TYPE_AUDIO, "openai"),
        ("sora-2", TYPE_VIDEO, "other"),
        ("text-moderation-latest", TYPE_MODERATION, "other"),
    ],
)
def test_classify(name, expected_type, expected_family):
    info = classify(name)
    assert info.type == expected_type
    assert info.family == expected_family
    assert info.chat is (expected_type == TYPE_CHAT)


def test_rerank_wins_over_embedding():
    """bge- 同时是向量前缀：顺序写反会把重排模型误判成向量模型。"""
    assert classify("bge-reranker-v2-m3").type == TYPE_RERANK
    assert classify("bge-large-zh").type == TYPE_EMBEDDING


def test_classify_blank_name():
    info = classify("   ")
    assert info.name == ""
    assert info.type == TYPE_CHAT


def test_model_info_to_dict_and_labels():
    info = classify("claude-sonnet-4-6")
    payload = info.to_dict()
    assert payload["name"] == "claude-sonnet-4-6"
    assert payload["type_label"] == "对话"
    assert payload["family_label"] == "Anthropic"
    assert payload["chat"] is True
    assert set(payload) == {"name", "type", "type_label", "family", "family_label", "chat"}


def test_chat_models_filters_and_keeps_order():
    infos = [classify(n) for n in ("gpt-4o", "text-embedding-3-small", "claude-3-5-haiku", "sora-2")]
    kept = [i.name for i in chat_models(infos)]
    assert kept == ["gpt-4o", "claude-3-5-haiku"]


def test_summarize_counts():
    infos = [classify(n) for n in ("gpt-4o", "claude-sonnet-4-6", "text-embedding-3-large")]
    stats = summarize(infos)
    assert stats["total"] == 3
    assert stats["chat"] == 2
    assert stats["by_type"][TYPE_CHAT] == 2
    assert stats["by_type"][TYPE_EMBEDDING] == 1
    assert stats["by_family"]["openai"] == 1


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://api.example.com/v1/chat/completions", "https://api.example.com"),
        ("https://api.example.com/v1/messages", "https://api.example.com"),
        ("https://api.example.com/v1/models", "https://api.example.com"),
        ("https://api.example.com/v1beta/models", "https://api.example.com"),
        ("api.example.com/v1", "https://api.example.com"),
        ("https://api.example.com/", "https://api.example.com"),
        ("", ""),
    ],
)
def test_root_url(raw, expected):
    assert _root_url(raw) == expected


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"data": [{"id": "a"}, {"id": "b"}]}, ["a", "b"]),
        ({"models": [{"name": "c"}, "d"]}, ["c", "d"]),
        ({"models": {"m1": {"name": "e"}}}, ["e"]),
        (["models/x", "x", "y"], ["x", "y"]),
        ({"data": [{"id": "models/gemini-2.5-pro"}]}, ["gemini-2.5-pro"]),
        ({"data": []}, []),
        ("not-a-payload", []),
    ],
)
def test_names_from_shapes(payload, expected):
    assert _names_from(payload) == expected


async def test_discover_openai_protocol(mock_base):
    found = await discover(mock_base, "test-key", "openai")
    assert found["error"] == ""
    assert "mock-llm-fast" in found["names"]
    assert found["path_used"].endswith("/v1/models")
    assert found["counts"]["total"] == len(found["names"])
    assert found["chat_count"] == len(found["names"])
    assert all(item["chat"] for item in found["models"])
    assert found["models"][0]["type_label"] == "对话"


async def test_discover_gemini_protocol_falls_back(mock_base):
    """gemini 协议的列表解析只认 {"models":[{"name":...}]}。

    假上游的 /v1beta/models 返回的是 OpenAI 形状（{"data":[{"id":...}]}），
    所以按协议解析会得到空列表，此时必须靠兜底路径把名字捞回来 ——
    真网关也确实存在"协议对不上、但 /v1/models 能用"的情况。
    """
    found = await discover(mock_base, "test-key", "gemini")
    assert found["error"] == ""
    assert "mock-llm-slow" in found["names"]
    assert found["chat_count"] >= 1
    assert found["path_used"].endswith("/v1/models")
    assert len(found["tried"]) >= 2  # 先试过协议路径，才有兜底


async def test_discover_unreachable_reports_error():
    found = await discover("http://127.0.0.1:1", "k", "openai", timeout_s=1.0)
    assert found["names"] == []
    assert found["error"]
    assert found["tried"]
