"""Tests for preview URL detection and Telegram notification.

Tests the orchestrator's ability to detect when agents deploy preview
environments and send Telegram notifications with the preview URL.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from factory.config import Config, RepoConfig, AgentTemplateConfig
from factory.db import Database
from factory.docker_toolkit import PREVIEW_DOMAIN
from factory.models import TaskCreate, TaskStatus
from factory.orchestrator import Orchestrator


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config():
    return Config()


# ── _detect_preview_url tests ─────────────────────────────────────────────


class TestDetectPreviewUrl:
    """Unit tests for preview URL detection in agent output."""

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_detects_test_env_url(self, MockRunner, MockRepoMgr, db, config):
        """Should detect a test environment URL in agent output."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        orch.notifier = AsyncMock()

        task = await db.create_task(TaskCreate(title="Test app", repo="myapp"))

        url = f"https://task-{task.id}.{PREVIEW_DOMAIN}"
        content = f"Test environment ready at {url}"

        with patch.object(orch, "_handle_preview_deployed", new_callable=AsyncMock):
            orch._detect_preview_url(task.id, content)
            await asyncio.sleep(0)

        assert task.id in orch._notified_preview_urls
        assert url in orch._notified_preview_urls[task.id]

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_detects_preview_env_url(self, MockRunner, MockRepoMgr, db, config):
        """Should detect a PR preview environment URL."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        orch.notifier = AsyncMock()

        url = f"https://pr-15.{PREVIEW_DOMAIN}"
        content = f"Preview environment ready at {url}"

        with patch.object(orch, "_handle_preview_deployed", new_callable=AsyncMock):
            orch._detect_preview_url(42, content)
            await asyncio.sleep(0)

        assert 42 in orch._notified_preview_urls
        assert url in orch._notified_preview_urls[42]

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_no_match_for_unrelated_output(self, MockRunner, MockRepoMgr, db, config):
        """Should not trigger for output without preview URLs."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value

        orch._detect_preview_url(42, "Just some regular output from the agent")

        assert 42 not in orch._notified_preview_urls

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_no_match_for_other_domains(self, MockRunner, MockRepoMgr, db, config):
        """Should not trigger for URLs on other domains."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value

        orch._detect_preview_url(42, "https://example.com/some-page")
        orch._detect_preview_url(42, "https://task-42.other-domain.com")

        assert 42 not in orch._notified_preview_urls

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_deduplication(self, MockRunner, MockRepoMgr, db, config):
        """Should not send duplicate notifications for the same URL."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        orch.notifier = AsyncMock()

        url = f"https://task-42.{PREVIEW_DOMAIN}"
        content = f"Test environment ready at {url}"

        with patch.object(orch, "_handle_preview_deployed", new_callable=AsyncMock) as mock_handle:
            orch._detect_preview_url(42, content)
            await asyncio.sleep(0)

            # Same URL again — should NOT create another task
            orch._detect_preview_url(42, content)
            await asyncio.sleep(0)

        # Only one call expected
        assert mock_handle.call_count == 1

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_different_urls_both_notified(self, MockRunner, MockRepoMgr, db, config):
        """Should notify for each unique URL (e.g., test then preview)."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        orch.notifier = AsyncMock()

        test_url = f"https://task-42.{PREVIEW_DOMAIN}"
        preview_url = f"https://pr-15.{PREVIEW_DOMAIN}"

        with patch.object(orch, "_handle_preview_deployed", new_callable=AsyncMock) as mock_handle:
            orch._detect_preview_url(42, f"Test ready at {test_url}")
            await asyncio.sleep(0)
            orch._detect_preview_url(42, f"Preview ready at {preview_url}")
            await asyncio.sleep(0)

        assert mock_handle.call_count == 2

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_url_embedded_in_longer_output(self, MockRunner, MockRepoMgr, db, config):
        """Should detect URLs embedded in longer output strings."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        orch.notifier = AsyncMock()

        url = f"https://task-42.{PREVIEW_DOMAIN}"
        content = f"Starting deployment...\nEnvironment ready at {url}\nRunning tests..."

        with patch.object(orch, "_handle_preview_deployed", new_callable=AsyncMock):
            orch._detect_preview_url(42, content)

        assert url in orch._notified_preview_urls[42]


