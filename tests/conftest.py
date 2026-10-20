# -*- coding: utf-8 -*-
"""测试夹具：把 src 加进 sys.path（不依赖 pip install -e），并提供样本/指标构造器。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from llm_stress.metrics import RequestSample, StageStats  # noqa: E402


@pytest.fixture
def make_sample():
    def _make(*, stage_index: int = 0, concurrency: int = 1, ok: bool = True,
              ttft_ms: float = 100.0, total_ms: float = 1000.0, out_tokens: int = 10,
              in_tokens: int = 20, error_class: str = "none", status: int = 200,
              started_at: float = 0.0, model: str = "mock", cost_usd: float = 0.0,
              out_tokens_source: str = "usage") -> RequestSample:
        return RequestSample(
            run_id="test-run",
            stage_index=stage_index,
            concurrency=concurrency,
            model=model,
            protocol="openai",
            sample_id=f"s{stage_index}-{started_at}",
            started_at=started_at,
            finished_at=started_at + total_ms / 1000.0,
            ok=ok,
            status=status,
            error_class=error_class,
            ttft_ms=ttft_ms if ok else None,
            total_ms=total_ms,
            in_tokens=in_tokens,
            out_tokens=out_tokens,
            out_tokens_source=out_tokens_source,
            cost_usd=cost_usd,
        )
    return _make


@pytest.fixture
def make_stats():
    def _make(*, concurrency: int = 1, stage_index: int = 0, ttft_p95: float = 500.0,
              total_p95: float = 5000.0, success_rate: float = 1.0,
              output_tps: float = 100.0, requests: int = 10, ok: int | None = None,
              errors: dict[str, int] | None = None, total_tokens: int = 1000,
              cost_usd: float = 0.0, rate_limited: int | None = None) -> StageStats:
        ok_n = requests if ok is None else ok
        errors = errors or {}
        return StageStats(
            stage_index=stage_index,
            concurrency=concurrency,
            requests=requests,
            ok=ok_n,
            failed=requests - ok_n,
            success_rate=success_rate,
            window_s=1.0,
            rps=float(ok_n),
            offered_rps=float(requests),
            output_tps=output_tps,
            input_tps=output_tps / 2,
            ttft={"p50": ttft_p95 * 0.6, "p90": ttft_p95 * 0.9, "p95": ttft_p95, "p99": ttft_p95 * 1.2},
            total={"p50": total_p95 * 0.6, "p90": total_p95 * 0.9, "p95": total_p95, "p99": total_p95 * 1.2},
            tpot={"p50": 20.0, "p90": 25.0, "p95": 30.0, "p99": 35.0},
            output_tps_p50=output_tps,
            statuses={"200": ok_n} if ok_n else {},
            errors=errors,
            rate_limited=int(errors.get("rate_limited", 0) if rate_limited is None else rate_limited),
            avg_out_tokens=100.0,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            started_at=0.0,
            finished_at=1.0,
        )
    return _make

# ---- 共享的本地假上游（session 级）----
# catalog / compare 这类测试需要一个真实在听的 HTTP 服务，但不该每个用例重起一次。
import socket as _socket
import subprocess as _subprocess
import time as _time

import httpx as _httpx

_MOCK_SCRIPT = ROOT / "tests" / "mock_api.py"


def _free_port() -> int:
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def mock_base(tmp_path_factory):
    """起一个本地假上游，返回 base_url；用例结束自动关掉。"""
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log = tmp_path_factory.mktemp("mock-shared") / "mock.log"
    fh = open(log, "wb")
    proc = _subprocess.Popen(
        [sys.executable, str(_MOCK_SCRIPT), "--port", str(port),
         "--concurrency-limit", "6", "--ttft-ms", "15", "--token-ms", "4", "--tokens", "8"],
        cwd=str(ROOT), stdout=fh, stderr=_subprocess.STDOUT,
    )
    try:
        deadline = _time.time() + 25.0
        ready = False
        while _time.time() < deadline:
            try:
                if _httpx.get(base + "/__stats", timeout=1.0, trust_env=False).status_code == 200:
                    ready = True
                    break
            except Exception:
                _time.sleep(0.15)
        if not ready:
            tail = log.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"假上游未就绪 base={base}\n--- 子进程日志 ---\n{tail}")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except _subprocess.TimeoutExpired:
            proc.kill()
        fh.close()

