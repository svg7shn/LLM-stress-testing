# -*- coding: utf-8 -*-
"""端到端集成测试：起一个本地假上游，真实跑完"压测 → 聚合 → 决策 → 报告"。

不联网、不花钱，验证的是：并发真的开出来了（假上游记录到的峰值并发）、
报告三件套真的落了盘、429 真的被归因成限流。
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "tests" / "mock_api.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(base: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(base + "/__stats", timeout=1.0, trust_env=False).status_code == 200:
                return True
        except Exception:
            time.sleep(0.15)
    return False


def _spawn(port: int, limit: int, ttft: int, token: int, tokens: int, log: Path):
    fh = open(log, "wb")
    proc = subprocess.Popen(
        [sys.executable, str(MOCK), "--port", str(port),
         "--concurrency-limit", str(limit), "--ttft-ms", str(ttft),
         "--token-ms", str(token), "--tokens", str(tokens)],
        cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
    )
    return proc, fh


def _start_or_fail(proc, fh, base: str, log: Path) -> str:
    if _wait_ready(base):
        return base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    fh.close()
    tail = log.read_text(encoding="utf-8", errors="replace")[-3000:]
    raise RuntimeError(f"假上游启动失败 base={base} exit={proc.returncode}\n--- 子进程日志 ---\n{tail}")


@pytest.fixture(scope="module")
def mock_upstream(tmp_path_factory):
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log = tmp_path_factory.mktemp("mockapi") / "mock.log"
    proc, fh = _spawn(port, 8, 20, 5, 10, log)
    try:
        _start_or_fail(proc, fh, base, log)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        fh.close()


@pytest.fixture(scope="module")
def tight_upstream(tmp_path_factory):
    """并发上限 1 的假上游，用来验证 429 归因。"""
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log = tmp_path_factory.mktemp("mockapi-tight") / "mock.log"
    proc, fh = _spawn(port, 1, 30, 8, 15, log)
    try:
        _start_or_fail(proc, fh, base, log)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        fh.close()


async def test_baseline_end_to_end(mock_upstream, tmp_path):
    from llm_stress.datasets import resolve
    from llm_stress.runner import run_scenario
    from llm_stress.scenarios import baseline

    httpx.post(mock_upstream + "/__reset", timeout=5.0, trust_env=False)
    result = await run_scenario(
        base_url=mock_upstream, api_key="test-key", protocol="openai",
        models=["mock-llm-fast"], dataset=resolve("short"),
        scenario=baseline(requests=4), out_dir=tmp_path, run_id="it-baseline",
    )

    assert result.aborted is False
    assert len(result.stats) == 1
    st = result.stats[0]
    assert st.requests == 4
    assert st.ok == 4
    assert st.success_rate == 1.0
    assert st.ttft["p50"] is not None and st.ttft["p50"] > 0
    assert st.output_tps > 0
    assert st.total_tokens > 0
    assert len(result.samples) == 4

    for key in ("markdown", "json", "samples"):
        assert Path(result.files[key]).exists(), key
    payload = json.loads(Path(result.files["json"]).read_text(encoding="utf-8"))
    assert payload["run_id"] == "it-baseline"
    assert payload["decision"]["verdict"]
    assert payload["sample_count"] == 4

    # 单并发：假上游观察到的峰值并发必须为 1，证明确实是串行基线
    stats = httpx.get(mock_upstream + "/__stats", timeout=5.0, trust_env=False).json()
    assert stats["requests"] == 4
    assert stats["peak_in_flight"] == 1


async def test_rate_limit_attribution(tight_upstream, tmp_path):
    from llm_stress.datasets import resolve
    from llm_stress.runner import run_scenario
    from llm_stress.scenarios import ladder

    httpx.post(tight_upstream + "/__reset", timeout=5.0, trust_env=False)
    result = await run_scenario(
        base_url=tight_upstream, api_key="test-key", protocol="openai",
        models=["mock-llm-fast"], dataset=resolve("short"),
        scenario=ladder(start=1, stop=2, step=2.0, requests_per_stage=4),
        out_dir=tmp_path, run_id="it-limit",
    )
    total_429 = sum(st.rate_limited for st in result.stats)
    assert total_429 > 0, "并发上限 1 时并发 2 档应当出现 429"
    assert any("429" in a or "限流" in a for a in result.decision.advice)
    assert result.decision.error_mix.get("rate_limited", 0) == total_429


async def test_models_endpoint(mock_upstream):
    from llm_stress.transport import list_models

    res = await list_models(mock_upstream, "test-key", "openai")
    assert res["error"] == ""
    assert "mock-llm-fast" in res["models"]
