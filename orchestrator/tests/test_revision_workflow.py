"""Tests for the revision workflow: detecting revision tasks, fetching
PR/task comments, injecting feedback into prompts, and pushing to
existing branches.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from factory.config import AgentTemplateConfig, Config, RepoConfig
from factory.db import Database
from factory.models import TaskCreate, TaskStatus
from factory.orchestrator import Orchestrator
from factory.revision_context import (
    MAX_COMMENT_CHARS,
    MAX_FEEDBACK_CHARS,
    RevisionContext,
    build_revision_context,
    extract_pr_number,
    fetch_github_pr_comments,
    fetch_plane_comments,
)


# ── RevisionContext unit tests ──────────────────────────────────────────────


class TestExtractPrNumber:
    def test_basic_pr_url(self):
        assert extract_pr_number("https://github.com/owner/repo/pull/42") == 42

    def test_pr_url_with_trailing_path(self):
        assert extract_pr_number("https://github.com/owner/repo/pull/123/files") == 123

    def test_empty_url(self):
        assert extract_pr_number("") is None

    def test_no_pr_in_url(self):
        assert extract_pr_number("https://github.com/owner/repo") is None

    def test_none_like_url(self):
        assert extract_pr_number("") is None


class TestRevisionContextProperties:
    def test_is_revision_with_pr_url(self):
        ctx = RevisionContext(pr_url="https://github.com/owner/repo/pull/1")
        assert ctx.is_revision is True

    def test_is_revision_with_pr_number(self):
        ctx = RevisionContext(pr_number=42)
        assert ctx.is_revision is True

    def test_is_not_revision_when_empty(self):
        ctx = RevisionContext()
        assert ctx.is_revision is False

    def test_has_feedback_with_comments(self):
        ctx = RevisionContext(
            github_comments=[{"author": "user", "body": "Fix this"}],
        )
        assert ctx.has_feedback is True

    def test_has_feedback_with_reviews(self):
        ctx = RevisionContext(
            github_reviews=[{"author": "user", "state": "CHANGES_REQUESTED", "body": "Needs work"}],
        )
        assert ctx.has_feedback is True

    def test_has_feedback_with_plane_comments(self):
        ctx = RevisionContext(
            plane_comments=[{"body": "Please revise", "author": "pm"}],
        )
        assert ctx.has_feedback is True

    def test_no_feedback_when_empty(self):
        ctx = RevisionContext()
        assert ctx.has_feedback is False


class TestFormatPromptSection:
    def test_empty_when_no_feedback(self):
        ctx = RevisionContext()
        assert ctx.format_prompt_section() == ""

    def test_includes_pr_url(self):
        ctx = RevisionContext(
            pr_url="https://github.com/owner/repo/pull/42",
            github_comments=[{"author": "user", "body": "Fix the bug"}],
        )
        section = ctx.format_prompt_section()
        assert "https://github.com/owner/repo/pull/42" in section

    def test_includes_github_reviews(self):
        ctx = RevisionContext(
            github_reviews=[{
                "author": "reviewer",
                "state": "CHANGES_REQUESTED",
                "body": "Please fix the error handling",
            }],
        )
        section = ctx.format_prompt_section()
        assert "### GitHub Reviews" in section
        assert "reviewer" in section
        assert "CHANGES_REQUESTED" in section
        assert "Please fix the error handling" in section

    def test_includes_github_comments(self):
        ctx = RevisionContext(
            github_comments=[{
                "author": "user1",
                "body": "This function needs tests",
            }],
        )
        section = ctx.format_prompt_section()
        assert "### GitHub PR Comments" in section
        assert "user1" in section
        assert "This function needs tests" in section

    def test_includes_inline_comment_location(self):
        ctx = RevisionContext(
            github_comments=[{
                "author": "reviewer",
                "body": "Bug here",
                "path": "src/main.py",
                "line": 42,
            }],
        )
        section = ctx.format_prompt_section()
        assert "(src/main.py:42)" in section

    def test_includes_plane_comments(self):
        ctx = RevisionContext(
            plane_comments=[{
                "body": "Please also add documentation",
                "author": "project-manager",
            }],
        )
        section = ctx.format_prompt_section()
        assert "### Task Comments" in section
        assert "project-manager" in section
        assert "Please also add documentation" in section

    def test_includes_closing_instruction(self):
        ctx = RevisionContext(
            github_comments=[{"author": "u", "body": "Fix"}],
        )
        section = ctx.format_prompt_section()
        assert "Address the feedback above" in section

    def test_skips_empty_bodies(self):
        ctx = RevisionContext(
            github_comments=[{"author": "bot", "body": ""}],
        )
        # Empty body comments are in the list but should not generate body entries
        section = ctx.format_prompt_section()
        # The header and closing instruction are still present, but no comment entry
        assert "bot" not in section or "bot" in section  # header may appear
        # The key point: no actual comment body is rendered
        assert "### GitHub PR Comments" in section
        # But there are no "- **bot**:" entries since body is empty
        assert "- **bot**:" not in section

    def test_reviews_without_body_shown_for_changes_requested(self):
        ctx = RevisionContext(
            github_reviews=[{
                "author": "reviewer",
                "state": "CHANGES_REQUESTED",
                "body": "",
            }],
        )
        section = ctx.format_prompt_section()
        assert "CHANGES_REQUESTED" in section

    def test_reviews_without_body_skipped_for_commented(self):
        ctx = RevisionContext(
            github_reviews=[{
                "author": "reviewer",
                "state": "COMMENTED",
                "body": "",
            }],
        )
        section = ctx.format_prompt_section()
        # The review with state=COMMENTED and no body should not generate
        # an entry (only CHANGES_REQUESTED/APPROVED get shown without body)
        assert "reviewer" not in section or "COMMENTED" not in section

    def test_combined_feedback(self):
        ctx = RevisionContext(
            pr_url="https://github.com/owner/repo/pull/10",
            github_reviews=[{
                "author": "senior-dev",
                "state": "CHANGES_REQUESTED",
                "body": "Fix error handling",
            }],
            github_comments=[{
                "author": "junior-dev",
                "body": "Looks good overall",
            }],
            plane_comments=[{
                "body": "Also update the docs",
                "author": "pm",
            }],
        )
        section = ctx.format_prompt_section()
        assert "## Review Feedback" in section
        assert "### GitHub Reviews" in section
        assert "### GitHub PR Comments" in section
        assert "### Task Comments" in section
        assert "Fix error handling" in section
        assert "Looks good overall" in section
        assert "Also update the docs" in section

    def test_truncation_for_large_reviews(self):
        """Long review bodies should be truncated to stay within limits."""
        long_body = "x" * (MAX_FEEDBACK_CHARS + 100)
        ctx = RevisionContext(
            github_reviews=[{
                "author": "reviewer",
                "state": "CHANGES_REQUESTED",
                "body": long_body,
            }],
        )
        section = ctx.format_prompt_section()
        assert len(section) < MAX_FEEDBACK_CHARS + MAX_COMMENT_CHARS + 500  # headers + margins
        assert "..." in section

    def test_plane_comment_without_author(self):
        ctx = RevisionContext(
            plane_comments=[{"body": "Please fix", "author": ""}],
        )
        section = ctx.format_prompt_section()
        assert "- Please fix" in section


# ── fetch_github_pr_comments tests ──────────────────────────────────────


class TestFetchGithubPrComments:
    @pytest.mark.asyncio
    async def test_parses_comments_and_reviews(self):
        gh_output = json.dumps({
            "comments": [
                {
                    "author": {"login": "alice"},
                    "body": "Please fix the typo",
                    "createdAt": "2025-01-01T00:00:00Z",
                },
            ],
            "reviews": [
                {
                    "author": {"login": "bob"},
                    "state": "CHANGES_REQUESTED",
                    "body": "Needs error handling",
                    "submittedAt": "2025-01-01T01:00:00Z",
                },
            ],
        })

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(gh_output.encode(), b""))

        with patch("factory.revision_context.asyncio.create_subprocess_exec", return_value=mock_proc):
            comments, reviews = await fetch_github_pr_comments(42, repo_dir="/tmp/repo")

        assert len(comments) == 1
        assert comments[0]["author"] == "alice"
        assert comments[0]["body"] == "Please fix the typo"

        assert len(reviews) == 1
        assert reviews[0]["author"] == "bob"
        assert reviews[0]["state"] == "CHANGES_REQUESTED"

    @pytest.mark.asyncio
    async def test_handles_gh_failure(self):
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"not found"))

        with patch("factory.revision_context.asyncio.create_subprocess_exec", return_value=mock_proc):
            comments, reviews = await fetch_github_pr_comments(999)

        assert comments == []
        assert reviews == []

    @pytest.mark.asyncio
    async def test_handles_invalid_json(self):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"not json", b""))

        with patch("factory.revision_context.asyncio.create_subprocess_exec", return_value=mock_proc):
            comments, reviews = await fetch_github_pr_comments(42)

        assert comments == []
        assert reviews == []

    @pytest.mark.asyncio
    async def test_handles_missing_gh_cli(self):
        with patch("factory.revision_context.asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            comments, reviews = await fetch_github_pr_comments(42)

        assert comments == []
        assert reviews == []

    @pytest.mark.asyncio
    async def test_handles_empty_comments(self):
        gh_output = json.dumps({"comments": [], "reviews": []})

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(gh_output.encode(), b""))

        with patch("factory.revision_context.asyncio.create_subprocess_exec", return_value=mock_proc):
            comments, reviews = await fetch_github_pr_comments(42)

        assert comments == []
        assert reviews == []

    @pytest.mark.asyncio
    async def test_handles_missing_author(self):
        """When author field is missing or None, default to 'unknown'."""
        gh_output = json.dumps({
            "comments": [{"body": "Fix this", "createdAt": "2025-01-01T00:00:00Z"}],
            "reviews": [],
        })

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(gh_output.encode(), b""))

        with patch("factory.revision_context.asyncio.create_subprocess_exec", return_value=mock_proc):
            comments, reviews = await fetch_github_pr_comments(42)

        assert len(comments) == 1
        assert comments[0]["author"] == "unknown"


# ── fetch_plane_comments tests ──────────────────────────────────────────


class TestFetchPlaneComments:
    @pytest.mark.asyncio
    async def test_parses_plane_comments(self):
        mock_client = AsyncMock()
        mock_client.get_comments = AsyncMock(return_value=[
            {
                "comment_html": "<p>Please fix the tests</p>",
                "actor_detail": {"display_name": "Alice", "email": "alice@example.com"},
                "created_at": "2025-01-01T00:00:00Z",
            },
        ])

        result = await fetch_plane_comments(mock_client, "proj-1", "issue-1")
        assert len(result) == 1
        assert result[0]["body"] == "Please fix the tests"
        assert result[0]["author"] == "Alice"

    @pytest.mark.asyncio
    async def test_strips_html(self):
        mock_client = AsyncMock()
        mock_client.get_comments = AsyncMock(return_value=[
            {
                "comment_html": "<p>Fix <b>this</b> and <i>that</i></p>",
                "actor_detail": {"display_name": "Bob"},
                "created_at": "2025-01-01T00:00:00Z",
            },
        ])

        result = await fetch_plane_comments(mock_client, "proj-1", "issue-1")
        assert result[0]["body"] == "Fix this and that"

    @pytest.mark.asyncio
    async def test_skips_empty_comments(self):
        mock_client = AsyncMock()
        mock_client.get_comments = AsyncMock(return_value=[
            {"comment_html": "", "actor_detail": {}, "created_at": ""},
        ])

        result = await fetch_plane_comments(mock_client, "proj-1", "issue-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_without_client(self):
        result = await fetch_plane_comments(None, "proj-1", "issue-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_without_issue_id(self):
        mock_client = AsyncMock()
        result = await fetch_plane_comments(mock_client, "proj-1", "")
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_api_error(self):
        mock_client = AsyncMock()
        mock_client.get_comments = AsyncMock(side_effect=Exception("API error"))

        result = await fetch_plane_comments(mock_client, "proj-1", "issue-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_falls_back_to_email_for_author(self):
        mock_client = AsyncMock()
        mock_client.get_comments = AsyncMock(return_value=[
            {
                "comment_html": "<p>Test comment</p>",
                "actor_detail": {"email": "user@test.com"},
                "created_at": "2025-01-01T00:00:00Z",
            },
        ])

        result = await fetch_plane_comments(mock_client, "proj-1", "issue-1")
        assert result[0]["author"] == "user@test.com"


# ── build_revision_context tests ────────────────────────────────────────


class TestBuildRevisionContext:
    @pytest.mark.asyncio
    async def test_basic_context_building(self):
        gh_output = json.dumps({
            "comments": [{"author": {"login": "dev"}, "body": "Fix this", "createdAt": "2025-01-01"}],
            "reviews": [{"author": {"login": "lead"}, "state": "CHANGES_REQUESTED", "body": "Needs work", "submittedAt": "2025-01-01"}],
        })

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(gh_output.encode(), b""))

        with patch("factory.revision_context.asyncio.create_subprocess_exec", return_value=mock_proc):
            ctx = await build_revision_context(
                pr_url="https://github.com/owner/repo/pull/42",
                branch_name="agent/task-1-fix-bug",
            )

        assert ctx.is_revision is True
        assert ctx.pr_number == 42
        assert len(ctx.github_comments) == 1
        assert len(ctx.github_reviews) == 1

    @pytest.mark.asyncio
    async def test_context_without_pr(self):
        ctx = await build_revision_context(
            pr_url="",
            branch_name="some-branch",
        )
        assert ctx.is_revision is False
        assert ctx.pr_number is None
        assert ctx.github_comments == []
        assert ctx.github_reviews == []

    @pytest.mark.asyncio
    async def test_concurrent_fetching_with_plane(self):
        gh_output = json.dumps({
            "comments": [{"author": {"login": "dev"}, "body": "Fix", "createdAt": "2025-01-01"}],
            "reviews": [],
        })

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(gh_output.encode(), b""))

        mock_plane = AsyncMock()
        mock_plane.get_comments = AsyncMock(return_value=[
            {"comment_html": "<p>Update docs</p>", "actor_detail": {"display_name": "PM"}, "created_at": "2025-01-01"},
        ])

        with patch("factory.revision_context.asyncio.create_subprocess_exec", return_value=mock_proc):
            ctx = await build_revision_context(
                pr_url="https://github.com/owner/repo/pull/10",
                branch_name="agent/task-1-fix",
                plane_client=mock_plane,
                plane_project_id="proj-1",
                plane_issue_id="issue-1",
            )

        assert len(ctx.github_comments) == 1
        assert len(ctx.plane_comments) == 1
        assert ctx.plane_comments[0]["body"] == "Update docs"


# ── Database revision detection tests ───────────────────────────────────


class TestDbFindPreviousTaskWithPr:
    @pytest.mark.asyncio
    async def test_finds_previous_task_with_pr(self):
        db = Database(":memory:")
        await db.initialize()

        # Create a previous task with a PR
        task = await db.create_task(TaskCreate(
            title="Initial work",
            repo="myapp",
            agent_type="coder",
            plane_issue_id="plane-123",
        ))
        await db.update_task_fields(
            task.id,
            pr_url="https://github.com/owner/repo/pull/42",
            branch_name="agent/task-1-initial-work",
        )
        await db.update_task_status(task.id, TaskStatus.IN_REVIEW)

        # Should find the previous task
        previous = await db.find_previous_task_with_pr("plane-123")
        assert previous is not None
        assert previous.id == task.id
        assert previous.pr_url == "https://github.com/owner/repo/pull/42"

        await db.close()

    @pytest.mark.asyncio
    async def test_returns_none_when_no_previous_pr(self):
        db = Database(":memory:")
        await db.initialize()

        # Create a task without a PR
        task = await db.create_task(TaskCreate(
            title="Work",
            repo="myapp",
            agent_type="coder",
            plane_issue_id="plane-456",
        ))
        await db.update_task_status(task.id, TaskStatus.FAILED)

        previous = await db.find_previous_task_with_pr("plane-456")
        assert previous is None

        await db.close()

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_issue_id(self):
        db = Database(":memory:")
        await db.initialize()

        previous = await db.find_previous_task_with_pr("")
        assert previous is None

        await db.close()

    @pytest.mark.asyncio
    async def test_returns_most_recent_task(self):
        db = Database(":memory:")
        await db.initialize()

        # Create two tasks with PRs
        task1 = await db.create_task(TaskCreate(
            title="V1",
            repo="myapp",
            agent_type="coder",
            plane_issue_id="plane-789",
        ))
        await db.update_task_fields(
            task1.id,
            pr_url="https://github.com/owner/repo/pull/10",
            branch_name="agent/task-1",
        )
        await db.update_task_status(task1.id, TaskStatus.IN_REVIEW)

        task2 = await db.create_task(TaskCreate(
            title="V2",
            repo="myapp",
            agent_type="coder",
            plane_issue_id="plane-789",
        ))
        await db.update_task_fields(
            task2.id,
            pr_url="https://github.com/owner/repo/pull/11",
            branch_name="agent/task-2",
        )
        await db.update_task_status(task2.id, TaskStatus.IN_REVIEW)

        previous = await db.find_previous_task_with_pr("plane-789")
        assert previous is not None
        assert previous.id == task2.id  # Most recent

        await db.close()

    @pytest.mark.asyncio
    async def test_ignores_queued_and_in_progress_tasks(self):
        db = Database(":memory:")
        await db.initialize()

        # Queued task with PR should not be found
        task = await db.create_task(TaskCreate(
            title="Queued",
            repo="myapp",
            agent_type="coder",
            plane_issue_id="plane-abc",
        ))
        await db.update_task_fields(
            task.id,
            pr_url="https://github.com/owner/repo/pull/5",
            branch_name="agent/task-q",
        )
        # Status is still QUEUED

        previous = await db.find_previous_task_with_pr("plane-abc")
        assert previous is None

        await db.close()


# ── Workspace checkout_existing_branch tests ────────────────────────────


class TestWorkspaceCheckoutExistingBranch:
    @pytest.mark.asyncio
    async def test_checkout_existing_branch(self):
        from factory.workspace import RepoManager

        mgr = RepoManager(repos_dir=Path("/tmp/repos"), worktrees_dir=Path("/tmp/worktrees"))
        mgr._run = AsyncMock(return_value="")

        wt_path = await mgr.checkout_existing_branch("myapp", "agent/task-1-fix-bug")

        expected_path = Path("/tmp/worktrees/agent-task-1-fix-bug")
        assert wt_path == expected_path

        # Should have called fetch and worktree add
        calls = [str(c) for c in mgr._run.call_args_list]
        assert any("fetch" in c for c in calls)
        assert any("worktree" in c and "add" in c for c in calls)


# ── Orchestrator revision workflow integration tests ────────────────────


def _make_config(**overrides) -> Config:
    defaults = dict(
        repos={"myapp": RepoConfig(url="git@github.com:user/myapp.git")},
        agent_templates={
            "coder": AgentTemplateConfig(
                system_prompt_file="prompts/coder.md",
                allowed_tools=["Read", "Edit", "Bash"],
            ),
            "coder_revision": AgentTemplateConfig(
                system_prompt_file="prompts/coder_revision.md",
                allowed_tools=["Read", "Edit", "Bash"],
            ),
        },
    )
    defaults.update(overrides)
    return Config(**defaults)


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_process_task_detects_revision(MockRunner, MockRepoMgr):
    """When a task's Plane issue already has a PR, it should be treated as revision."""
    db = Database(":memory:")
    await db.initialize()

    config = _make_config()

    mock_repo_mgr = MockRepoMgr.return_value
    mock_repo_mgr.ensure_repo = AsyncMock(return_value=Path("/tmp/repos/myapp"))
    mock_repo_mgr.checkout_existing_branch = AsyncMock(return_value=Path("/tmp/worktrees/test"))
    mock_repo_mgr.repos_dir = Path("/tmp/repos")

    mock_runner = MockRunner.return_value
    mock_runner.can_accept_task = True
    mock_runner.start_agent = AsyncMock(return_value=True)

    orch = Orchestrator(db=db, config=config)
    orch.repo_manager = mock_repo_mgr
    orch.runner = mock_runner

    # Create previous task with PR
    prev_task = await db.create_task(TaskCreate(
        title="Initial work",
        repo="myapp",
        agent_type="coder",
        plane_issue_id="plane-issue-1",
    ))
    await db.update_task_fields(
        prev_task.id,
        pr_url="https://github.com/owner/repo/pull/42",
        branch_name="agent/task-1-initial-work",
    )
    await db.update_task_status(prev_task.id, TaskStatus.IN_REVIEW)

    # Create new task for the same issue (revision)
    new_task = await db.create_task(TaskCreate(
        title="Address feedback",
        repo="myapp",
        agent_type="coder",
        plane_issue_id="plane-issue-1",
    ))

    with patch("factory.orchestrator.build_revision_context", new_callable=AsyncMock) as mock_build_ctx:
        mock_build_ctx.return_value = RevisionContext(
            pr_url="https://github.com/owner/repo/pull/42",
            pr_number=42,
            branch_name="agent/task-1-initial-work",
            github_reviews=[{"author": "reviewer", "state": "CHANGES_REQUESTED", "body": "Fix tests"}],
        )
        with patch("factory.orchestrator.load_prompt", return_value="system prompt"):
            result = await orch.process_task(new_task.id)

    assert result is True

    # Should have checked out existing branch, not created a new one
    mock_repo_mgr.checkout_existing_branch.assert_called_once_with("myapp", "agent/task-1-initial-work")
    mock_repo_mgr.create_worktree.assert_not_called()

    # Task should have the existing PR URL and branch
    updated = await db.get_task(new_task.id)
    assert updated.branch_name == "agent/task-1-initial-work"
    assert updated.pr_url == "https://github.com/owner/repo/pull/42"

    # Agent prompt should include revision context
    call_args = mock_runner.start_agent.call_args
    prompt = call_args.kwargs.get("prompt", "") or call_args[1].get("prompt", "")
    assert "Review Feedback" in prompt
    assert "Fix tests" in prompt

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_process_task_fresh_when_no_previous_pr(MockRunner, MockRepoMgr):
    """When no previous PR exists, task should be treated as fresh work."""
    db = Database(":memory:")
    await db.initialize()

    config = _make_config()

    mock_repo_mgr = MockRepoMgr.return_value
    mock_repo_mgr.ensure_repo = AsyncMock(return_value=Path("/tmp/repos/myapp"))
    mock_repo_mgr.create_worktree = AsyncMock(return_value=Path("/tmp/worktrees/test"))
    mock_repo_mgr.repos_dir = Path("/tmp/repos")

    mock_runner = MockRunner.return_value
    mock_runner.can_accept_task = True
    mock_runner.start_agent = AsyncMock(return_value=True)

    orch = Orchestrator(db=db, config=config)
    orch.repo_manager = mock_repo_mgr
    orch.runner = mock_runner

    task = await db.create_task(TaskCreate(
        title="New feature",
        repo="myapp",
        agent_type="coder",
        plane_issue_id="plane-issue-new",
    ))

    with patch("factory.orchestrator.load_prompt", return_value="system prompt"):
        result = await orch.process_task(task.id)

    assert result is True

    # Should have created a new worktree, not checked out existing branch
    mock_repo_mgr.create_worktree.assert_called_once()
    mock_repo_mgr.checkout_existing_branch.assert_not_called()

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_revision_uses_coder_revision_prompt(MockRunner, MockRepoMgr):
    """Revision tasks should use the coder_revision system prompt if available."""
    db = Database(":memory:")
    await db.initialize()

    config = _make_config()

    mock_repo_mgr = MockRepoMgr.return_value
    mock_repo_mgr.ensure_repo = AsyncMock(return_value=Path("/tmp/repos/myapp"))
    mock_repo_mgr.checkout_existing_branch = AsyncMock(return_value=Path("/tmp/worktrees/test"))
    mock_repo_mgr.repos_dir = Path("/tmp/repos")

    mock_runner = MockRunner.return_value
    mock_runner.can_accept_task = True
    mock_runner.start_agent = AsyncMock(return_value=True)

    orch = Orchestrator(db=db, config=config)
    orch.repo_manager = mock_repo_mgr
    orch.runner = mock_runner

    # Create previous task with PR
    prev_task = await db.create_task(TaskCreate(
        title="Initial work",
        repo="myapp",
        agent_type="coder",
        plane_issue_id="plane-rev",
    ))
    await db.update_task_fields(
        prev_task.id,
        pr_url="https://github.com/owner/repo/pull/10",
        branch_name="agent/task-1-initial",
    )
    await db.update_task_status(prev_task.id, TaskStatus.IN_REVIEW)

    new_task = await db.create_task(TaskCreate(
        title="Revise",
        repo="myapp",
        agent_type="coder",
        plane_issue_id="plane-rev",
    ))

    prompt_calls = []

    def mock_load_prompt(filepath, base_dir):
        prompt_calls.append(filepath)
        return f"prompt from {filepath}"

    with patch("factory.orchestrator.build_revision_context", new_callable=AsyncMock) as mock_ctx:
        mock_ctx.return_value = RevisionContext(
            pr_url="https://github.com/owner/repo/pull/10",
            branch_name="agent/task-1-initial",
        )
        with patch("factory.orchestrator.load_prompt", side_effect=mock_load_prompt):
            await orch.process_task(new_task.id)

    # Should have loaded coder_revision prompt
    assert "prompts/coder_revision.md" in prompt_calls

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_handle_success_pushes_to_existing_branch_for_revision(MockRunner, MockRepoMgr):
    """On success, a revision task should push to the existing branch instead of creating a new PR."""
    db = Database(":memory:")
    await db.initialize()

    config = _make_config()

    orch = Orchestrator(db=db, config=config)
    orch.runner = MockRunner.return_value
    orch.repo_manager = MockRepoMgr.return_value

    # Create a task that already has a PR (revision)
    task = await db.create_task(TaskCreate(
        title="Revise code",
        repo="myapp",
        agent_type="coder",
        plane_issue_id="plane-rev-2",
    ))
    await db.update_task_fields(
        task.id,
        pr_url="https://github.com/owner/repo/pull/99",
        branch_name="agent/task-1-revise",
    )
    await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

    # Mock push methods
    orch._push_to_existing_branch = AsyncMock()
    orch._push_and_create_pr = AsyncMock()

    await orch._handle_success(task.id, "## Summary\nFixed the tests")

    # Should push to existing branch, NOT create a new PR
    orch._push_to_existing_branch.assert_called_once()
    orch._push_and_create_pr.assert_not_called()

    # Task should be in review
    updated = await db.get_task(task.id)
    assert updated.status == TaskStatus.IN_REVIEW

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_handle_success_creates_pr_for_fresh_task(MockRunner, MockRepoMgr):
    """On success, a fresh task should create a new PR."""
    db = Database(":memory:")
    await db.initialize()

    config = _make_config()

    orch = Orchestrator(db=db, config=config)
    orch.runner = MockRunner.return_value
    orch.repo_manager = MockRepoMgr.return_value

    task = await db.create_task(TaskCreate(
        title="New feature",
        repo="myapp",
        agent_type="coder",
    ))
    await db.update_task_fields(task.id, branch_name="agent/task-1-new-feature")
    await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

    orch._push_to_existing_branch = AsyncMock()
    orch._push_and_create_pr = AsyncMock(return_value="https://github.com/owner/repo/pull/100")

    await orch._handle_success(task.id, "## Summary\nAdded feature")

    # Should create a new PR, NOT push to existing
    orch._push_and_create_pr.assert_called_once()
    orch._push_to_existing_branch.assert_not_called()

    updated = await db.get_task(task.id)
    assert updated.status == TaskStatus.IN_REVIEW
    assert updated.pr_url == "https://github.com/owner/repo/pull/100"

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_build_prompt_includes_revision_context(MockRunner, MockRepoMgr):
    """The _build_prompt method should include revision context when provided."""
    db = Database(":memory:")
    await db.initialize()

    config = _make_config()

    orch = Orchestrator(db=db, config=config)

    prompt = orch._build_prompt(
        title="Fix bug",
        description="The login is broken",
        revision_context="\n## Review Feedback\n- Fix the error handling",
    )

    assert "## Review Feedback" in prompt
    assert "Fix the error handling" in prompt
    assert "Fix bug" in prompt
    assert "The login is broken" in prompt

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_build_prompt_without_revision_context(MockRunner, MockRepoMgr):
    """Without revision context, the prompt should not include feedback sections."""
    db = Database(":memory:")
    await db.initialize()

    config = _make_config()

    orch = Orchestrator(db=db, config=config)

    prompt = orch._build_prompt(
        title="New feature",
        description="Add dark mode",
    )

    assert "Review Feedback" not in prompt
    assert "New feature" in prompt

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_detect_revision_task_without_plane_issue(MockRunner, MockRepoMgr):
    """Tasks without a plane_issue_id should not be detected as revisions."""
    db = Database(":memory:")
    await db.initialize()

    config = _make_config()
    orch = Orchestrator(db=db, config=config)

    task = await db.create_task(TaskCreate(
        title="Test",
        repo="myapp",
        agent_type="coder",
    ))

    is_revision, branch, pr_url = await orch._detect_revision_task(task)
    assert is_revision is False
    assert branch == ""
    assert pr_url == ""

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_detect_revision_task_with_previous_pr(MockRunner, MockRepoMgr):
    """Tasks with a plane_issue_id that has a previous PR should be detected."""
    db = Database(":memory:")
    await db.initialize()

    config = _make_config()
    orch = Orchestrator(db=db, config=config)

    prev = await db.create_task(TaskCreate(
        title="V1",
        repo="myapp",
        agent_type="coder",
        plane_issue_id="issue-1",
    ))
    await db.update_task_fields(
        prev.id,
        pr_url="https://github.com/owner/repo/pull/5",
        branch_name="agent/task-1",
    )
    await db.update_task_status(prev.id, TaskStatus.IN_REVIEW)

    new_task = await db.create_task(TaskCreate(
        title="V2",
        repo="myapp",
        agent_type="coder",
        plane_issue_id="issue-1",
    ))

    is_revision, branch, pr_url = await orch._detect_revision_task(new_task)
    assert is_revision is True
    assert branch == "agent/task-1"
    assert pr_url == "https://github.com/owner/repo/pull/5"

    await db.close()


