"""Tests for agent context handoff mechanism."""

import json
from datetime import datetime
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
    AgentHandoff, HandoffCreate, HANDOFF_OUTPUT_TYPES,
    TaskCreate, TaskStatus, WorkflowStatus,
)
from factory.orchestrator import (
    HANDOFF_INJECT_LIMIT, HANDOFF_MAX_CONTENT, HANDOFF_SUMMARY_LIMIT,
    Orchestrator,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_config(**overrides) -> Config:
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
            "code_review": WorkflowConfig(steps=[
                WorkflowStepConfig(agent="coder", output="code_changes"),
                WorkflowStepConfig(agent="reviewer", input="code_changes", output="review_feedback"),
                WorkflowStepConfig(agent="coder", input="review_feedback", condition="has_issues"),
            ]),
        },
    )
    defaults.update(overrides)
    return Config(**defaults)


async def _make_orchestrator(db: Database, config: Config | None = None):
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


# ── Database handoff CRUD tests ─────────────────────────────────────────


async def test_db_create_handoff():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task A", repo="myapp"))
    handoff = await db.create_handoff(HandoffCreate(
        from_task_id=task.id,
        output_type="code_diff",
        content="diff --git a/file.py",
        summary="Changed file.py",
    ))

    assert handoff.id is not None
    assert handoff.from_task_id == task.id
    assert handoff.to_task_id is None
    assert handoff.output_type == "code_diff"
    assert handoff.content == "diff --git a/file.py"
    assert handoff.summary == "Changed file.py"
    assert handoff.created_at is not None

    await db.close()


async def test_db_get_handoff():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task A", repo="myapp"))
    created = await db.create_handoff(HandoffCreate(
        from_task_id=task.id,
        output_type="general",
        content="Some output",
    ))

    fetched = await db.get_handoff(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.content == "Some output"

    # Non-existent returns None
    missing = await db.get_handoff(99999)
    assert missing is None

    await db.close()


async def test_db_handoff_with_workflow():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="WF Task", repo="myapp"))
    wf = await db.create_workflow(name="code_review", title="Review", repo="myapp")

    handoff = await db.create_handoff(HandoffCreate(
        from_task_id=task.id,
        workflow_id=wf.id,
        output_type="code_diff",
        content="diff content",
    ))

    assert handoff.workflow_id == wf.id

    await db.close()


async def test_db_get_handoffs_for_task():
    db = Database(":memory:")
    await db.initialize()

    task_a = await db.create_task(TaskCreate(title="Task A", repo="myapp"))
    task_b = await db.create_task(TaskCreate(title="Task B", repo="myapp"))

    await db.create_handoff(HandoffCreate(
        from_task_id=task_a.id,
        to_task_id=task_b.id,
        output_type="code_diff",
        content="diff 1",
    ))
    await db.create_handoff(HandoffCreate(
        from_task_id=task_a.id,
        to_task_id=task_b.id,
        output_type="review_comments",
        content="review 1",
    ))

    inputs = await db.get_handoffs_for_task(task_b.id)
    assert len(inputs) == 2
    assert inputs[0].output_type == "code_diff"
    assert inputs[1].output_type == "review_comments"

    # Task A has no inputs
    no_inputs = await db.get_handoffs_for_task(task_a.id)
    assert len(no_inputs) == 0

    await db.close()


async def test_db_get_handoffs_from_task():
    db = Database(":memory:")
    await db.initialize()

    task_a = await db.create_task(TaskCreate(title="Task A", repo="myapp"))
    task_b = await db.create_task(TaskCreate(title="Task B", repo="myapp"))

    await db.create_handoff(HandoffCreate(
        from_task_id=task_a.id,
        to_task_id=task_b.id,
        content="output from A",
    ))

    outputs = await db.get_handoffs_from_task(task_a.id)
    assert len(outputs) == 1
    assert outputs[0].content == "output from A"

    # Task B produced nothing
    no_outputs = await db.get_handoffs_from_task(task_b.id)
    assert len(no_outputs) == 0

    await db.close()


