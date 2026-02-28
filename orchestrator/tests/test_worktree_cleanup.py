"""Tests for worktree and branch cleanup on task failure/cancellation.

Tests the RepoManager.cleanup_task_worktree() method, the convenience
wrapper cleanup_task_worktree(), and the orchestrator integration that
triggers cleanup on failed/cancelled tasks.
"""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from factory.workspace import RepoManager, cleanup_task_worktree


# ── Helpers ──────────────────────────────────────────────────────────────


def _setup_git_repo(tmp_path: Path) -> tuple[RepoManager, Path, Path]:
    """Create a bare origin, clone it, and return a RepoManager plus paths."""
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        check=True, capture_output=True,
    )

    # Seed with an initial commit
    temp_work = tmp_path / "temp_work"
    subprocess.run(
        ["git", "clone", str(origin), str(temp_work)],
        check=True, capture_output=True,
    )
    (temp_work / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=str(temp_work), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@test.com",
         "commit", "-m", "init"],
        cwd=str(temp_work), check=True, capture_output=True,
    )
    subprocess.run(["git", "push"], cwd=str(temp_work), check=True, capture_output=True)

    repos_dir = tmp_path / "repos"
    worktrees_dir = tmp_path / "worktrees"
    repos_dir.mkdir()
    worktrees_dir.mkdir()

    mgr = RepoManager(repos_dir=repos_dir, worktrees_dir=worktrees_dir)
    return mgr, repos_dir, worktrees_dir


# ── RepoManager.cleanup_task_worktree unit tests ────────────────────────