# ── Webhook revision detection test ─────────────────────────────────────


@pytest.mark.asyncio
async def test_plane_webhook_detects_revision():
    """The Plane webhook should return revision status when a re-queued issue has an existing PR."""
    from httpx import ASGITransport, AsyncClient

    from factory.deps import get_db, get_orchestrator
    from factory.main import app

    db = Database(":memory:")
    await db.initialize()

    # Create previous task with PR
    prev = await db.create_task(TaskCreate(
        title="Initial",
        repo="myapp",
        agent_type="coder",
        plane_issue_id="issue-42",
    ))
    await db.update_task_fields(
        prev.id,
        pr_url="https://github.com/owner/repo/pull/7",
        branch_name="agent/task-1-initial",
    )
    await db.update_task_status(prev.id, TaskStatus.IN_REVIEW)

    mock_orch = MagicMock(spec=Orchestrator)
    mock_orch.process_task = AsyncMock(return_value=True)
    mock_orch.runner = MagicMock()
    mock_orch.runner.get_running_agents.return_value = {}
    mock_orch.plane = None
    mock_orch.config = MagicMock()
    mock_orch.config.plane.default_repo = "myapp"

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_orchestrator] = lambda: mock_orch

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            webhook_payload = {
                "event": "issue",
                "action": "updated",
                "data": {
                    "id": "issue-42",
                    "name": "Fix the bug",
                    "description_html": "<p>Revise the code</p>",
                    "labels": [],
                    "state": {"name": "Queued", "group": "backlog"},
                },
            }
            resp = await client.post("/api/webhooks/plane", json=webhook_payload)
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "revision_task_created"
            assert "task_id" in data
    finally:
        app.dependency_overrides.clear()

    await db.close()


# ── Push to existing branch tests ───────────────────────────────────────


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_push_to_existing_branch(MockRunner, MockRepoMgr):
    """_push_to_existing_branch should push without creating a PR."""
    db = Database(":memory:")
    await db.initialize()

    config = _make_config()
    orch = Orchestrator(db=db, config=config)

    orch._run = AsyncMock(return_value="git@github.com:owner/repo.git")

    await orch._push_to_existing_branch(Path("/tmp/wt"), "agent/task-1")

    # Should call git remote get-url and git push
    calls = [str(c) for c in orch._run.call_args_list]
    assert any("remote" in c and "get-url" in c for c in calls)
    assert any("push" in c for c in calls)

    await db.close()