async def test_db_get_handoffs_for_workflow():
    db = Database(":memory:")
    await db.initialize()

    wf = await db.create_workflow(name="code_review", title="Review", repo="myapp")
    task_a = await db.create_task(TaskCreate(title="Task A", repo="myapp"))
    task_b = await db.create_task(TaskCreate(title="Task B", repo="myapp"))

    await db.create_handoff(HandoffCreate(
        from_task_id=task_a.id,
        workflow_id=wf.id,
        content="step 1 output",
    ))
    await db.create_handoff(HandoffCreate(
        from_task_id=task_b.id,
        workflow_id=wf.id,
        content="step 2 output",
    ))
    # Handoff in a different workflow
    wf2 = await db.create_workflow(name="other", title="Other", repo="myapp")
    await db.create_handoff(HandoffCreate(
        from_task_id=task_a.id,
        workflow_id=wf2.id,
        content="other workflow",
    ))

    handoffs = await db.get_handoffs_for_workflow(wf.id)
    assert len(handoffs) == 2

    await db.close()


async def test_db_link_handoff_to_task():
    db = Database(":memory:")
    await db.initialize()

    task_a = await db.create_task(TaskCreate(title="Task A", repo="myapp"))
    task_b = await db.create_task(TaskCreate(title="Task B", repo="myapp"))

    handoff = await db.create_handoff(HandoffCreate(
        from_task_id=task_a.id,
        content="will be linked later",
    ))
    assert handoff.to_task_id is None

    linked = await db.link_handoff_to_task(handoff.id, task_b.id)
    assert linked.to_task_id == task_b.id

    # Verify it shows up in task B's inputs
    inputs = await db.get_handoffs_for_task(task_b.id)
    assert len(inputs) == 1
    assert inputs[0].id == handoff.id

    await db.close()


# ── Output type detection tests ──────────────────────────────────────────


def test_detect_output_type_code_diff():
    assert Orchestrator._detect_output_type("diff --git a/foo.py b/foo.py") == "code_diff"
    assert Orchestrator._detect_output_type("--- a/file.py\n+++ b/file.py") == "code_diff"
    assert Orchestrator._detect_output_type("@@ -1,3 +1,5 @@") == "code_diff"


def test_detect_output_type_review_comments():
    assert Orchestrator._detect_output_type("Review comment: needs refactoring") == "review_comments"
    assert Orchestrator._detect_output_type("nit: use a better name") == "review_comments"
    assert Orchestrator._detect_output_type("Suggestion: try using map()") == "review_comments"
    assert Orchestrator._detect_output_type("Change requested for the validation logic") == "review_comments"


def test_detect_output_type_research_notes():
    assert Orchestrator._detect_output_type("Research findings on caching strategies") == "research_notes"
    assert Orchestrator._detect_output_type("Analysis of the performance bottleneck") == "research_notes"
    assert Orchestrator._detect_output_type("Investigation into the memory leak") == "research_notes"


def test_detect_output_type_test_results():
    assert Orchestrator._detect_output_type("Test results: 42 passed, 0 failed") == "test_results"
    assert Orchestrator._detect_output_type("All tests pass") == "test_results"
    assert Orchestrator._detect_output_type("pytest output: PASSED") == "test_results"


def test_detect_output_type_error_report():
    assert Orchestrator._detect_output_type("Error: file not found") == "error_report"
    assert Orchestrator._detect_output_type("Traceback (most recent call last):") == "error_report"
    assert Orchestrator._detect_output_type("Exception occurred during build") == "error_report"


def test_detect_output_type_general():
    assert Orchestrator._detect_output_type("Just some normal output") == "general"
    assert Orchestrator._detect_output_type("Task completed successfully") == "general"


# ── Structured output extraction tests ───────────────────────────────────


def test_extract_structured_output_valid():
    output = 'Some text {"type": "handoff_output", "output_type": "code_diff", "content": "the diff", "summary": "changed files"} more text'
    result = Orchestrator._extract_structured_output(output)
    assert result["output_type"] == "code_diff"
    assert result["content"] == "the diff"
    assert result["summary"] == "changed files"


def test_extract_structured_output_no_match():
    output = "No structured output here"
    result = Orchestrator._extract_structured_output(output)
    assert result == {}


def test_extract_structured_output_wrong_type():
    output = '{"type": "clarification_needed", "question": "What?"}'
    result = Orchestrator._extract_structured_output(output)
    assert result == {}


def test_extract_structured_output_invalid_json():
    output = '{"type": "handoff_output", broken'
    result = Orchestrator._extract_structured_output(output)
    assert result == {}


# ── Summary generation tests ─────────────────────────────────────────────


def test_summarize_short_output():
    """Short output is returned as-is."""
    output = "Short output"
    assert Orchestrator._summarize_output(output) == output


