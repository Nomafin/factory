"""Tests for Docker cleanup on task completion.

Tests the cleanup_test_environments() function and its integration
with the orchestrator's task completion hook.
"""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from factory.docker_toolkit import cleanup_test_environments


# ── cleanup_test_environments unit tests ─────────────────────────────────


class TestCleanupTestEnvironments:
    """Unit tests for cleanup_test_environments()."""

    @patch("factory.docker_toolkit.subprocess.run")
    def test_finds_and_removes_test_containers(self, mock_run):
        """Should stop and remove each test container found."""
        # docker ps returns two container IDs
        mock_run.return_value = MagicMock(
            stdout="abc123\ndef456\n", returncode=0,
        )

        removed = cleanup_test_environments(task_id=42)

        assert removed == 2

        # First call: docker ps to list containers
        ps_call = mock_run.call_args_list[0]
        cmd = ps_call[0][0]
        assert "docker" in cmd
        assert "ps" in cmd
        assert "-aq" in cmd
        assert "label=factory.task-id=42" in " ".join(cmd)
        assert "label=factory.env-type=test" in " ".join(cmd)

        # Subsequent calls: stop + rm for each container
        assert mock_run.call_count == 5  # 1 ps + 2 stop + 2 rm
        stop_calls = [
            c for c in mock_run.call_args_list
            if "stop" in c[0][0]
        ]
        rm_calls = [
            c for c in mock_run.call_args_list
            if "rm" in c[0][0]
        ]
        assert len(stop_calls) == 2
        assert len(rm_calls) == 2

        # Verify correct container IDs
        assert "abc123" in stop_calls[0][0][0]
        assert "def456" in stop_calls[1][0][0]
        assert "abc123" in rm_calls[0][0][0]
        assert "def456" in rm_calls[1][0][0]

    @patch("factory.docker_toolkit.subprocess.run")
    def test_no_containers_found(self, mock_run):
        """Should return 0 when no containers match."""
        mock_run.return_value = MagicMock(stdout="\n", returncode=0)

        removed = cleanup_test_environments(task_id=42)

        assert removed == 0
        # Only the ps call, no stop/rm calls
        assert mock_run.call_count == 1

    @patch("factory.docker_toolkit.subprocess.run")
    def test_empty_output(self, mock_run):
        """Should handle completely empty docker ps output."""
        mock_run.return_value = MagicMock(stdout="", returncode=0)

        removed = cleanup_test_environments(task_id=42)

        assert removed == 0
        assert mock_run.call_count == 1

    @patch("factory.docker_toolkit.subprocess.run")
    def test_single_container(self, mock_run):
        """Should handle a single container correctly."""
        mock_run.return_value = MagicMock(
            stdout="container1\n", returncode=0,
        )

        removed = cleanup_test_environments(task_id=7)

        assert removed == 1
        assert mock_run.call_count == 3  # 1 ps + 1 stop + 1 rm

    @patch("factory.docker_toolkit.subprocess.run")
    def test_filters_by_task_id_and_test_type(self, mock_run):
        """Should filter containers by task-id AND env-type=test."""
        mock_run.return_value = MagicMock(stdout="", returncode=0)

        cleanup_test_environments(task_id=99)

        ps_cmd = mock_run.call_args_list[0][0][0]
        filters = [arg for arg in ps_cmd if arg.startswith("label=")]
        assert "label=factory.task-id=99" in filters
        assert "label=factory.env-type=test" in filters

    @patch("factory.docker_toolkit.subprocess.run")
    def test_docker_ps_failure_returns_zero(self, mock_run):
        """Should return 0 if docker ps fails (non-zero exit)."""
        mock_run.return_value = MagicMock(
            stdout="", stderr="error", returncode=1,
        )

        removed = cleanup_test_environments(task_id=42)

        assert removed == 0
        assert mock_run.call_count == 1

    @patch("factory.docker_toolkit.subprocess.run")
    def test_docker_ps_timeout(self, mock_run):
        """Should handle docker ps timeout gracefully."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=30)

        removed = cleanup_test_environments(task_id=42)

        assert removed == 0

    @patch("factory.docker_toolkit.subprocess.run")
    def test_docker_not_found(self, mock_run):
        """Should handle docker binary not found gracefully."""
        mock_run.side_effect = FileNotFoundError("docker not found")

        removed = cleanup_test_environments(task_id=42)

        assert removed == 0

    @patch("factory.docker_toolkit.subprocess.run")
    def test_container_stop_failure_continues(self, mock_run):
        """Should continue to next container if stop/rm fails for one."""
        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0)
            if "ps" in cmd:
                result.stdout = "good1\nbad1\ngood2\n"
            elif "stop" in cmd and "bad1" in cmd:
                raise subprocess.TimeoutExpired(cmd="docker", timeout=30)
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect

        removed = cleanup_test_environments(task_id=42)

        # good1 and good2 succeed, bad1 fails
        assert removed == 2

    @patch("factory.docker_toolkit.subprocess.run")
    def test_stop_and_rm_called_with_check_false(self, mock_run):
        """Stop and rm should use check=False (best-effort)."""
        mock_run.return_value = MagicMock(
            stdout="c1\n", returncode=0,
        )

        cleanup_test_environments(task_id=1)

        for call_args in mock_run.call_args_list[1:]:
            assert call_args[1].get("check") is False or "check" not in call_args[1]

    @patch("factory.docker_toolkit.subprocess.run")
    def test_returns_count_of_removed(self, mock_run):
        """Should return the exact count of successfully removed containers."""
        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0)
            if "ps" in cmd:
                result.stdout = "c1\nc2\nc3\n"
            elif "rm" in cmd and "c2" in cmd:
                # c2 rm fails with timeout
                raise subprocess.TimeoutExpired(cmd="docker", timeout=30)
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect

        removed = cleanup_test_environments(task_id=42)

        # c1 succeeds fully, c2 fails at rm (counted as failure), c3 succeeds
        assert removed == 2

    @patch("factory.docker_toolkit.subprocess.run")
    def test_timeout_is_set_on_all_subprocess_calls(self, mock_run):
        """All subprocess calls should have a timeout to prevent hangs."""
        mock_run.return_value = MagicMock(
            stdout="c1\n", returncode=0,
        )

        cleanup_test_environments(task_id=42)

        for call_args in mock_run.call_args_list:
            assert "timeout" in call_args[1], (
                f"Missing timeout in call: {call_args[0][0]}"
            )


# ── Preview environments should NOT be cleaned up ────────────────────────


class TestCleanupPreviewExclusion:
    """Verify that preview environments are not affected by task cleanup."""

    @patch("factory.docker_toolkit.subprocess.run")
    def test_only_filters_for_test_env_type(self, mock_run):
        """The docker ps filter should specifically request env-type=test."""
        mock_run.return_value = MagicMock(stdout="", returncode=0)

        cleanup_test_environments(task_id=42)

        ps_cmd = mock_run.call_args_list[0][0][0]
        # Verify the filter explicitly asks for test type
        filter_args = [
            ps_cmd[i + 1]
            for i, arg in enumerate(ps_cmd)
            if arg == "--filter"
        ]
        assert "label=factory.env-type=test" in filter_args
        # Ensure we're NOT filtering for preview
        assert "label=factory.env-type=preview" not in filter_args


# ── Orchestrator integration tests ───────────────────────────────────────


class TestOrchestratorDockerCleanup:
    """Test that the orchestrator calls Docker cleanup on task completion."""

    @patch("factory.orchestrator.cleanup_test_environments")
    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_cleanup_called_on_success(
        self, MockRunner, MockRepoMgr, mock_cleanup,
    ):
        """Docker cleanup should run when an agent completes successfully."""
        from factory.config import Config
        from factory.db import Database
        from factory.orchestrator import Orchestrator

        db = Database(":memory:")
        await db.initialize()

        config = Config()
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value

        # Simulate task completion callback
        orch._on_agent_complete(task_id=42, returncode=0, output="done")

        mock_cleanup.assert_called_once_with(42)

        await db.close()

    @patch("factory.orchestrator.cleanup_test_environments")
    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_cleanup_called_on_failure(
        self, MockRunner, MockRepoMgr, mock_cleanup,
    ):
        """Docker cleanup should run when an agent fails."""
        from factory.config import Config
        from factory.db import Database
        from factory.orchestrator import Orchestrator

        db = Database(":memory:")
        await db.initialize()

        config = Config()
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value

        orch._on_agent_complete(task_id=42, returncode=1, output="error")

        mock_cleanup.assert_called_once_with(42)

        await db.close()

    @patch("factory.orchestrator.cleanup_test_environments")
    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_cleanup_failure_does_not_block_completion(
        self, MockRunner, MockRepoMgr, mock_cleanup,
    ):
        """If Docker cleanup raises, the task should still complete normally."""
        from factory.config import Config
        from factory.db import Database
        from factory.orchestrator import Orchestrator

        db = Database(":memory:")
        await db.initialize()

        config = Config()
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value

        mock_cleanup.side_effect = RuntimeError("Docker daemon not running")

        # Should not raise — cleanup is best-effort
        orch._on_agent_complete(task_id=42, returncode=0, output="done")

        mock_cleanup.assert_called_once_with(42)

        await db.close()

    @patch("factory.orchestrator.cleanup_test_environments")
    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_cleanup_runs_before_handlers(
        self, MockRunner, MockRepoMgr, mock_cleanup,
    ):
        """Docker cleanup should run before success/failure handlers."""
        from factory.config import Config
        from factory.db import Database
        from factory.orchestrator import Orchestrator

        db = Database(":memory:")
        await db.initialize()

        config = Config()
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value

        call_order = []
        mock_cleanup.side_effect = lambda tid: call_order.append("cleanup")

        # Patch _handle_success to track call order
        original_handle_success = orch._handle_success

        async def tracked_success(*args, **kwargs):
            call_order.append("success")

        with patch.object(orch, "_handle_success", tracked_success):
            orch._on_agent_complete(task_id=42, returncode=0, output="done")

        # Cleanup should have been called synchronously
        assert "cleanup" in call_order

        await db.close()

    @patch("factory.orchestrator.cleanup_test_environments")
    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_cleanup_called_with_correct_task_id(
        self, MockRunner, MockRepoMgr, mock_cleanup,
    ):
        """Should pass the correct task_id to cleanup."""
        from factory.config import Config
        from factory.db import Database
        from factory.orchestrator import Orchestrator

        db = Database(":memory:")
        await db.initialize()

        config = Config()
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value

        orch._on_agent_complete(task_id=123, returncode=0, output="done")

        mock_cleanup.assert_called_once_with(123)

        await db.close()


# ── Logging tests ────────────────────────────────────────────────────────


class TestCleanupLogging:
    """Verify that cleanup actions are properly logged."""

    @patch("factory.docker_toolkit.subprocess.run")
    def test_logs_container_removal(self, mock_run, caplog):
        """Should log each container removal."""
        import logging

        mock_run.return_value = MagicMock(
            stdout="abc123\n", returncode=0,
        )

        with caplog.at_level(logging.INFO, logger="factory.docker_toolkit"):
            cleanup_test_environments(task_id=42)

        log_messages = caplog.text
        assert "Cleaning up test environments for task 42" in log_messages
        assert "abc123" in log_messages

    @patch("factory.docker_toolkit.subprocess.run")
    def test_logs_no_containers_found(self, mock_run, caplog):
        """Should log when no containers are found."""
        import logging

        mock_run.return_value = MagicMock(stdout="", returncode=0)

        with caplog.at_level(logging.INFO, logger="factory.docker_toolkit"):
            cleanup_test_environments(task_id=42)

        assert "No test containers found for task 42" in caplog.text

    @patch("factory.docker_toolkit.subprocess.run")
    def test_logs_cleanup_summary(self, mock_run, caplog):
        """Should log a summary of cleaned up containers."""
        import logging

        mock_run.return_value = MagicMock(
            stdout="c1\nc2\n", returncode=0,
        )

        with caplog.at_level(logging.INFO, logger="factory.docker_toolkit"):
            cleanup_test_environments(task_id=42)

        assert "Cleaned up 2/2 test container(s) for task 42" in caplog.text

    @patch("factory.docker_toolkit.subprocess.run")
    def test_logs_warning_on_docker_ps_failure(self, mock_run, caplog):
        """Should log a warning when docker ps fails."""
        import logging

        mock_run.return_value = MagicMock(
            stdout="", stderr="daemon error", returncode=1,
        )

        with caplog.at_level(logging.WARNING, logger="factory.docker_toolkit"):
            cleanup_test_environments(task_id=42)

        assert "docker ps failed" in caplog.text

    @patch("factory.docker_toolkit.subprocess.run")
    def test_logs_warning_on_container_failure(self, mock_run, caplog):
        """Should log a warning for each failed container removal."""
        import logging

        def side_effect(cmd, **kwargs):
            if "ps" in cmd:
                return MagicMock(stdout="bad1\n", returncode=0)
            raise subprocess.TimeoutExpired(cmd="docker", timeout=30)

        mock_run.side_effect = side_effect

        with caplog.at_level(logging.WARNING, logger="factory.docker_toolkit"):
            cleanup_test_environments(task_id=42)

        assert "Failed to remove container bad1" in caplog.text