# ── _handle_preview_deployed tests ─────────────────────────────────────


class TestHandlePreviewDeployed:
    """Tests for the async handler that stores URL and sends notifications."""

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_stores_preview_url_in_db(self, MockRunner, MockRepoMgr, db, config):
        """Should store the preview URL in the tasks table."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        orch.notifier = AsyncMock()

        task = await db.create_task(TaskCreate(title="Build app", repo="myapp"))
        assert task.preview_url == ""

        preview_url = f"https://task-{task.id}.{PREVIEW_DOMAIN}"
        await orch._handle_preview_deployed(task.id, preview_url)

        updated = await db.get_task(task.id)
        assert updated.preview_url == preview_url

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_sends_telegram_notification(self, MockRunner, MockRepoMgr, db, config):
        """Should send a Telegram notification with the preview URL."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        notifier = AsyncMock()
        orch.notifier = notifier

        task = await db.create_task(TaskCreate(title="Build app", repo="myapp"))

        preview_url = f"https://task-{task.id}.{PREVIEW_DOMAIN}"
        await orch._handle_preview_deployed(task.id, preview_url)

        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "Preview deployed" in msg
        assert "Build app" in msg
        assert preview_url in msg
        assert "\U0001f680" in msg  # rocket emoji

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_telegram_message_format(self, MockRunner, MockRepoMgr, db, config):
        """Verify the exact message format matches spec."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        notifier = AsyncMock()
        orch.notifier = notifier

        task = await db.create_task(TaskCreate(title="My Task", repo="myapp"))

        preview_url = f"https://task-{task.id}.{PREVIEW_DOMAIN}"
        await orch._handle_preview_deployed(task.id, preview_url)

        msg = notifier.send.call_args[0][0]
        expected = f"\U0001f680 Preview deployed: My Task\nURL: {preview_url}"
        assert msg == expected

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_posts_plane_comment(self, MockRunner, MockRepoMgr, db, config):
        """Should post a comment to the Plane issue."""
        config.plane.api_key = "test-key"
        config.plane.base_url = "https://plane.test"
        config.plane.workspace_slug = "test-ws"
        config.plane.project_id = "test-proj"

        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        orch.notifier = AsyncMock()
        orch.plane = AsyncMock()

        task = await db.create_task(TaskCreate(
            title="Build app", repo="myapp", plane_issue_id="issue-123",
        ))

        preview_url = f"https://task-{task.id}.{PREVIEW_DOMAIN}"
        await orch._handle_preview_deployed(task.id, preview_url)

        orch.plane.add_comment.assert_called_once()
        comment_args = orch.plane.add_comment.call_args[0]
        assert preview_url in comment_args[2]

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_adds_log_entry(self, MockRunner, MockRepoMgr, db, config):
        """Should add a log entry for the deployment."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        orch.notifier = AsyncMock()

        task = await db.create_task(TaskCreate(title="Build app", repo="myapp"))

        preview_url = f"https://task-{task.id}.{PREVIEW_DOMAIN}"
        await orch._handle_preview_deployed(task.id, preview_url)

        logs = await db.get_logs(task.id)
        assert any(preview_url in log["message"] for log in logs)

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_handles_missing_task_gracefully(self, MockRunner, MockRepoMgr, db, config):
        """Should not raise when the task doesn't exist."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        orch.notifier = AsyncMock()

        # Task 999 doesn't exist
        await orch._handle_preview_deployed(999, f"https://task-999.{PREVIEW_DOMAIN}")

        # No notification should be sent
        orch.notifier.send.assert_not_called()

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_no_telegram_when_notifier_is_none(self, MockRunner, MockRepoMgr, db, config):
        """Should work without errors when no Telegram notifier is configured."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        orch.notifier = None

        task = await db.create_task(TaskCreate(title="Build app", repo="myapp"))

        preview_url = f"https://task-{task.id}.{PREVIEW_DOMAIN}"
        # Should not raise
        await orch._handle_preview_deployed(task.id, preview_url)

        # URL should still be stored
        updated = await db.get_task(task.id)
        assert updated.preview_url == preview_url