def test_summarize_long_output_with_sections():
    """Long output with ## Summary / ## Changes extracts those sections."""
    output = "x" * 3000 + "\n## Summary\nThis is the summary.\n\n## Changes\n- Changed A\n- Changed B\n"
    result = Orchestrator._summarize_output(output)
    assert "This is the summary" in result
    assert "Changed A" in result


def test_summarize_long_output_truncation():
    """Long output without summary sections is truncated with ellipsis."""
    output = "a" * 5000
    result = Orchestrator._summarize_output(output, limit=100)
    assert len(result) == 100
    assert result.endswith("...")


def test_summarize_respects_limit():
    """Summary never exceeds the limit."""
    output = "x" * 100
    result = Orchestrator._summarize_output(output, limit=50)
    assert len(result) <= 50


# ── Handoff context formatting tests ─────────────────────────────────────


def test_format_handoff_context_empty():
    assert Orchestrator._format_handoff_context([]) == ""


def test_format_handoff_context_single():
    handoff = AgentHandoff(
        id=1, from_task_id=1, output_type="code_diff",
        content="small diff", summary="small diff",
        created_at=datetime.now(),
    )
    result = Orchestrator._format_handoff_context([handoff])
    assert "## Context from previous agent steps" in result
    assert "code_diff" in result
    assert "small diff" in result


def test_format_handoff_context_multiple():
    h1 = AgentHandoff(
        id=1, from_task_id=1, output_type="code_diff",
        content="diff content", summary="diff summary",
        created_at=datetime.now(),
    )
    h2 = AgentHandoff(
        id=2, from_task_id=2, output_type="review_comments",
        content="review content", summary="review summary",
        created_at=datetime.now(),
    )
    result = Orchestrator._format_handoff_context([h1, h2])
    assert "code_diff" in result
    assert "review_comments" in result


def test_format_handoff_context_uses_summary_for_large():
    """Large content should use summary instead of full content."""
    large = "x" * 50000
    handoff = AgentHandoff(
        id=1, from_task_id=1, output_type="general",
        content=large, summary="Short summary",
        created_at=datetime.now(),
    )
    result = Orchestrator._format_handoff_context([handoff], inject_limit=1000)
    assert "Short summary" in result
    assert "x" * 1000 not in result


def test_format_handoff_context_respects_inject_limit():
    """Output should not exceed inject_limit."""
    h = AgentHandoff(
        id=1, from_task_id=1, output_type="general",
        content="a" * 500, summary="a" * 500,
        created_at=datetime.now(),
    )
    result = Orchestrator._format_handoff_context([h], inject_limit=200)
    assert len(result) <= 300  # header + limit


# ── Prompt building with handoff context ─────────────────────────────────


def test_build_prompt_without_handoff():
    """Existing prompt building still works without handoffs."""
    db = MagicMock()
    config = _make_config()
    orch = Orchestrator(db=db, config=config)
    prompt = orch._build_prompt("My task", "Do things")
    assert "My task" in prompt
    assert "Context from previous agent steps" not in prompt


def test_build_prompt_with_handoff_context():
    db = MagicMock()
    config = _make_config()
    orch = Orchestrator(db=db, config=config)
    handoff_ctx = "\n## Context from previous agent steps\n### code_diff (from task #1)\ndiff content"
    prompt = orch._build_prompt("My task", "Do things", handoff_context=handoff_ctx)
    assert "Context from previous agent steps" in prompt
    assert "code_diff" in prompt


def test_build_prompt_handoff_before_clarification():
    """Handoff context should appear before clarification history."""
    db = MagicMock()
    config = _make_config()
    orch = Orchestrator(db=db, config=config)
    handoff_ctx = "\n## Context from previous agent steps\nSome context"
    history = [{"question": "What approach?", "response": "Use caching"}]
    prompt = orch._build_prompt(
        "My task", "Do things",
        handoff_context=handoff_ctx,
        clarification_history=history,
    )
    handoff_pos = prompt.index("Context from previous agent steps")
    clarification_pos = prompt.index("Previous clarifications")
    assert handoff_pos < clarification_pos