class TestCleanupTaskWorktree:
    """Unit tests for RepoManager.cleanup_task_worktree()."""

    async def test_removes_worktree_and_local_branch(self, tmp_path):
        """Should remove the worktree directory and delete the local branch."""
        mgr, repos_dir, worktrees_dir = _setup_git_repo(tmp_path)
        origin = tmp_path / "origin"

        # Clone repo and create worktree
        await mgr.ensure_repo("testrepo", str(origin))
        branch = "agent/task-1-test-cleanup"
        wt_path = await mgr.create_worktree("testrepo", branch)

        assert wt_path.exists()

        # Cleanup
        result = await mgr.cleanup_task_worktree("testrepo", branch)

        assert result["worktree"] is True
        assert result["local_branch"] is True
        assert not wt_path.exists()

    async def test_deletes_remote_branch_when_requested(self, tmp_path):
        """Should delete the remote branch when delete_remote_branch=True."""
        mgr, repos_dir, worktrees_dir = _setup_git_repo(tmp_path)
        origin = tmp_path / "origin"

        await mgr.ensure_repo("testrepo", str(origin))
        branch = "agent/task-2-remote-cleanup"
        wt_path = await mgr.create_worktree("testrepo", branch)

        # Push the branch to the remote
        repo_path = repos_dir / "testrepo"
        subprocess.run(
            ["git", "push", "origin", branch],
            cwd=str(wt_path), check=True, capture_output=True,
        )

        result = await mgr.cleanup_task_worktree(
            "testrepo", branch, delete_remote_branch=True,
        )

        assert result["worktree"] is True
        assert result["local_branch"] is True
        assert result["remote_branch"] is True
        assert not wt_path.exists()

    async def test_skips_remote_branch_by_default(self, tmp_path):
        """Should NOT delete remote branch when delete_remote_branch=False."""
        mgr, repos_dir, worktrees_dir = _setup_git_repo(tmp_path)
        origin = tmp_path / "origin"

        await mgr.ensure_repo("testrepo", str(origin))
        branch = "agent/task-3-no-remote"
        wt_path = await mgr.create_worktree("testrepo", branch)

        # Push branch
        subprocess.run(
            ["git", "push", "origin", branch],
            cwd=str(wt_path), check=True, capture_output=True,
        )

        result = await mgr.cleanup_task_worktree("testrepo", branch)

        assert result["remote_branch"] is False

        # Verify the remote branch still exists
        proc = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", branch],
            cwd=str(repos_dir / "testrepo"),
            capture_output=True, text=True,
        )
        assert branch in proc.stdout

    async def test_handles_missing_worktree_gracefully(self, tmp_path):
        """Should not fail if worktree directory doesn't exist."""
        mgr, repos_dir, worktrees_dir = _setup_git_repo(tmp_path)
        origin = tmp_path / "origin"

        await mgr.ensure_repo("testrepo", str(origin))

        # Try cleanup for a branch that never had a worktree created
        result = await mgr.cleanup_task_worktree(
            "testrepo", "agent/task-99-nonexistent",
        )

        # worktree didn't exist, local branch didn't exist
        assert result["worktree"] is False
        assert result["local_branch"] is False
        assert result["remote_branch"] is False

    async def test_handles_missing_repo_gracefully(self, tmp_path):
        """Should not fail if repo directory doesn't exist."""
        repos_dir = tmp_path / "repos"
        worktrees_dir = tmp_path / "worktrees"
        repos_dir.mkdir()
        worktrees_dir.mkdir()

        mgr = RepoManager(repos_dir=repos_dir, worktrees_dir=worktrees_dir)

        result = await mgr.cleanup_task_worktree(
            "nonexistent-repo", "agent/task-1-test",
        )

        assert result == {"worktree": False, "local_branch": False, "remote_branch": False}

    async def test_handles_empty_branch_name(self, tmp_path):
        """Should not fail with empty branch name."""
        mgr, repos_dir, worktrees_dir = _setup_git_repo(tmp_path)
        origin = tmp_path / "origin"

        await mgr.ensure_repo("testrepo", str(origin))
        result = await mgr.cleanup_task_worktree("testrepo", "")

        assert result == {"worktree": False, "local_branch": False, "remote_branch": False}

    async def test_prunes_stale_worktree_refs(self, tmp_path):
        """Should run git worktree prune during cleanup."""
        mgr, repos_dir, worktrees_dir = _setup_git_repo(tmp_path)
        origin = tmp_path / "origin"

        await mgr.ensure_repo("testrepo", str(origin))
        branch = "agent/task-4-prune-test"
        wt_path = await mgr.create_worktree("testrepo", branch)

        # Manually delete the worktree directory to simulate a crash
        import shutil
        shutil.rmtree(wt_path)

        # Cleanup should prune the stale reference
        result = await mgr.cleanup_task_worktree("testrepo", branch)

        # Worktree was already gone, but branch should be cleaned
        assert result["local_branch"] is True

    async def test_rmtree_fallback_on_git_remove_failure(self, tmp_path):
        """Should fall back to rmtree if git worktree remove fails."""
        mgr, repos_dir, worktrees_dir = _setup_git_repo(tmp_path)
        origin = tmp_path / "origin"

        await mgr.ensure_repo("testrepo", str(origin))
        branch = "agent/task-5-rmtree-fallback"
        wt_path = await mgr.create_worktree("testrepo", branch)

        # Make git worktree remove fail by corrupting the lock
        lock_path = wt_path / ".git"
        # We can't easily make git worktree remove fail in a real scenario,
        # so let's test the logic with mocking
        original_run = mgr._run

        call_count = 0

        async def failing_run(*args, **kwargs):
            nonlocal call_count
            if "worktree" in args and "remove" in args:
                raise RuntimeError("Simulated git worktree remove failure")
            return await original_run(*args, **kwargs)

        mgr._run = failing_run

        result = await mgr.cleanup_task_worktree("testrepo", branch)

        # Should have fallen back to rmtree
        assert result["worktree"] is True
        assert not wt_path.exists()


# ── cleanup_task_worktree convenience wrapper ────────────────────────────


