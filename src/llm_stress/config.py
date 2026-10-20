# -*- coding: utf-8 -*-
"""运行期常量与路径。刻意不做多层配置合并，避免"到底生效的是哪一份"这类排查成本。"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_TIMEOUT_S = 120.0
DEFAULT_DATASET = "mixed"
DEFAULT_PROTOCOL = "openai"
DEFAULT_SCENE = "ladder"
EMIT_INTERVAL_S = 0.25

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def reports_dir() -> Path:
    """报告目录：LST_REPORTS_DIR 环境变量优先，否则项目根下的 reports/。"""
    env = os.environ.get("LST_REPORTS_DIR", "").strip()
    out = Path(env) if env else PROJECT_ROOT / "reports"
    out.mkdir(parents=True, exist_ok=True)
    return out


def mask_key(key: str) -> str:
    """脱敏：保留前 6 位与后 4 位，中间打码。太短则整体打码。"""
    if not key:
        return ""
    if len(key) <= 12:
        return key[:2] + "*" * 6
    return f"{key[:6]}{'*' * 6}{key[-4:]}"
