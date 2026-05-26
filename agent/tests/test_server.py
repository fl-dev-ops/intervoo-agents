from __future__ import annotations

from server import server


def test_agent_server_memory_thresholds_are_configured() -> None:
    assert server._job_memory_warn_mb == 2048
    assert server._job_memory_limit_mb == 4096