class TestCleanupTaskWorktreeWrapper:
    """Tests for the module-level convenience function."""

    async def test_wrapper_delegates_to_repo_manager(self, tmp_path):
        """The wrapper should create a RepoManager and call cleanup."""
        repos_dir = tmp_path / "repos"
        worktrees_dir = tmp_path / "worktrees"
        repos_dir.mkdir()
        worktrees_dir.mkdir()

        with patch.object(RepoManager, "cleanup_task_worktree", new_callable=AsyncMock) as mock_cleanup:
            mock_cleanup.return_value = {
                "worktree": True, "local_branch": True, "remote_branch": False,
            }

            result = await cleanup_task_worktree(
                repos_dir=repos_dir,
                worktrees_dir=worktrees_dir,
                repo_name="myrepo",
                branch_name="agent/task-1-test",
                delete_remote_branch=False,
            )

            mock_cleanup.assert_called_once_with(
                repo_name="myrepo",
                branch_name="agent/task-1-test",
                delete_remote_branch=False,
            )
            assert result["worktree"] is True


# ── RepoManager.list_worktrees ──────────────────────────────────────────


class TestListWorktrees:
    """Tests for RepoManager.list_worktrees()."""

    async def test_lists_worktrees(self, tmp_path):
        """Should return worktree paths and branches."""
        mgr, repos_dir, worktrees_dir = _setup_git_repo(tmp_path)
        origin = tmp_path / "origin"

        await mgr.ensure_repo("testrepo", str(origin))
        await mgr.create_worktree("testrepo", "agent/task-10-feature-a")
        await mgr.create_worktree("testrepo", "agent/task-11-feature-b")

        worktrees = await mgr.list_worktrees("testrepo")

        # Should include the main repo worktree plus the two we created
        branches = [wt.get("branch", "") for wt in worktrees]
        assert "agent/task-10-feature-a" in branches
        assert "agent/task-11-feature-b" in branches

    async def test_returns_empty_for_nonexistent_repo(self, tmp_path):
        """Should return empty list for a repo that doesn't exist."""
        repos_dir = tmp_path / "repos"
        worktrees_dir = tmp_path / "worktrees"
        repos_dir.mkdir()
        worktrees_dir.mkdir()

        mgr = RepoManager(repos_dir=repos_dir, worktrees_dir=worktrees_dir)
        result = await mgr.list_worktrees("nonexistent")

        assert result == []


# ── Orchestrator integration tests ──────────────────────────────────────


