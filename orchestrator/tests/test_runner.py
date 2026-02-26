from unittest.mock import MagicMock
from pathlib import Path

from factory.runner import AgentRunner


async def test_agent_runner_lifecycle():
    runner = AgentRunner(max_concurrent=2)
    assert runner.available_slots == 2
    assert runner.running_count == 0


async def test_agent_runner_concurrency_limit():
    runner = AgentRunner(max_concurrent=1)
    runner._running[1] = MagicMock()
    assert runner.available_slots == 0
    assert runner.can_accept_task is False


async def test_build_claude_command():
    runner = AgentRunner(max_concurrent=2)
    cmd = runner._build_command(
        prompt="Fix the login bug",
        workdir=Path("/tmp/worktree"),
        allowed_tools=["Read", "Edit", "Bash"],
    )
    assert "-p" in cmd
    assert "Fix the login bug" in cmd
    assert "--output-format" in cmd
    assert "stream-json" in cmd
    assert "--allowedTools" in cmd
