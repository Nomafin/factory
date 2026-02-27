"""Tests for coder-reviewer collaboration workflow with automatic iteration."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from factory.config import (
    AgentTemplateConfig, Config, RepoConfig, WorkflowConfig, WorkflowStepConfig,
)
from factory.db import Database
from factory.deps import get_db, get_orchestrator
from factory.main import app
from factory.models import (
    CodeReviewCreate, ReviewIssue, ReviewResult, TaskCreate, TaskStatus,
    Workflow, WorkflowCreate, WorkflowStatus,
)
from factory.orchestrator import (
    Orchestrator, parse_review_output, _has_review_issues, _output_indicates_unable,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_config(**overrides) -> Config:
    """Create a Config with a code_review workflow that supports looping."""
    defaults = dict(
        repos={"myapp": RepoConfig(url="git@github.com:user/myapp.git")},
        agent_templates={
            "coder": AgentTemplateConfig(
                system_prompt_file="prompts/coder.md",
                allowed_tools=["Read", "Edit", "Bash"],
            ),
            "reviewer": AgentTemplateConfig(
                system_prompt_file="prompts/reviewer.md",
                allowed_tools=["Read", "Glob", "Grep"],
            ),
        },
        workflows={
            "code_review": WorkflowConfig(
                max_iterations=3,
                steps=[
                    WorkflowStepConfig(agent="coder", output="code_changes"),
                    WorkflowStepConfig(
                        agent="reviewer", input="code_changes",
                        output="review_feedback",
                    ),
                    WorkflowStepConfig(
                        agent="coder", input="review_feedback",
                        condition="has_issues",
                        loop_to="review_feedback",
                    ),
                ],
            ),
        },
    )
    defaults.update(overrides)
    return Config(**defaults)


async def _make_orchestrator(db: Database, config: Config | None = None):
    """Create an Orchestrator with mocked runner and repo manager."""
    config = config or _make_config()
    orch = Orchestrator(db=db, config=config)
    orch.plane = None
    orch.notifier = None

    mock_runner = MagicMock()
    mock_runner.can_accept_task = True
    mock_runner.start_agent = AsyncMock(return_value=True)
    mock_runner.cancel_agent = AsyncMock(return_value=True)
    mock_runner.get_running_agents = MagicMock(return_value={})
    orch.runner = mock_runner

    mock_repo_mgr = MagicMock()
    mock_repo_mgr.ensure_repo = AsyncMock(return_value=Path("/tmp/repos/myapp"))
    mock_repo_mgr.create_worktree = AsyncMock(return_value=Path("/tmp/worktrees/test"))
    orch.repo_manager = mock_repo_mgr

    return orch


# ── ReviewResult model tests ─────────────────────────────────────────────


class TestReviewResult:
    """Tests for the ReviewResult structured output model."""

    def test_review_result_approved(self):
        review = ReviewResult(approved=True, summary="LGTM")
        assert review.approved is True
        assert review.has_blockers_or_majors is False

    def test_review_result_with_blocker(self):
        review = ReviewResult(
            approved=False,
            summary="Found issues",
            issues=[
                ReviewIssue(
                    severity="blocker",
                    description="SQL injection vulnerability",
                    file="api.py",
                    line=42,
                    suggestion="Use parameterized queries",
                ),
            ],
        )
        assert review.approved is False
        assert review.has_blockers_or_majors is True
        assert review.blocker_count == 1
        assert review.major_count == 0

    def test_review_result_with_major(self):
        review = ReviewResult(
            approved=False,
            issues=[
                ReviewIssue(severity="major", description="Missing error handling"),
            ],
        )
        assert review.has_blockers_or_majors is True
        assert review.major_count == 1

    def test_review_result_minor_only(self):
        review = ReviewResult(
            approved=True,
            issues=[
                ReviewIssue(severity="minor", description="Could use better variable name"),
                ReviewIssue(severity="nit", description="Extra whitespace"),
            ],
        )
        assert review.has_blockers_or_majors is False
        assert review.minor_count == 1
        assert review.nit_count == 1

    def test_review_result_empty(self):
        review = ReviewResult(approved=True)
        assert review.has_blockers_or_majors is False
        assert review.blocker_count == 0
        assert review.major_count == 0
        assert review.minor_count == 0
        assert review.nit_count == 0

    def test_review_result_mixed_severities(self):
        review = ReviewResult(
            approved=False,
            issues=[
                ReviewIssue(severity="blocker", description="Security issue"),
                ReviewIssue(severity="major", description="Bug"),
                ReviewIssue(severity="minor", description="Style"),
                ReviewIssue(severity="nit", description="Formatting"),
            ],
        )
        assert review.blocker_count == 1
        assert review.major_count == 1
        assert review.minor_count == 1
        assert review.nit_count == 1
        assert review.has_blockers_or_majors is True


# ── parse_review_output tests ────────────────────────────────────────────


class TestParseReviewOutput:
    """Tests for parsing structured review JSON from agent output."""

    def test_parse_fenced_json_block(self):
        output = """Here's my review:

```json
{
  "approved": false,
  "summary": "Found security issues",
  "issues": [
    {
      "severity": "blocker",
      "description": "SQL injection in query builder",
      "file": "db.py",
      "line": 55,
      "suggestion": "Use parameterized queries"
    }
  ],
  "suggestions": ["Consider adding input validation"]
}
```

Please fix the above issues."""

        review = parse_review_output(output)
        assert review is not None
        assert review.approved is False
        assert review.summary == "Found security issues"
        assert len(review.issues) == 1
        assert review.issues[0].severity == "blocker"
        assert review.issues[0].file == "db.py"
        assert review.issues[0].line == 55
        assert len(review.suggestions) == 1

    def test_parse_approved_review(self):
        output = """Code looks great!

```json
{
  "approved": true,
  "summary": "Well-structured code with good test coverage",
  "issues": [],
  "suggestions": ["Could add more docstrings"]
}
```"""

        review = parse_review_output(output)
        assert review is not None
        assert review.approved is True
        assert len(review.issues) == 0

    def test_parse_review_with_multiple_issues(self):
        output = """```json
{
  "approved": false,
  "summary": "Several issues found",
  "issues": [
    {"severity": "major", "description": "Missing null check", "file": "handler.py"},
    {"severity": "minor", "description": "Inconsistent naming", "file": "models.py"},
    {"severity": "nit", "description": "Extra blank line", "file": "utils.py"}
  ]
}
```"""

        review = parse_review_output(output)
        assert review is not None
        assert len(review.issues) == 3
        assert review.issues[0].severity == "major"
        assert review.issues[1].severity == "minor"
        assert review.issues[2].severity == "nit"

    def test_parse_raw_json_without_fencing(self):
        output = 'Review result: {"approved": false, "summary": "Bug found", "issues": [{"severity": "major", "description": "Off-by-one error"}]}'

        review = parse_review_output(output)
        assert review is not None
        assert review.approved is False
        assert len(review.issues) == 1

    def test_parse_returns_none_for_no_json(self):
        output = "LGTM! The code looks great, no issues found."
        review = parse_review_output(output)
        assert review is None

    def test_parse_returns_none_for_non_review_json(self):
        output = '```json\n{"type": "clarification_needed", "question": "What framework?"}\n```'
        review = parse_review_output(output)
        assert review is None

    def test_parse_handles_invalid_json(self):
        output = '```json\n{invalid json here}\n```'
        review = parse_review_output(output)
        assert review is None

    def test_parse_normalizes_unknown_severity(self):
        output = """```json
{
  "approved": false,
  "summary": "Issues",
  "issues": [{"severity": "critical", "description": "Bad code"}]
}
```"""
        review = parse_review_output(output)
        assert review is not None
        # Unknown severity "critical" should be normalized to "minor"
        assert review.issues[0].severity == "minor"

    def test_parse_unfenced_code_block(self):
        output = """My review:

```
{
  "approved": true,
  "summary": "All good",
  "issues": []
}
```"""
        review = parse_review_output(output)
        assert review is not None
        assert review.approved is True


# ── Helper function tests ────────────────────────────────────────────────


class TestHelperFunctions:
    """Tests for _has_review_issues and _output_indicates_unable."""

    def test_has_review_issues_true(self):
        assert _has_review_issues("Found an issue with error handling") is True
        assert _has_review_issues("There's a bug in the validation") is True
        assert _has_review_issues("Needs revision of the API layer") is True
        assert _has_review_issues("Change requested for the parser") is True

    def test_has_review_issues_false(self):
        assert _has_review_issues("LGTM! Great code.") is False
        assert _has_review_issues("Everything looks good.") is False
        assert _has_review_issues("Well done, approved.") is False

    def test_output_indicates_unable_true(self):
        assert _output_indicates_unable("Unable to address this feedback") is True
        assert _output_indicates_unable("Cannot fix the suggested change") is True
        assert _output_indicates_unable("This is beyond scope") is True

    def test_output_indicates_unable_false(self):
        assert _output_indicates_unable("Fixed all issues") is False
        assert _output_indicates_unable("Addressed all feedback") is False
        assert _output_indicates_unable("Made the requested changes") is False


# ── Config tests ─────────────────────────────────────────────────────────


class TestCodeReviewConfig:
    """Tests for code_review workflow configuration."""

    def test_config_with_max_iterations(self):
        config = _make_config()
        wf = config.workflows["code_review"]
        assert wf.max_iterations == 3

    def test_config_with_loop_to(self):
        config = _make_config()
        wf = config.workflows["code_review"]
        assert wf.steps[2].loop_to == "review_feedback"

    def test_config_default_max_iterations(self):
        wf = WorkflowConfig(steps=[
            WorkflowStepConfig(agent="coder"),
        ])
        assert wf.max_iterations == 3

    def test_config_custom_max_iterations(self):
        wf = WorkflowConfig(
            max_iterations=5,
            steps=[WorkflowStepConfig(agent="coder")],
        )
        assert wf.max_iterations == 5


# ── Database tests ───────────────────────────────────────────────────────


class TestDatabaseCodeReview:
    """Tests for database operations supporting code review workflows."""

    async def test_create_workflow_with_max_iterations(self):
        db = Database(":memory:")
        await db.initialize()

        wf = await db.create_workflow(
            name="code_review", title="Review PR",
            repo="myapp", max_iterations=5,
        )
        assert wf.max_iterations == 5
        assert wf.iteration == 0

        await db.close()

    async def test_update_workflow_iteration(self):
        db = Database(":memory:")
        await db.initialize()

        wf = await db.create_workflow(
            name="code_review", title="Review PR", repo="myapp",
        )
        updated = await db.update_workflow_fields(wf.id, iteration=2)
        assert updated.iteration == 2

        await db.close()

    async def test_create_step_with_loop_to(self):
        db = Database(":memory:")
        await db.initialize()

        wf = await db.create_workflow(name="test", title="Test", repo="myapp")
        step = await db.create_workflow_step(
            workflow_id=wf.id, step_index=2, agent_type="coder",
            input_key="review_feedback", condition="has_issues",
            loop_to="review_feedback",
        )
        assert step.loop_to == "review_feedback"

        await db.close()

    async def test_workflow_default_max_iterations(self):
        db = Database(":memory:")
        await db.initialize()

        wf = await db.create_workflow(name="test", title="Test", repo="myapp")
        assert wf.max_iterations == 3

        await db.close()


# ── Orchestrator loop logic tests ────────────────────────────────────────


class TestWorkflowLooping:
    """Tests for the review-revision iteration loop."""

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_loop_on_blocker_issues(self, mock_prompt):
        """Revision step should loop back to review when blockers found."""
        db = Database(":memory:")
        await db.initialize()

        orch = await _make_orchestrator(db)
        wf = await orch.start_workflow(
            workflow_name="code_review",
            title="Review feature",
            repo="myapp",
        )

        # Step 0 (coder) completes
        await orch._advance_workflow(wf.id, 0, "Initial code written")

        # Step 1 (reviewer) completes with structured blocker
        review_json = json.dumps({
            "approved": False,
            "summary": "Security issue found",
            "issues": [
                {"severity": "blocker", "description": "SQL injection"},
            ],
        })
        review_output = f"Review:\n```json\n{review_json}\n```"
        await orch._advance_workflow(wf.id, 1, review_output)

        # Step 2 (coder revision) should run because has_issues is True
        wf_after = await db.get_workflow(wf.id)
        assert wf_after.steps[2].status == "running"

        # Step 2 completes - the revision was made
        revision_output = "Fixed the SQL injection by using parameterized queries"
        await orch._advance_workflow(wf.id, 2, revision_output)

        # Should have looped - iteration incremented
        wf_looped = await db.get_workflow(wf.id)
        assert wf_looped.iteration == 1

        # Review step (step 1) should be running again
        assert wf_looped.steps[1].status == "running"

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_no_loop_when_approved(self, mock_prompt):
        """Should NOT loop when reviewer approves the code."""
        db = Database(":memory:")
        await db.initialize()

        orch = await _make_orchestrator(db)
        wf = await orch.start_workflow(
            workflow_name="code_review",
            title="Review feature",
            repo="myapp",
        )

        # Step 0 (coder) completes
        await orch._advance_workflow(wf.id, 0, "Code changes done")

        # Step 1 (reviewer) approves
        review_output = '```json\n{"approved": true, "summary": "LGTM", "issues": []}\n```'
        await orch._advance_workflow(wf.id, 1, review_output)

        # Step 2 should be skipped (no_issues means condition fails)
        wf_after = await db.get_workflow(wf.id)
        assert wf_after.steps[2].status == "skipped"
        assert wf_after.status == WorkflowStatus.COMPLETED

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_no_loop_on_minor_only(self, mock_prompt):
        """Should NOT loop when only minor/nit issues (approved with suggestions)."""
        db = Database(":memory:")
        await db.initialize()

        orch = await _make_orchestrator(db)
        wf = await orch.start_workflow(
            workflow_name="code_review",
            title="Review feature",
            repo="myapp",
        )

        await orch._advance_workflow(wf.id, 0, "Code done")

        # Reviewer approves with minor issues
        review_output = """```json
{
    "approved": true,
    "summary": "Mostly good, minor suggestions",
    "issues": [
        {"severity": "minor", "description": "Could rename variable"},
        {"severity": "nit", "description": "Extra whitespace"}
    ]
}
```"""
        await orch._advance_workflow(wf.id, 1, review_output)

        wf_after = await db.get_workflow(wf.id)
        assert wf_after.steps[2].status == "skipped"
        assert wf_after.status == WorkflowStatus.COMPLETED

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_max_iterations_stops_loop(self, mock_prompt):
        """Should stop looping when max_iterations is reached."""
        db = Database(":memory:")
        await db.initialize()

        # Use max_iterations=1 for quick testing
        config = _make_config(
            workflows={
                "code_review": WorkflowConfig(
                    max_iterations=1,
                    steps=[
                        WorkflowStepConfig(agent="coder", output="code_changes"),
                        WorkflowStepConfig(
                            agent="reviewer", input="code_changes",
                            output="review_feedback",
                        ),
                        WorkflowStepConfig(
                            agent="coder", input="review_feedback",
                            condition="has_issues",
                            loop_to="review_feedback",
                        ),
                    ],
                ),
            },
        )
        orch = await _make_orchestrator(db, config)
        wf = await orch.start_workflow(
            workflow_name="code_review",
            title="Review feature",
            repo="myapp",
        )

        # Step 0 (coder) completes
        await orch._advance_workflow(wf.id, 0, "Code done")

        # Step 1 (reviewer) finds issues
        review_output = '```json\n{"approved": false, "summary": "Issues found", "issues": [{"severity": "blocker", "description": "Bug"}]}\n```'
        await orch._advance_workflow(wf.id, 1, review_output)

        # Step 2 (revision) runs
        wf_mid = await db.get_workflow(wf.id)
        assert wf_mid.steps[2].status == "running"

        # Step 2 completes - would normally loop but max_iterations=1
        await orch._advance_workflow(wf.id, 2, "Fixed the bug")

        # Should NOT loop - max iterations reached (iteration 0 -> would be 1 >= max_iterations=1)
        wf_final = await db.get_workflow(wf.id)
        assert wf_final.status == WorkflowStatus.COMPLETED
        assert wf_final.iteration == 0  # Did not increment because we didn't loop

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_coder_unable_stops_loop(self, mock_prompt):
        """Should stop looping when coder reports inability to fix."""
        db = Database(":memory:")
        await db.initialize()

        orch = await _make_orchestrator(db)
        wf = await orch.start_workflow(
            workflow_name="code_review",
            title="Review feature",
            repo="myapp",
        )

        await orch._advance_workflow(wf.id, 0, "Code done")

        # Reviewer finds issues
        review_output = '```json\n{"approved": false, "summary": "Issues", "issues": [{"severity": "blocker", "description": "Architecture problem"}]}\n```'
        await orch._advance_workflow(wf.id, 1, review_output)

        # Step 2 runs and coder reports unable to fix
        wf_mid = await db.get_workflow(wf.id)
        assert wf_mid.steps[2].status == "running"

        await orch._advance_workflow(
            wf.id, 2,
            "Unable to address the architecture feedback - this requires a design discussion",
        )

        # Should NOT loop - coder reported unable
        wf_final = await db.get_workflow(wf.id)
        assert wf_final.status == WorkflowStatus.COMPLETED

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_multiple_iterations(self, mock_prompt):
        """Test multiple review-revision iterations before approval."""
        db = Database(":memory:")
        await db.initialize()

        orch = await _make_orchestrator(db)
        wf = await orch.start_workflow(
            workflow_name="code_review",
            title="Complex feature",
            repo="myapp",
        )

        # Iteration 0: coder writes code
        await orch._advance_workflow(wf.id, 0, "Initial implementation")

        # Iteration 0: reviewer finds blockers
        review1 = '```json\n{"approved": false, "summary": "Needs work", "issues": [{"severity": "blocker", "description": "Missing auth"}]}\n```'
        await orch._advance_workflow(wf.id, 1, review1)

        # Revision runs
        wf1 = await db.get_workflow(wf.id)
        assert wf1.steps[2].status == "running"

        # Coder fixes
        await orch._advance_workflow(wf.id, 2, "Added authentication")

        # Looped back - iteration 1
        wf2 = await db.get_workflow(wf.id)
        assert wf2.iteration == 1
        assert wf2.steps[1].status == "running"  # Review again

        # Iteration 1: reviewer finds more issues
        review2 = '```json\n{"approved": false, "summary": "Almost there", "issues": [{"severity": "major", "description": "Edge case not handled"}]}\n```'
        await orch._advance_workflow(wf.id, 1, review2)

        # Revision runs again
        wf3 = await db.get_workflow(wf.id)
        assert wf3.steps[2].status == "running"

        # Coder fixes edge case
        await orch._advance_workflow(wf.id, 2, "Handled edge case")

        # Looped back - iteration 2
        wf4 = await db.get_workflow(wf.id)
        assert wf4.iteration == 2
        assert wf4.steps[1].status == "running"

        # Iteration 2: reviewer approves
        review3 = '```json\n{"approved": true, "summary": "All good now!", "issues": []}\n```'
        await orch._advance_workflow(wf.id, 1, review3)

        # Should complete - approved
        wf_final = await db.get_workflow(wf.id)
        assert wf_final.steps[2].status == "skipped"
        assert wf_final.status == WorkflowStatus.COMPLETED

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_unstructured_review_fallback(self, mock_prompt):
        """Loop should work with unstructured review output (fallback)."""
        db = Database(":memory:")
        await db.initialize()

        orch = await _make_orchestrator(db)
        wf = await orch.start_workflow(
            workflow_name="code_review",
            title="Review feature",
            repo="myapp",
        )

        await orch._advance_workflow(wf.id, 0, "Code written")

        # Unstructured review with issue keywords
        review_output = "Found several issues: Missing error handling in the API endpoint."
        await orch._advance_workflow(wf.id, 1, review_output)

        # Condition should detect issues via fallback
        wf_after = await db.get_workflow(wf.id)
        assert wf_after.steps[2].status == "running"

        await db.close()


# ── Condition evaluation with structured review ──────────────────────────


class TestStructuredConditionEvaluation:
    """Tests for condition evaluation using structured review output."""

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_has_issues_with_structured_blocker(self, mock_prompt):
        db = Database(":memory:")
        await db.initialize()

        orch = await _make_orchestrator(db)
        wf = await db.create_workflow(name="test", title="Test", repo="myapp")
        step = await db.create_workflow_step(
            workflow_id=wf.id, step_index=0, agent_type="reviewer",
            output_key="review",
        )
        review_json = json.dumps({
            "approved": False,
            "summary": "Bad",
            "issues": [{"severity": "blocker", "description": "Security hole"}],
        })
        await db.update_workflow_step_status(
            step.id, "completed",
            output_data=f"```json\n{review_json}\n```",
        )

        result = await orch._evaluate_condition(wf.id, "has_issues")
        assert result is True

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_has_issues_false_with_structured_approval(self, mock_prompt):
        db = Database(":memory:")
        await db.initialize()

        orch = await _make_orchestrator(db)
        wf = await db.create_workflow(name="test", title="Test", repo="myapp")
        step = await db.create_workflow_step(
            workflow_id=wf.id, step_index=0, agent_type="reviewer",
            output_key="review",
        )
        review_json = json.dumps({
            "approved": True,
            "summary": "LGTM",
            "issues": [],
        })
        await db.update_workflow_step_status(
            step.id, "completed",
            output_data=f"```json\n{review_json}\n```",
        )

        result = await orch._evaluate_condition(wf.id, "has_issues")
        assert result is False

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_no_issues_with_structured_approval(self, mock_prompt):
        db = Database(":memory:")
        await db.initialize()

        orch = await _make_orchestrator(db)
        wf = await db.create_workflow(name="test", title="Test", repo="myapp")
        step = await db.create_workflow_step(
            workflow_id=wf.id, step_index=0, agent_type="reviewer",
            output_key="review",
        )
        review_json = json.dumps({
            "approved": True,
            "summary": "LGTM",
            "issues": [],
        })
        await db.update_workflow_step_status(
            step.id, "completed",
            output_data=f"```json\n{review_json}\n```",
        )

        result = await orch._evaluate_condition(wf.id, "no_issues")
        assert result is True

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_review_approved_condition(self, mock_prompt):
        db = Database(":memory:")
        await db.initialize()

        orch = await _make_orchestrator(db)
        wf = await db.create_workflow(name="test", title="Test", repo="myapp")
        step = await db.create_workflow_step(
            workflow_id=wf.id, step_index=0, agent_type="reviewer",
            output_key="review",
        )
        review_json = json.dumps({
            "approved": True,
            "summary": "All good",
            "issues": [{"severity": "nit", "description": "Trailing space"}],
        })
        await db.update_workflow_step_status(
            step.id, "completed",
            output_data=f"```json\n{review_json}\n```",
        )

        result = await orch._evaluate_condition(wf.id, "review_approved")
        assert result is True

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_has_issues_minor_only_structured(self, mock_prompt):
        """Minor-only issues should NOT be treated as has_issues (structured)."""
        db = Database(":memory:")
        await db.initialize()

        orch = await _make_orchestrator(db)
        wf = await db.create_workflow(name="test", title="Test", repo="myapp")
        step = await db.create_workflow_step(
            workflow_id=wf.id, step_index=0, agent_type="reviewer",
            output_key="review",
        )
        review_json = json.dumps({
            "approved": True,
            "summary": "Minor stuff only",
            "issues": [{"severity": "minor", "description": "Naming"}],
        })
        await db.update_workflow_step_status(
            step.id, "completed",
            output_data=f"```json\n{review_json}\n```",
        )

        result = await orch._evaluate_condition(wf.id, "has_issues")
        assert result is False

        await db.close()


# ── _should_loop tests ───────────────────────────────────────────────────


class TestShouldLoop:
    """Tests for the _should_loop determination.

    _should_loop is called after the revision step completes. It always
    loops back unless max iterations reached or coder reports unable.
    The review quality check happens in the condition evaluation, not here.
    """

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_should_loop_normal_revision(self, mock_prompt):
        """Normal revision output should trigger a loop."""
        db = Database(":memory:")
        await db.initialize()
        orch = await _make_orchestrator(db)

        wf = await db.create_workflow(
            name="code_review", title="Test", repo="myapp", max_iterations=3,
        )

        output = "Fixed the SQL injection by using parameterized queries"
        result = await orch._should_loop(wf, output)
        assert result is True

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_should_not_loop_at_max_iterations(self, mock_prompt):
        db = Database(":memory:")
        await db.initialize()
        orch = await _make_orchestrator(db)

        wf = await db.create_workflow(
            name="code_review", title="Test", repo="myapp", max_iterations=2,
        )
        # Simulate being at iteration 2 (>= max of 2)
        await db.update_workflow_fields(wf.id, iteration=2)
        wf = await db.get_workflow(wf.id)

        output = "Fixed all the issues"
        result = await orch._should_loop(wf, output)
        assert result is False

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_should_not_loop_when_unable(self, mock_prompt):
        db = Database(":memory:")
        await db.initialize()
        orch = await _make_orchestrator(db)

        wf = await db.create_workflow(
            name="code_review", title="Test", repo="myapp", max_iterations=3,
        )

        output = "Unable to address the architectural concerns raised in review"
        result = await orch._should_loop(wf, output)
        assert result is False

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_should_loop_at_first_iteration(self, mock_prompt):
        """First iteration should always loop."""
        db = Database(":memory:")
        await db.initialize()
        orch = await _make_orchestrator(db)

        wf = await db.create_workflow(
            name="code_review", title="Test", repo="myapp", max_iterations=3,
        )

        output = "Made the requested changes"
        result = await orch._should_loop(wf, output)
        assert result is True

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_should_loop_just_under_max(self, mock_prompt):
        """Should loop when iteration + 1 < max_iterations."""
        db = Database(":memory:")
        await db.initialize()
        orch = await _make_orchestrator(db)

        wf = await db.create_workflow(
            name="code_review", title="Test", repo="myapp", max_iterations=3,
        )
        await db.update_workflow_fields(wf.id, iteration=1)
        wf = await db.get_workflow(wf.id)

        output = "Fixed everything"
        result = await orch._should_loop(wf, output)
        assert result is True

        await db.close()


# ── _find_step_by_name tests ─────────────────────────────────────────────


class TestFindStepByName:
    """Tests for finding a step index by name."""

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_find_by_output_key(self, mock_prompt):
        db = Database(":memory:")
        await db.initialize()
        orch = await _make_orchestrator(db)

        wf = await orch.start_workflow(
            workflow_name="code_review",
            title="Test",
            repo="myapp",
        )

        # "review_feedback" is the output_key of step 1
        idx = orch._find_step_by_name(wf, "review_feedback")
        assert idx == 1

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_find_by_numeric_index(self, mock_prompt):
        db = Database(":memory:")
        await db.initialize()
        orch = await _make_orchestrator(db)

        wf = await orch.start_workflow(
            workflow_name="code_review",
            title="Test",
            repo="myapp",
        )

        idx = orch._find_step_by_name(wf, "1")
        assert idx == 1

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_find_nonexistent_returns_none(self, mock_prompt):
        db = Database(":memory:")
        await db.initialize()
        orch = await _make_orchestrator(db)

        wf = await orch.start_workflow(
            workflow_name="code_review",
            title="Test",
            repo="myapp",
        )

        idx = orch._find_step_by_name(wf, "nonexistent_step")
        assert idx is None

        await db.close()


# ── API tests ────────────────────────────────────────────────────────────


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def mock_orchestrator(db):
    orch = MagicMock(spec=Orchestrator)
    orch.cancel_task = AsyncMock()
    orch.process_task = AsyncMock(return_value=True)
    orch.resume_task = AsyncMock(return_value=True)
    orch.runner = MagicMock()
    orch.runner.get_running_agents.return_value = {}
    orch.plane = None
    orch.config = MagicMock()
    orch.config.plane.default_repo = "factory"
    orch.config.workflows = {
        "code_review": WorkflowConfig(
            max_iterations=3,
            steps=[
                WorkflowStepConfig(agent="coder", output="code_changes"),
                WorkflowStepConfig(
                    agent="reviewer", input="code_changes",
                    output="review_feedback",
                ),
                WorkflowStepConfig(
                    agent="coder", input="review_feedback",
                    condition="has_issues",
                    loop_to="review_feedback",
                ),
            ],
        ),
    }
    return orch


@pytest.fixture
async def client(db, mock_orchestrator):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_api_create_code_review_workflow(client, db, mock_orchestrator):
    """POST /api/workflows/code_review creates and starts a code_review workflow."""
    async def mock_start_workflow(**kwargs):
        wf = await db.create_workflow(
            name="code_review",
            title=kwargs["title"],
            repo=kwargs.get("repo", ""),
        )
        return await db.update_workflow_status(wf.id, WorkflowStatus.RUNNING)

    mock_orchestrator.start_workflow = AsyncMock(side_effect=mock_start_workflow)

    resp = await client.post("/api/workflows/code_review", json={
        "title": "Implement auth feature",
        "description": "Add JWT authentication to the API",
        "repo": "myapp",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "code_review"
    assert data["title"] == "Implement auth feature"
    assert data["status"] == "running"


async def test_api_code_review_default_repo(client, db, mock_orchestrator):
    """POST /api/workflows/code_review uses default repo when not specified."""
    async def mock_start_workflow(**kwargs):
        wf = await db.create_workflow(
            name="code_review",
            title=kwargs["title"],
            repo=kwargs.get("repo", ""),
        )
        return await db.update_workflow_status(wf.id, WorkflowStatus.RUNNING)

    mock_orchestrator.start_workflow = AsyncMock(side_effect=mock_start_workflow)

    resp = await client.post("/api/workflows/code_review", json={
        "title": "Fix bug",
    })
    assert resp.status_code == 201
    # Repo should be set from default
    mock_orchestrator.start_workflow.assert_called_once()
    call_kwargs = mock_orchestrator.start_workflow.call_args[1]
    assert call_kwargs["repo"] == "factory"


async def test_api_code_review_not_configured(client, mock_orchestrator):
    """POST /api/workflows/code_review returns 400 when not configured."""
    mock_orchestrator.config.workflows = {}

    resp = await client.post("/api/workflows/code_review", json={
        "title": "Should fail",
    })
    assert resp.status_code == 400
    assert "not configured" in resp.json()["detail"]


async def test_api_code_review_start_failure(client, mock_orchestrator):
    """POST /api/workflows/code_review returns 503 when start fails."""
    mock_orchestrator.start_workflow = AsyncMock(return_value=None)

    resp = await client.post("/api/workflows/code_review", json={
        "title": "Should fail",
        "repo": "myapp",
    })
    assert resp.status_code == 503


async def test_api_code_review_returns_workflow_id(client, db, mock_orchestrator):
    """POST /api/workflows/code_review returns workflow_id for tracking."""
    async def mock_start_workflow(**kwargs):
        wf = await db.create_workflow(
            name="code_review",
            title=kwargs["title"],
            repo=kwargs.get("repo", ""),
        )
        return await db.update_workflow_status(wf.id, WorkflowStatus.RUNNING)

    mock_orchestrator.start_workflow = AsyncMock(side_effect=mock_start_workflow)

    resp = await client.post("/api/workflows/code_review", json={
        "title": "Track this",
        "repo": "myapp",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert isinstance(data["id"], int)


async def test_api_workflow_response_includes_iteration(client, db):
    """GET /api/workflows/{id} includes iteration and max_iterations."""
    wf = await db.create_workflow(
        name="code_review", title="Test", repo="myapp", max_iterations=5,
    )
    await db.update_workflow_fields(wf.id, iteration=2)

    resp = await client.get(f"/api/workflows/{wf.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["iteration"] == 2
    assert data["max_iterations"] == 5


async def test_api_workflow_step_includes_loop_to(client, db):
    """Workflow step response includes loop_to field."""
    wf = await db.create_workflow(name="code_review", title="Test", repo="myapp")
    await db.create_workflow_step(
        workflow_id=wf.id, step_index=2, agent_type="coder",
        condition="has_issues", loop_to="review_feedback",
    )

    resp = await client.get(f"/api/workflows/{wf.id}")
    data = resp.json()
    assert len(data["steps"]) == 1
    assert data["steps"][0]["loop_to"] == "review_feedback"


# ── Integration-style tests ──────────────────────────────────────────────


class TestFullCodeReviewWorkflow:
    """Integration tests for the full code_review workflow lifecycle."""

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_full_workflow_approve_first_time(self, mock_prompt):
        """Full workflow: coder -> reviewer approves -> done (no revision needed)."""
        db = Database(":memory:")
        await db.initialize()

        orch = await _make_orchestrator(db)
        wf = await orch.start_workflow(
            workflow_name="code_review",
            title="Add dark mode",
            description="Implement dark mode toggle in settings",
            repo="myapp",
        )

        assert wf.status == WorkflowStatus.RUNNING
        assert wf.steps[0].status == "running"

        # Coder finishes
        await orch._advance_workflow(wf.id, 0, "Added dark mode with CSS variables")

        # Reviewer approves
        await orch._advance_workflow(wf.id, 1, """Great implementation!