# ── _on_agent_output integration tests ────────────────────────────────────


class TestOnAgentOutputPreviewDetection:
    """Test that preview URL detection is integrated into _on_agent_output."""

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_preview_url_detected_in_agent_output(self, MockRunner, MockRepoMgr, db, config):
        """Preview URLs in agent output should trigger notification."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        orch.notifier = AsyncMock()

        task = await db.create_task(TaskCreate(title="Build app", repo="myapp"))
        await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

        url = f"https://task-{task.id}.{PREVIEW_DOMAIN}"
        content = f"Test environment ready at {url}"

        orch._on_agent_output(task.id, content)

        # Give event loop a tick
        await asyncio.sleep(0.1)

        # URL should be in notified set
        assert task.id in orch._notified_preview_urls
        assert url in orch._notified_preview_urls[task.id]

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_regular_output_does_not_trigger(self, MockRunner, MockRepoMgr, db, config):
        """Regular agent output without URLs should not trigger notifications."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        orch.notifier = AsyncMock()

        task = await db.create_task(TaskCreate(title="Fix bug", repo="myapp"))

        orch._on_agent_output(task.id, "Analyzing code...")

        assert task.id not in orch._notified_preview_urls


# ── _on_agent_complete cleanup tests ──────────────────────────────────────


class TestOnAgentCompletePreviewCleanup:
    """Test that preview URL tracking is cleaned up on task completion."""

    @patch("factory.orchestrator.cleanup_test_environments")
    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_cleans_up_notified_urls_on_complete(
        self, MockRunner, MockRepoMgr, mock_cleanup, db, config,
    ):
        """Should clean up the notified URLs tracking on task completion."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        orch.notifier = AsyncMock()

        # Pre-populate the tracking set
        orch._notified_preview_urls[42] = {f"https://task-42.{PREVIEW_DOMAIN}"}

        orch._on_agent_complete(task_id=42, returncode=0, output="done")

        assert 42 not in orch._notified_preview_urls


# ── _handle_success with preview URL tests ───────────────────────────────


class TestHandleSuccessWithPreviewUrl:
    """Test that _handle_success includes preview URL in notifications."""

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_success_notification_includes_preview_url(
        self, MockRunner, MockRepoMgr, db, config,
    ):
        """Completion notification should include the preview URL if set."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        notifier = AsyncMock()
        orch.notifier = notifier

        task = await db.create_task(TaskCreate(
            title="Build greeting app", repo="myapp",
        ))
        await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)
        await db.update_task_fields(
            task.id,
            branch_name="agent-task-1",
            preview_url=f"https://task-{task.id}.{PREVIEW_DOMAIN}",
        )

        # Mock the PR creation path
        with patch.object(orch, "_push_and_create_pr", new_callable=AsyncMock) as mock_pr:
            mock_pr.return_value = "https://github.com/user/myapp/pull/1"
            await orch._handle_success(task.id, "## Summary\nDone.")

        # Check the notification message includes preview URL
        calls = notifier.send.call_args_list
        assert len(calls) >= 1
        final_msg = calls[-1][0][0]
        assert "Preview:" in final_msg
        assert f"https://task-{task.id}.{PREVIEW_DOMAIN}" in final_msg

    @patch("factory.orchestrator.RepoManager")
    @patch("factory.orchestrator.AgentRunner")
    async def test_success_notification_without_preview_url(
        self, MockRunner, MockRepoMgr, db, config,
    ):
        """Completion notification should work fine without a preview URL."""
        orch = Orchestrator(db=db, config=config)
        orch.runner = MockRunner.return_value
        notifier = AsyncMock()
        orch.notifier = notifier

        task = await db.create_task(TaskCreate(
            title="Fix bug", repo="myapp",
        ))
        await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)
        await db.update_task_fields(task.id, branch_name="agent-task-2")

        with patch.object(orch, "_push_and_create_pr", new_callable=AsyncMock) as mock_pr:
            mock_pr.return_value = "https://github.com/user/myapp/pull/2"
            await orch._handle_success(task.id, "## Summary\nFixed.")

        calls = notifier.send.call_args_list
        assert len(calls) >= 1
        final_msg = calls[-1][0][0]
        assert "Preview:" not in final_msg