# ── Orchestrator integration: handoff creation on success ────────────────


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_handle_success_creates_handoff(mock_prompt):
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)

    # Create and start a standalone task
    task = await db.create_task(TaskCreate(title="Code task", repo="myapp"))
    await db.update_task_fields(task.id, branch_name="agent/task-1-test")

    # Simulate agent success (skip PR creation)
    with patch.object(orch, "_push_and_create_pr", new_callable=AsyncMock, return_value="https://pr.url"):
        await orch._handle_success(task.id, "diff --git a/file.py\n+added line")

    # A handoff should have been created
    handoffs = await db.get_handoffs_from_task(task.id)
    assert len(handoffs) == 1
    assert handoffs[0].output_type == "code_diff"
    assert "diff --git" in handoffs[0].content

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_handle_success_creates_handoff_with_structured_output(mock_prompt):
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    task = await db.create_task(TaskCreate(title="Code task", repo="myapp"))
    await db.update_task_fields(task.id, branch_name="agent/task-1-test")

    structured = json.dumps({
        "type": "handoff_output",
        "output_type": "review_comments",
        "content": "Found issues in auth module",
        "summary": "Auth issues found",
    })
    output = f"Agent finished.\n{structured}\nDone."

    with patch.object(orch, "_push_and_create_pr", new_callable=AsyncMock, return_value=""):
        await orch._handle_success(task.id, output)

    handoffs = await db.get_handoffs_from_task(task.id)
    assert len(handoffs) == 1
    assert handoffs[0].output_type == "review_comments"
    assert handoffs[0].content == "Found issues in auth module"
    assert handoffs[0].summary == "Auth issues found"

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_handoff_created_for_workflow_step(mock_prompt):
    """When a workflow step completes, a handoff is created with the workflow_id."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await orch.start_workflow(
        workflow_name="code_review",
        title="Review feature X",
        repo="myapp",
    )

    task_id = wf.steps[0].task_id

    # Simulate step 0 completing via _handle_success
    await orch._handle_success(task_id, "Here are the code changes")

    # Handoff should be linked to workflow
    handoffs = await db.get_handoffs_from_task(task_id)
    assert len(handoffs) == 1
    assert handoffs[0].workflow_id == wf.id

    await db.close()


# ── Orchestrator integration: handoff injection on task start ────────────


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_process_task_injects_handoff_context(mock_prompt):
    """When a task has linked handoffs, they are injected into the prompt."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)

    # Create two tasks and a handoff between them
    task_a = await db.create_task(TaskCreate(title="Coder task", repo="myapp"))
    task_b = await db.create_task(TaskCreate(title="Reviewer task", repo="myapp"))

    await db.create_handoff(HandoffCreate(
        from_task_id=task_a.id,
        to_task_id=task_b.id,
        output_type="code_diff",
        content="diff --git a/foo.py",
        summary="Changed foo.py",
    ))

    # Start task_b and capture the prompt
    success = await orch.process_task(task_b.id)
    assert success is True

    # The prompt should be passed as a keyword arg
    actual_call = orch.runner.start_agent.call_args
    prompt_used = actual_call.kwargs.get("prompt", "")
    assert "Context from previous agent steps" in prompt_used
    assert "code_diff" in prompt_used

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_process_task_without_handoffs(mock_prompt):
    """A task without handoffs should still work normally."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    task = await db.create_task(TaskCreate(title="Solo task", repo="myapp"))

    success = await orch.process_task(task.id)
    assert success is True

    prompt_used = orch.runner.start_agent.call_args.kwargs.get("prompt", "")
    assert "Context from previous agent steps" not in prompt_used

    await db.close()


# ── Workflow end-to-end handoff flow ─────────────────────────────────────


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_workflow_handoff_end_to_end(mock_prompt):
    """Full workflow: coder -> reviewer -> coder with handoffs at each step."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await orch.start_workflow(
        workflow_name="code_review",
        title="Review feature X",
        repo="myapp",
    )

    # Step 0 (coder) completes
    step0_task_id = wf.steps[0].task_id
    await orch._handle_success(step0_task_id, "diff --git a/feature.py\n+new code")

    # Should have a handoff from step 0
    step0_handoffs = await db.get_handoffs_from_task(step0_task_id)
    assert len(step0_handoffs) == 1
    assert step0_handoffs[0].output_type == "code_diff"

    # Step 1 (reviewer) should now be running
    updated_wf = await db.get_workflow(wf.id)
    step1_task_id = updated_wf.steps[1].task_id
    assert step1_task_id is not None

    # The handoff should be linked to the reviewer task
    step0_handoff = await db.get_handoff(step0_handoffs[0].id)
    assert step0_handoff.to_task_id == step1_task_id

    # Step 1 (reviewer) completes with issues
    await orch._handle_success(
        step1_task_id,
        "Review comment: Found an issue with error handling. Needs revision.",
    )

    step1_handoffs = await db.get_handoffs_from_task(step1_task_id)
    assert len(step1_handoffs) == 1
    assert step1_handoffs[0].output_type == "review_comments"

    # Step 2 (coder revision) should be running
    final_wf = await db.get_workflow(wf.id)
    step2_task_id = final_wf.steps[2].task_id
    assert step2_task_id is not None

    # All workflow handoffs
    all_handoffs = await db.get_handoffs_for_workflow(wf.id)
    assert len(all_handoffs) == 2

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_workflow_skipped_step_no_handoff(mock_prompt):
    """When a step is skipped, no handoff is created for it."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await orch.start_workflow(
        workflow_name="code_review",
        title="Clean code",
        repo="myapp",
    )

    # Step 0 completes
    await orch._handle_success(wf.steps[0].task_id, "Here are the code changes")

    # Step 1 (reviewer) completes with no issues -> step 2 should be skipped
    updated_wf = await db.get_workflow(wf.id)
    await orch._handle_success(
        updated_wf.steps[1].task_id,
        "LGTM! The code looks great, well structured.",
    )

    final_wf = await db.get_workflow(wf.id)
    assert final_wf.steps[2].status == "skipped"
    assert final_wf.status == WorkflowStatus.COMPLETED

    # Only 2 handoffs (step 0 and step 1), none for skipped step
    all_handoffs = await db.get_handoffs_for_workflow(wf.id)
    assert len(all_handoffs) == 2

    await db.close()


# ── Handoff model and output_type validation tests ───────────────────────


def test_handoff_output_types_set():
    """Verify the HANDOFF_OUTPUT_TYPES set contains expected types."""
    assert "code_diff" in HANDOFF_OUTPUT_TYPES
    assert "review_comments" in HANDOFF_OUTPUT_TYPES
    assert "research_notes" in HANDOFF_OUTPUT_TYPES
    assert "test_results" in HANDOFF_OUTPUT_TYPES
    assert "general" in HANDOFF_OUTPUT_TYPES
    assert "error_report" in HANDOFF_OUTPUT_TYPES


def test_handoff_create_defaults():
    h = HandoffCreate(from_task_id=1)
    assert h.output_type == "general"
    assert h.content == ""
    assert h.summary == ""
    assert h.to_task_id is None
    assert h.workflow_id is None


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_create_handoff_from_output_unknown_type_defaults(mock_prompt):
    """Unknown output types should be normalised to 'general'."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    task = await db.create_task(TaskCreate(title="Task", repo="myapp"))

    structured = json.dumps({
        "type": "handoff_output",
        "output_type": "unknown_type_xyz",
        "content": "data",
        "summary": "sum",
    })
    hid = await orch._create_handoff_from_output(task.id, structured)
    handoff = await db.get_handoff(hid)
    assert handoff.output_type == "general"

    await db.close()