```json
{"approved": true, "summary": "Clean implementation", "issues": []}
```""")

        final = await db.get_workflow(wf.id)
        assert final.status == WorkflowStatus.COMPLETED
        assert final.iteration == 0

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_full_workflow_one_revision(self, mock_prompt):
        """Full workflow: coder -> reviewer rejects -> revision -> reviewer approves."""
        db = Database(":memory:")
        await db.initialize()

        orch = await _make_orchestrator(db)
        wf = await orch.start_workflow(
            workflow_name="code_review",
            title="Add API endpoint",
            repo="myapp",
        )

        # Coder writes initial code
        await orch._advance_workflow(wf.id, 0, "Created /api/users endpoint")

        # Reviewer finds issues
        await orch._advance_workflow(wf.id, 1, """```json
{
    "approved": false,
    "summary": "Missing validation",
    "issues": [
        {"severity": "major", "description": "No input validation on user data", "file": "api.py", "line": 15}
    ]
}
```""")

        # Revision runs and coder fixes
        wf1 = await db.get_workflow(wf.id)
        assert wf1.steps[2].status == "running"
        revision_task = await db.get_task(wf1.steps[2].task_id)
        assert "review_feedback" in revision_task.description

        await orch._advance_workflow(wf.id, 2, "Added pydantic validation for user input")

        # Looped back for second review
        wf2 = await db.get_workflow(wf.id)
        assert wf2.iteration == 1
        assert wf2.steps[1].status == "running"

        # Second review approves
        await orch._advance_workflow(wf.id, 1, """```json
{"approved": true, "summary": "Validation looks good now", "issues": []}
```""")

        # Final state
        final = await db.get_workflow(wf.id)
        assert final.status == WorkflowStatus.COMPLETED

        await db.close()

    @patch("factory.orchestrator.load_prompt", return_value="system prompt")
    async def test_full_workflow_max_iterations_reached(self, mock_prompt):
        """Full workflow: keeps iterating until max_iterations hit."""
        db = Database(":memory:")
        await db.initialize()

        config = _make_config(
            workflows={
                "code_review": WorkflowConfig(
                    max_iterations=2,
                    steps=[
                        WorkflowStepConfig(agent="coder", output="code_changes"),
                        WorkflowStepConfig(
                            agent="reviewer", input="code_changes",
                            output="review_feedback",
                        ),
                        WorkflowStepConfig(
                            agent="coder", input="review_feedback",
                            condition="has_issues",
                            loop_to="review_feedback",
                        ),
                    ],
                ),
            },
        )
        orch = await _make_orchestrator(db, config)
        wf = await orch.start_workflow(
            workflow_name="code_review",
            title="Tricky feature",
            repo="myapp",
        )

        # Iteration 0
        await orch._advance_workflow(wf.id, 0, "Initial code")
        await orch._advance_workflow(wf.id, 1, '```json\n{"approved": false, "summary": "Issues", "issues": [{"severity": "blocker", "description": "Bug 1"}]}\n```')
        await orch._advance_workflow(wf.id, 2, "Fixed bug 1")

        # Iteration 1
        wf1 = await db.get_workflow(wf.id)
        assert wf1.iteration == 1
        await orch._advance_workflow(wf.id, 1, '```json\n{"approved": false, "summary": "Still issues", "issues": [{"severity": "blocker", "description": "Bug 2"}]}\n```')
        await orch._advance_workflow(wf.id, 2, "Fixed bug 2")

        # Should NOT loop - iteration would be 2 >= max_iterations 2
        final = await db.get_workflow(wf.id)
        assert final.status == WorkflowStatus.COMPLETED

        await db.close()