class TestOrchestratorWorktreeCleanup:
    """Test that the orchestrator triggers worktree cleanup on failure/cancel."""

    @patch("factory.orchestrator.cleanup_task_worktree", new_callable=AsyncMock)
    @patch("factory.orchestrator.cleanup_test_environments")
    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_cleanup_called_on_failure(
        self, MockRunner, MockRepoMgr, mock_docker_cleanup, mock_wt_cleanup,
    ):
        """Worktree cleanup should run when a task fails."""
        from factory.config import Config
        from factory.db import Database
        from factory.models import TaskCreate, TaskStatus
        from factory.orchestrator import Orchestrator

        db = Database(":memory:")
        await db.initialize()

        # Create a task that has a repo and branch_name set
        task = await db.create_task(TaskCreate(title="Test task", repo="myrepo"))
        await db.update_task_fields(task.id, branch_name="agent/task-1-test")
        await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

        config = Config()
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value

        # Simulate failure handling
        await orch._handle_failure(task.id, "some error output")

        mock_wt_cleanup.assert_called_once()
        call_kwargs = mock_wt_cleanup.call_args[1]
        assert call_kwargs["repo_name"] == "myrepo"
        assert call_kwargs["branch_name"] == "agent/task-1-test"
        assert call_kwargs["delete_remote_branch"] is True  # No PR URL

        await db.close()

    @patch("factory.orchestrator.cleanup_task_worktree", new_callable=AsyncMock)
    @patch("factory.orchestrator.cleanup_test_environments")
    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_no_remote_delete_when_pr_exists(
        self, MockRunner, MockRepoMgr, mock_docker_cleanup, mock_wt_cleanup,
    ):
        """Should NOT delete remote branch if task has a PR URL."""
        from factory.config import Config
        from factory.db import Database
        from factory.models import TaskCreate, TaskStatus
        from factory.orchestrator import Orchestrator

        db = Database(":memory:")
        await db.initialize()

        task = await db.create_task(TaskCreate(title="Test task", repo="myrepo"))
        await db.update_task_fields(
            task.id,
            branch_name="agent/task-2-test",
            pr_url="https://github.com/org/repo/pull/42",
        )
        await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

        config = Config()
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value

        await orch._handle_failure(task.id, "error")

        mock_wt_cleanup.assert_called_once()
        call_kwargs = mock_wt_cleanup.call_args[1]
        assert call_kwargs["delete_remote_branch"] is False

        await db.close()

    @patch("factory.orchestrator.cleanup_task_worktree", new_callable=AsyncMock)
    @patch("factory.orchestrator.cleanup_test_environments")
    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_cleanup_called_on_cancel(
        self, MockRunner, MockRepoMgr, mock_docker_cleanup, mock_wt_cleanup,
    ):
        """Worktree cleanup should run when a task is cancelled."""
        from factory.config import Config
        from factory.db import Database
        from factory.models import TaskCreate, TaskStatus
        from factory.orchestrator import Orchestrator

        db = Database(":memory:")
        await db.initialize()

        task = await db.create_task(TaskCreate(title="Cancel task", repo="myrepo"))
        await db.update_task_fields(task.id, branch_name="agent/task-3-cancel")
        await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

        config = Config()
        orch = Orchestrator(db=db, config=config)
        mock_runner = MockRunner.return_value
        mock_runner.cancel_agent = AsyncMock(return_value=True)
        orch.runner = mock_runner

        await orch.cancel_task(task.id)

        mock_wt_cleanup.assert_called_once()
        call_kwargs = mock_wt_cleanup.call_args[1]
        assert call_kwargs["repo_name"] == "myrepo"
        assert call_kwargs["branch_name"] == "agent/task-3-cancel"

        await db.close()

    @patch("factory.orchestrator.cleanup_task_worktree", new_callable=AsyncMock)
    @patch("factory.orchestrator.cleanup_test_environments")
    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_cleanup_failure_does_not_block_failure_handling(
        self, MockRunner, MockRepoMgr, mock_docker_cleanup, mock_wt_cleanup,
    ):
        """If worktree cleanup raises, failure handling should still complete."""
        from factory.config import Config
        from factory.db import Database
        from factory.models import TaskCreate, TaskStatus
        from factory.orchestrator import Orchestrator

        db = Database(":memory:")
        await db.initialize()

        task = await db.create_task(TaskCreate(title="Error task", repo="myrepo"))
        await db.update_task_fields(task.id, branch_name="agent/task-4-error")
        await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

        config = Config()
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value

        mock_wt_cleanup.side_effect = RuntimeError("git broken")

        # Should not raise
        await orch._handle_failure(task.id, "error output")

        # Task should still be marked as failed
        task = await db.get_task(task.id)
        assert task.status == TaskStatus.FAILED

        await db.close()

    @patch("factory.orchestrator.cleanup_task_worktree", new_callable=AsyncMock)
    @patch("factory.orchestrator.cleanup_test_environments")
    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_no_cleanup_when_waiting_for_input(
        self, MockRunner, MockRepoMgr, mock_docker_cleanup, mock_wt_cleanup,
    ):
        """Should NOT clean up worktree when task is waiting for user input."""
        from factory.config import Config
        from factory.db import Database
        from factory.models import TaskCreate, TaskStatus
        from factory.orchestrator import Orchestrator

        db = Database(":memory:")
        await db.initialize()

        task = await db.create_task(TaskCreate(title="Waiting task", repo="myrepo"))
        await db.update_task_fields(task.id, branch_name="agent/task-5-waiting")
        await db.update_task_status(task.id, TaskStatus.WAITING_FOR_INPUT)

        config = Config()
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value

        await orch._handle_failure(task.id, "error")

        # Should have returned early without calling cleanup
        mock_wt_cleanup.assert_not_called()

        await db.close()

    @patch("factory.orchestrator.cleanup_task_worktree", new_callable=AsyncMock)
    @patch("factory.orchestrator.cleanup_test_environments")
    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_no_cleanup_when_no_branch_name(
        self, MockRunner, MockRepoMgr, mock_docker_cleanup, mock_wt_cleanup,
    ):
        """Should skip cleanup if task has no branch_name set."""
        from factory.config import Config
        from factory.db import Database
        from factory.models import TaskCreate, TaskStatus
        from factory.orchestrator import Orchestrator

        db = Database(":memory:")
        await db.initialize()

        # Task without branch_name (e.g., failed before workspace setup)
        task = await db.create_task(TaskCreate(title="No branch", repo="myrepo"))
        await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

        config = Config()
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value

        await orch._handle_failure(task.id, "error")

        # Should not attempt cleanup without a branch name
        mock_wt_cleanup.assert_not_called()

        await db.close()

    @patch("factory.orchestrator.cleanup_task_worktree", new_callable=AsyncMock)
    @patch("factory.orchestrator.cleanup_test_environments")
    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_no_cleanup_when_no_repo(
        self, MockRunner, MockRepoMgr, mock_docker_cleanup, mock_wt_cleanup,
    ):
        """Should skip cleanup if task has no repo set."""
        from factory.config import Config
        from factory.db import Database
        from factory.models import TaskCreate, TaskStatus
        from factory.orchestrator import Orchestrator

        db = Database(":memory:")
        await db.initialize()

        task = await db.create_task(TaskCreate(title="No repo"))
        await db.update_task_fields(task.id, branch_name="agent/task-7")
        await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

        config = Config()
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value

        await orch._handle_failure(task.id, "error")

        # Should not attempt cleanup without a repo
        mock_wt_cleanup.assert_not_called()

        await db.close()