# ── Database migration tests ──────────────────────────────────────────────


class TestPreviewUrlDatabase:
    """Test the preview_url database column and migrations."""

    async def test_preview_url_column_exists(self, db):
        """The preview_url column should exist after migration."""
        task = await db.create_task(TaskCreate(title="Test", repo="myapp"))
        assert task.preview_url == ""

    async def test_preview_url_can_be_updated(self, db):
        """Should be able to update the preview_url field."""
        task = await db.create_task(TaskCreate(title="Test", repo="myapp"))

        preview_url = f"https://task-{task.id}.{PREVIEW_DOMAIN}"
        updated = await db.update_task_fields(task.id, preview_url=preview_url)

        assert updated.preview_url == preview_url

    async def test_preview_url_persists_after_refetch(self, db):
        """Preview URL should persist when re-fetching the task."""
        task = await db.create_task(TaskCreate(title="Test", repo="myapp"))
        preview_url = f"https://task-{task.id}.{PREVIEW_DOMAIN}"

        await db.update_task_fields(task.id, preview_url=preview_url)
        refetched = await db.get_task(task.id)

        assert refetched.preview_url == preview_url

    async def test_preview_url_in_task_list(self, db):
        """Preview URL should be present when listing tasks."""
        task = await db.create_task(TaskCreate(title="Test", repo="myapp"))
        preview_url = f"https://task-{task.id}.{PREVIEW_DOMAIN}"

        await db.update_task_fields(task.id, preview_url=preview_url)
        tasks = await db.list_tasks()

        assert tasks[0].preview_url == preview_url

    async def test_preview_url_default_empty(self, db):
        """New tasks should have an empty preview_url by default."""
        task = await db.create_task(TaskCreate(title="Test", repo="myapp"))
        assert task.preview_url == ""


# ── Preview URL regex tests ──────────────────────────────────────────────


class TestPreviewUrlRegex:
    """Test the regex pattern used for preview URL detection."""

    def _match(self, content: str) -> str | None:
        """Helper to test the regex directly."""
        match = Orchestrator._PREVIEW_URL_RE.search(content)
        return match.group(0) if match else None

    def test_matches_test_env_url(self):
        url = f"https://task-42.{PREVIEW_DOMAIN}"
        assert self._match(f"Ready at {url}") == url

    def test_matches_preview_env_url(self):
        url = f"https://pr-15.{PREVIEW_DOMAIN}"
        assert self._match(f"Preview: {url}") == url

    def test_matches_large_task_id(self):
        url = f"https://task-99999.{PREVIEW_DOMAIN}"
        assert self._match(url) == url

    def test_matches_single_digit_pr(self):
        url = f"https://pr-1.{PREVIEW_DOMAIN}"
        assert self._match(url) == url

    def test_no_match_for_http(self):
        """Should not match HTTP URLs (only HTTPS)."""
        assert self._match(f"http://task-42.{PREVIEW_DOMAIN}") is None

    def test_no_match_for_wrong_domain(self):
        assert self._match("https://task-42.example.com") is None

    def test_no_match_for_missing_prefix(self):
        assert self._match(f"https://foo-42.{PREVIEW_DOMAIN}") is None

    def test_no_match_for_empty_string(self):
        assert self._match("") is None

    def test_no_match_for_partial_url(self):
        assert self._match(f"task-42.{PREVIEW_DOMAIN}") is None

    def test_url_in_json_output(self):
        """Should detect URL in JSON-formatted agent output."""
        url = f"https://task-42.{PREVIEW_DOMAIN}"
        content = f'{{"message": "Environment ready at {url}"}}'
        assert self._match(content) == url

    def test_url_in_markdown_output(self):
        """Should detect URL in markdown-formatted output."""
        url = f"https://pr-10.{PREVIEW_DOMAIN}"
        content = f"Preview: [{url}]({url})"
        assert self._match(content) == url