# ── API endpoint tests ───────────────────────────────────────────────────


@pytest.fixture
async def api_db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def api_mock_orchestrator(api_db):
    orch = MagicMock(spec=Orchestrator)
    orch.cancel_task = AsyncMock()
    orch.process_task = AsyncMock(return_value=True)
    orch.resume_task = AsyncMock(return_value=True)
    orch.runner = MagicMock()
    orch.runner.get_running_agents.return_value = {}
    orch.plane = None
    orch.config = MagicMock()
    orch.config.plane.default_repo = "factory"
    orch.config.workflows = {}
    return orch


@pytest.fixture
async def api_client(api_db, api_mock_orchestrator):
    app.dependency_overrides[get_db] = lambda: api_db
    app.dependency_overrides[get_orchestrator] = lambda: api_mock_orchestrator
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_api_create_handoff(api_client, api_db):
    task = await api_db.create_task(TaskCreate(title="Source task", repo="myapp"))
    resp = await api_client.post("/api/handoffs", json={
        "from_task_id": task.id,
        "output_type": "code_diff",
        "content": "diff content",
        "summary": "sum",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["from_task_id"] == task.id
    assert data["output_type"] == "code_diff"


async def test_api_create_handoff_missing_source(api_client, api_db):
    resp = await api_client.post("/api/handoffs", json={
        "from_task_id": 9999,
        "content": "data",
    })
    assert resp.status_code == 404


async def test_api_create_handoff_missing_target(api_client, api_db):
    task = await api_db.create_task(TaskCreate(title="Source", repo="myapp"))
    resp = await api_client.post("/api/handoffs", json={
        "from_task_id": task.id,
        "to_task_id": 9999,
        "content": "data",
    })
    assert resp.status_code == 404


async def test_api_get_handoff(api_client, api_db):
    task = await api_db.create_task(TaskCreate(title="Task", repo="myapp"))
    h = await api_db.create_handoff(HandoffCreate(
        from_task_id=task.id, content="data",
    ))
    resp = await api_client.get(f"/api/handoffs/{h.id}")
    assert resp.status_code == 200
    assert resp.json()["content"] == "data"


async def test_api_get_handoff_not_found(api_client):
    resp = await api_client.get("/api/handoffs/99999")
    assert resp.status_code == 404


async def test_api_get_task_handoffs_to(api_client, api_db):
    task_a = await api_db.create_task(TaskCreate(title="A", repo="myapp"))
    task_b = await api_db.create_task(TaskCreate(title="B", repo="myapp"))
    await api_db.create_handoff(HandoffCreate(
        from_task_id=task_a.id, to_task_id=task_b.id,
        content="input for B",
    ))

    resp = await api_client.get(f"/api/tasks/{task_b.id}/handoffs?direction=to")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["content"] == "input for B"


async def test_api_get_task_handoffs_from(api_client, api_db):
    task_a = await api_db.create_task(TaskCreate(title="A", repo="myapp"))
    task_b = await api_db.create_task(TaskCreate(title="B", repo="myapp"))
    await api_db.create_handoff(HandoffCreate(
        from_task_id=task_a.id, to_task_id=task_b.id,
        content="output from A",
    ))

    resp = await api_client.get(f"/api/tasks/{task_a.id}/handoffs?direction=from")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["content"] == "output from A"


async def test_api_get_task_handoffs_not_found(api_client):
    resp = await api_client.get("/api/tasks/9999/handoffs")
    assert resp.status_code == 404


async def test_api_get_workflow_handoffs(api_client, api_db):
    wf = await api_db.create_workflow(name="wf", title="WF", repo="myapp")
    task = await api_db.create_task(TaskCreate(title="Task", repo="myapp"))
    await api_db.create_handoff(HandoffCreate(
        from_task_id=task.id, workflow_id=wf.id,
        content="wf handoff",
    ))

    resp = await api_client.get(f"/api/workflows/{wf.id}/handoffs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["content"] == "wf handoff"


async def test_api_get_workflow_handoffs_not_found(api_client):
    resp = await api_client.get("/api/workflows/9999/handoffs")
    assert resp.status_code == 404


# ── Edge cases ───────────────────────────────────────────────────────────


async def test_handoff_content_truncation():
    """Very large content should be truncated to HANDOFF_MAX_CONTENT."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    task = await db.create_task(TaskCreate(title="Task", repo="myapp"))

    large_output = "x" * (HANDOFF_MAX_CONTENT + 10000)
    hid = await orch._create_handoff_from_output(task.id, large_output)
    handoff = await db.get_handoff(hid)
    assert len(handoff.content) <= HANDOFF_MAX_CONTENT

    await db.close()


async def test_handoff_summary_truncation():
    """Summary should be truncated to HANDOFF_SUMMARY_LIMIT."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    task = await db.create_task(TaskCreate(title="Task", repo="myapp"))

    large_output = "y" * (HANDOFF_SUMMARY_LIMIT + 5000)
    hid = await orch._create_handoff_from_output(task.id, large_output)
    handoff = await db.get_handoff(hid)
    assert len(handoff.summary) <= HANDOFF_SUMMARY_LIMIT

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_handoff_error_does_not_break_success_flow(mock_prompt):
    """If handoff creation fails, the task should still complete."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    task = await db.create_task(TaskCreate(title="Task", repo="myapp"))
    await db.update_task_fields(task.id, branch_name="agent/task-1-test")

    # Force handoff creation to fail
    with patch.object(orch, "_create_handoff_from_output", side_effect=Exception("DB error")):
        with patch.object(orch, "_push_and_create_pr", new_callable=AsyncMock, return_value=""):
            await orch._handle_success(task.id, "Some output")

    # Task should still be updated
    updated = await db.get_task(task.id)
    assert updated.status == TaskStatus.IN_REVIEW

    await db.close()


async def test_empty_output_still_creates_handoff():
    """Even empty output creates a handoff record."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    task = await db.create_task(TaskCreate(title="Task", repo="myapp"))

    hid = await orch._create_handoff_from_output(task.id, "")
    handoff = await db.get_handoff(hid)
    assert handoff is not None
    assert handoff.output_type == "general"
    assert handoff.content == ""

    await db.close()