# ── Logging tests ────────────────────────────────────────────────────────


class TestWorktreeCleanupLogging:
    """Verify that cleanup actions are properly logged."""

    async def test_logs_successful_worktree_removal(self, tmp_path, caplog):
        """Should log when a worktree is successfully removed."""
        import logging

        mgr, repos_dir, worktrees_dir = _setup_git_repo(tmp_path)
        origin = tmp_path / "origin"

        await mgr.ensure_repo("testrepo", str(origin))
        branch = "agent/task-20-log-test"
        await mgr.create_worktree("testrepo", branch)

        with caplog.at_level(logging.INFO, logger="factory.workspace"):
            await mgr.cleanup_task_worktree("testrepo", branch)

        assert "Removed worktree" in caplog.text

    async def test_logs_successful_branch_deletion(self, tmp_path, caplog):
        """Should log when a local branch is deleted."""
        import logging

        mgr, repos_dir, worktrees_dir = _setup_git_repo(tmp_path)
        origin = tmp_path / "origin"

        await mgr.ensure_repo("testrepo", str(origin))
        branch = "agent/task-21-branch-log"
        await mgr.create_worktree("testrepo", branch)

        with caplog.at_level(logging.INFO, logger="factory.workspace"):
            await mgr.cleanup_task_worktree("testrepo", branch)

        assert "Deleted local branch" in caplog.text

    async def test_logs_warning_for_missing_repo(self, tmp_path, caplog):
        """Should log a warning when repo path doesn't exist."""
        import logging

        repos_dir = tmp_path / "repos"
        worktrees_dir = tmp_path / "worktrees"
        repos_dir.mkdir()
        worktrees_dir.mkdir()

        mgr = RepoManager(repos_dir=repos_dir, worktrees_dir=worktrees_dir)

        with caplog.at_level(logging.WARNING, logger="factory.workspace"):
            await mgr.cleanup_task_worktree("nonexistent", "agent/task-22")

        assert "does not exist" in caplog.text
