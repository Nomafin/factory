"""Tests for multi-agent workflow orchestration."""

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
    TaskCreate, TaskStatus, Workflow, WorkflowCreate, WorkflowStatus,
)
from factory.orchestrator import Orchestrator


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_config(**overrides) -> Config:
    """Create a Config with a standard code_review workflow."""
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


# ── Database workflow tests ──────────────────────────────────────────────


async def test_db_create_workflow():
    db = Database(":memory:")
    await db.initialize()

    wf = await db.create_workflow(
        name="code_review", title="Review my PR",
        description="Please review", repo="myapp",
    )

    assert wf.id is not None
    assert wf.name == "code_review"
    assert wf.title == "Review my PR"
    assert wf.status == WorkflowStatus.PENDING
    assert wf.current_step == 0

    await db.close()


async def test_db_create_and_get_workflow():
    db = Database(":memory:")
    await db.initialize()

    wf = await db.create_workflow(name="code_review", title="My workflow", repo="myapp")
    fetched = await db.get_workflow(wf.id)

    assert fetched is not None
    assert fetched.name == "code_review"
    assert fetched.steps == []

    await db.close()


async def test_db_create_workflow_steps():
    db = Database(":memory:")
    await db.initialize()

    wf = await db.create_workflow(name="code_review", title="My workflow", repo="myapp")
    step0 = await db.create_workflow_step(
        workflow_id=wf.id, step_index=0, agent_type="coder",
        output_key="code_changes",
    )
    step1 = await db.create_workflow_step(
        workflow_id=wf.id, step_index=1, agent_type="reviewer",
        input_key="code_changes", output_key="review_feedback",
    )

    assert step0.step_index == 0
    assert step0.agent_type == "coder"
    assert step1.input_key == "code_changes"

    # Reload workflow with steps
    fetched = await db.get_workflow(wf.id)
    assert len(fetched.steps) == 2
    assert fetched.steps[0].agent_type == "coder"
    assert fetched.steps[1].agent_type == "reviewer"

    await db.close()


async def test_db_update_workflow_status():
    db = Database(":memory:")
    await db.initialize()

    wf = await db.create_workflow(name="code_review", title="My workflow", repo="myapp")
    updated = await db.update_workflow_status(wf.id, WorkflowStatus.RUNNING)

    assert updated.status == WorkflowStatus.RUNNING
    assert updated.started_at is not None

    completed = await db.update_workflow_status(wf.id, WorkflowStatus.COMPLETED)
    assert completed.status == WorkflowStatus.COMPLETED
    assert completed.completed_at is not None

    await db.close()


async def test_db_update_workflow_step_status():
    db = Database(":memory:")
    await db.initialize()

    wf = await db.create_workflow(name="code_review", title="My workflow", repo="myapp")
    step = await db.create_workflow_step(
        workflow_id=wf.id, step_index=0, agent_type="coder",
    )

    updated = await db.update_workflow_step_status(
        step.id, "running", task_id=42,
    )
    assert updated.status == "running"
    assert updated.task_id == 42
    assert updated.started_at is not None

    completed = await db.update_workflow_step_status(
        step.id, "completed", output_data="Some output from coder",
    )
    assert completed.status == "completed"
    assert completed.output_data == "Some output from coder"
    assert completed.completed_at is not None

    await db.close()


async def test_db_get_step_output():
    db = Database(":memory:")
    await db.initialize()

    wf = await db.create_workflow(name="code_review", title="My workflow", repo="myapp")
    step = await db.create_workflow_step(
        workflow_id=wf.id, step_index=0, agent_type="coder",
        output_key="code_changes",
    )
    await db.update_workflow_step_status(
        step.id, "completed", output_data="diff --git a/foo.py",
    )

    output = await db.get_step_output(wf.id, "code_changes")
    assert output == "diff --git a/foo.py"

    # Non-existent key returns empty string
    missing = await db.get_step_output(wf.id, "nonexistent")
    assert missing == ""

    await db.close()


async def test_db_list_workflows():
    db = Database(":memory:")
    await db.initialize()

    await db.create_workflow(name="wf1", title="First", repo="myapp")
    wf2 = await db.create_workflow(name="wf2", title="Second", repo="myapp")
    await db.update_workflow_status(wf2.id, WorkflowStatus.RUNNING)

    all_wfs = await db.list_workflows()
    assert len(all_wfs) == 2

    running = await db.list_workflows(status=WorkflowStatus.RUNNING)
    assert len(running) == 1
    assert running[0].name == "wf2"

    pending = await db.list_workflows(status=WorkflowStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].name == "wf1"

    await db.close()


async def test_db_workflow_fields_update():
    db = Database(":memory:")
    await db.initialize()

    wf = await db.create_workflow(name="code_review", title="My workflow", repo="myapp")
    updated = await db.update_workflow_fields(wf.id, current_step=2)

    assert updated.current_step == 2

    await db.close()


async def test_db_task_workflow_fields():
    """Test that tasks have workflow_id and workflow_step fields."""
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(
        title="Workflow task", repo="myapp", agent_type="coder",
    ))
    assert task.workflow_id is None
    assert task.workflow_step is None

    await db.update_task_fields(task.id, workflow_id=1, workflow_step=0)
    updated = await db.get_task(task.id)
    assert updated.workflow_id == 1
    assert updated.workflow_step == 0

    await db.close()


# ── Config tests ─────────────────────────────────────────────────────────


def test_config_workflows():
    config = _make_config()
    assert "code_review" in config.workflows
    wf = config.workflows["code_review"]
    assert len(wf.steps) == 3
    assert wf.steps[0].agent == "coder"
    assert wf.steps[0].output == "code_changes"
    assert wf.steps[1].input == "code_changes"
    assert wf.steps[2].condition == "has_issues"


def test_config_empty_workflows():
    config = Config()
    assert config.workflows == {}


# ── Orchestrator workflow tests ──────────────────────────────────────────


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_start_workflow(mock_prompt):
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await orch.start_workflow(
        workflow_name="code_review",
        title="Review feature X",
        description="Please review this feature",
        repo="myapp",
    )

    assert wf is not None
    assert wf.name == "code_review"
    assert wf.status == WorkflowStatus.RUNNING
    assert len(wf.steps) == 3

    # First step should be running
    assert wf.steps[0].status == "running"
    assert wf.steps[0].agent_type == "coder"
    assert wf.steps[0].task_id is not None

    # A task should have been created and linked
    task = await db.get_task(wf.steps[0].task_id)
    assert task is not None
    assert task.workflow_id == wf.id
    assert task.workflow_step == 0
    assert task.agent_type == "coder"

    # Runner should have been called
    orch.runner.start_agent.assert_called_once()

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_start_workflow_unknown_name(mock_prompt):
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await orch.start_workflow(
        workflow_name="nonexistent",
        title="Should fail",
        repo="myapp",
    )

    assert wf is None

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_workflow_advance_step(mock_prompt):
    """Test that completing a step advances to the next one."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await orch.start_workflow(
        workflow_name="code_review",
        title="Review feature X",
        repo="myapp",
    )

    # Simulate step 0 (coder) completing
    first_task_id = wf.steps[0].task_id
    await orch._advance_workflow(wf.id, 0, "Here are the code changes I made")

    # Verify step 0 is completed
    updated_wf = await db.get_workflow(wf.id)
    assert updated_wf.steps[0].status == "completed"
    assert updated_wf.steps[0].output_data == "Here are the code changes I made"

    # Step 1 (reviewer) should now be running
    assert updated_wf.steps[1].status == "running"
    assert updated_wf.steps[1].task_id is not None

    # Verify the reviewer task gets the code_changes as input
    reviewer_task = await db.get_task(updated_wf.steps[1].task_id)
    assert reviewer_task.agent_type == "reviewer"
    assert "code_changes" in reviewer_task.description
    assert "Here are the code changes I made" in reviewer_task.description

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_workflow_conditional_step_skip(mock_prompt):
    """Test that a conditional step is skipped when condition is not met."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await orch.start_workflow(
        workflow_name="code_review",
        title="Review feature X",
        repo="myapp",
    )

    # Step 0 completes
    await orch._advance_workflow(wf.id, 0, "Here are the code changes")

    # Step 1 (reviewer) completes with a positive review (no issues)
    updated_wf = await db.get_workflow(wf.id)
    step1_task_id = updated_wf.steps[1].task_id
    review_output = "LGTM! The code looks great, well structured, no changes needed."
    await orch._advance_workflow(wf.id, 1, review_output)

    # Step 2 has condition "has_issues" which should NOT be met
    final_wf = await db.get_workflow(wf.id)
    assert final_wf.steps[2].status == "skipped"

    # Workflow should be completed
    assert final_wf.status == WorkflowStatus.COMPLETED

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_workflow_conditional_step_runs(mock_prompt):
    """Test that a conditional step runs when condition IS met."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await orch.start_workflow(
        workflow_name="code_review",
        title="Review feature X",
        repo="myapp",
    )

    # Step 0 completes
    await orch._advance_workflow(wf.id, 0, "Here are the code changes")

    # Step 1 (reviewer) completes with issues found
    updated_wf = await db.get_workflow(wf.id)
    review_output = "Found several issues: 1. Missing error handling 2. Bug in validation"
    await orch._advance_workflow(wf.id, 1, review_output)

    # Step 2 has condition "has_issues" which SHOULD be met
    final_wf = await db.get_workflow(wf.id)
    assert final_wf.steps[2].status == "running"
    assert final_wf.steps[2].task_id is not None

    # The revision task should include the review feedback
    revision_task = await db.get_task(final_wf.steps[2].task_id)
    assert "review_feedback" in revision_task.description
    assert "Missing error handling" in revision_task.description

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_workflow_full_completion(mock_prompt):
    """Test a complete workflow run through all steps."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await orch.start_workflow(
        workflow_name="code_review",
        title="Review feature X",
        repo="myapp",
    )

    # Step 0 (coder) completes
    await orch._advance_workflow(wf.id, 0, "Code changes done")

    # Step 1 (reviewer) completes with issues
    await orch._advance_workflow(wf.id, 1, "Found an issue in the error handling")

    # Step 2 (coder revision) completes
    await orch._advance_workflow(wf.id, 2, "Fixed the issue, all good now")

    final_wf = await db.get_workflow(wf.id)
    assert final_wf.status == WorkflowStatus.COMPLETED
    assert final_wf.completed_at is not None
    assert all(s.status in ("completed", "skipped") for s in final_wf.steps)

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_workflow_fail_on_step_failure(mock_prompt):
    """Test that workflow fails when a step's task fails."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await orch.start_workflow(
        workflow_name="code_review",
        title="Review feature X",
        repo="myapp",
    )

    # Simulate step 0 failure
    await orch._fail_workflow(wf.id, 0, "Agent crashed")

    updated_wf = await db.get_workflow(wf.id)
    assert updated_wf.status == WorkflowStatus.FAILED
    assert "Agent crashed" in updated_wf.error

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_cancel_workflow(mock_prompt):
    """Test cancelling a running workflow."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await orch.start_workflow(
        workflow_name="code_review",
        title="Review feature X",
        repo="myapp",
    )

    success = await orch.cancel_workflow(wf.id)
    assert success is True

    updated_wf = await db.get_workflow(wf.id)
    assert updated_wf.status == WorkflowStatus.CANCELLED

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_cancel_non_running_workflow(mock_prompt):
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await db.create_workflow(name="code_review", title="Test", repo="myapp")

    # Workflow is PENDING, not RUNNING
    success = await orch.cancel_workflow(wf.id)
    assert success is False

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_handle_success_advances_workflow(mock_prompt):
    """Test that _handle_success triggers workflow advancement."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await orch.start_workflow(
        workflow_name="code_review",
        title="Review feature X",
        repo="myapp",
    )

    # Get the task for step 0
    task_id = wf.steps[0].task_id
    task = await db.get_task(task_id)

    # Simulate _handle_success being called when agent completes
    await orch._handle_success(task_id, "Code changes done successfully")

    # Workflow should have advanced
    updated_wf = await db.get_workflow(wf.id)
    assert updated_wf.steps[0].status == "completed"
    assert updated_wf.steps[1].status == "running"

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_handle_failure_fails_workflow(mock_prompt):
    """Test that _handle_failure triggers workflow failure."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await orch.start_workflow(
        workflow_name="code_review",
        title="Review feature X",
        repo="myapp",
    )

    task_id = wf.steps[0].task_id

    await orch._handle_failure(task_id, "Agent crashed with error")

    updated_wf = await db.get_workflow(wf.id)
    assert updated_wf.status == WorkflowStatus.FAILED

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_recover_orphaned_workflows(mock_prompt):
    """Test that orphaned running workflows are recovered on startup."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)

    # Create a "running" workflow manually (simulating an orphan)
    wf = await db.create_workflow(name="code_review", title="Orphaned", repo="myapp")
    await db.update_workflow_status(wf.id, WorkflowStatus.RUNNING)

    await orch.recover_orphaned_workflows()

    updated = await db.get_workflow(wf.id)
    assert updated.status == WorkflowStatus.FAILED
    assert "restart" in updated.error.lower()

    await db.close()


# ── Condition evaluation tests ────────────────────────────────────────


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_evaluate_has_issues_true(mock_prompt):
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await db.create_workflow(name="test", title="Test", repo="myapp")
    step = await db.create_workflow_step(
        workflow_id=wf.id, step_index=0, agent_type="reviewer",
        output_key="review",
    )
    await db.update_workflow_step_status(
        step.id, "completed",
        output_data="Found an issue with the error handling",
    )

    result = await orch._evaluate_condition(wf.id, "has_issues")
    assert result is True

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_evaluate_has_issues_false(mock_prompt):
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await db.create_workflow(name="test", title="Test", repo="myapp")
    step = await db.create_workflow_step(
        workflow_id=wf.id, step_index=0, agent_type="reviewer",
        output_key="review",
    )
    await db.update_workflow_step_status(
        step.id, "completed",
        output_data="Everything looks great, LGTM!",
    )

    result = await orch._evaluate_condition(wf.id, "has_issues")
    assert result is False

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_evaluate_no_issues(mock_prompt):
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await db.create_workflow(name="test", title="Test", repo="myapp")
    step = await db.create_workflow_step(
        workflow_id=wf.id, step_index=0, agent_type="reviewer",
        output_key="review",
    )
    await db.update_workflow_step_status(
        step.id, "completed",
        output_data="Code looks great, no changes needed",
    )

    result = await orch._evaluate_condition(wf.id, "no_issues")
    assert result is True

    await db.close()


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_evaluate_condition_no_previous_output(mock_prompt):
    """Condition defaults to True when there's no previous output."""
    db = Database(":memory:")
    await db.initialize()

    orch = await _make_orchestrator(db)
    wf = await db.create_workflow(name="test", title="Test", repo="myapp")

    result = await orch._evaluate_condition(wf.id, "has_issues")
    assert result is True  # Default to running the step

    await db.close()


# ── Two-step workflow tests ──────────────────────────────────────────────


@patch("factory.orchestrator.load_prompt", return_value="system prompt")
async def test_two_step_workflow(mock_prompt):
    """Test a simple two-step workflow without conditions."""
    db = Database(":memory:")
    await db.initialize()

    config = _make_config(
        workflows={
            "simple": WorkflowConfig(steps=[
                WorkflowStepConfig(agent="coder", output="code"),
                WorkflowStepConfig(agent="reviewer", input="code"),
            ]),
        },
    )
    orch = await _make_orchestrator(db, config)

    wf = await orch.start_workflow(
        workflow_name="simple",
        title="Simple review",
        repo="myapp",
    )
    assert wf.status == WorkflowStatus.RUNNING

    # Complete step 0
    await orch._advance_workflow(wf.id, 0, "Code written")

    # Complete step 1
    await orch._advance_workflow(wf.id, 1, "Review done, LGTM")

    final = await db.get_workflow(wf.id)
    assert final.status == WorkflowStatus.COMPLETED

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
        "code_review": WorkflowConfig(steps=[
            WorkflowStepConfig(agent="coder", output="code_changes"),
            WorkflowStepConfig(agent="reviewer", input="code_changes"),
        ]),
    }
    return orch


@pytest.fixture
async def client(db, mock_orchestrator):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_api_create_workflow(client, db, mock_orchestrator):
    """POST /api/workflows creates and starts a workflow."""
    # Set up mock to return a workflow
    async def mock_start_workflow(**kwargs):
        wf = await db.create_workflow(
            name=kwargs["workflow_name"],
            title=kwargs["title"],
            repo=kwargs.get("repo", ""),
        )
        return await db.update_workflow_status(wf.id, WorkflowStatus.RUNNING)

    mock_orchestrator.start_workflow = AsyncMock(side_effect=mock_start_workflow)

    resp = await client.post("/api/workflows", json={
        "workflow_name": "code_review",
        "title": "Review feature X",
        "description": "Please review this feature",
        "repo": "myapp",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "code_review"
    assert data["title"] == "Review feature X"
    assert data["status"] == "running"


async def test_api_create_workflow_unknown_name(client, mock_orchestrator):
    """POST /api/workflows with unknown workflow name returns 400."""
    resp = await client.post("/api/workflows", json={
        "workflow_name": "nonexistent",
        "title": "Should fail",
    })
    assert resp.status_code == 400
    assert "Unknown workflow" in resp.json()["detail"]


async def test_api_list_workflows(client, db):
    """GET /api/workflows returns all workflows."""
    await db.create_workflow(name="wf1", title="First", repo="myapp")
    await db.create_workflow(name="wf2", title="Second", repo="myapp")

    resp = await client.get("/api/workflows")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


async def test_api_list_workflows_by_status(client, db):
    """GET /api/workflows?status=running returns filtered list."""
    wf1 = await db.create_workflow(name="wf1", title="First", repo="myapp")
    await db.create_workflow(name="wf2", title="Second", repo="myapp")
    await db.update_workflow_status(wf1.id, WorkflowStatus.RUNNING)

    resp = await client.get("/api/workflows?status=running")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "wf1"


async def test_api_get_workflow(client, db):
    """GET /api/workflows/{id} returns a specific workflow."""
    wf = await db.create_workflow(name="code_review", title="My workflow", repo="myapp")
    await db.create_workflow_step(
        workflow_id=wf.id, step_index=0, agent_type="coder",
    )

    resp = await client.get(f"/api/workflows/{wf.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "code_review"
    assert len(data["steps"]) == 1


async def test_api_get_workflow_not_found(client):
    """GET /api/workflows/999 returns 404."""
    resp = await client.get("/api/workflows/999")
    assert resp.status_code == 404


async def test_api_cancel_workflow(client, db, mock_orchestrator):
    """POST /api/workflows/{id}/cancel cancels a running workflow."""
    wf = await db.create_workflow(name="code_review", title="My workflow", repo="myapp")
    await db.update_workflow_status(wf.id, WorkflowStatus.RUNNING)

    async def mock_cancel(wid):
        await db.update_workflow_status(wid, WorkflowStatus.CANCELLED)
        return True

    mock_orchestrator.cancel_workflow = AsyncMock(side_effect=mock_cancel)

    resp = await client.post(f"/api/workflows/{wf.id}/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cancelled"


async def test_api_cancel_workflow_not_running(client, db, mock_orchestrator):
    """POST /api/workflows/{id}/cancel returns 400 if not running."""
    wf = await db.create_workflow(name="code_review", title="My workflow", repo="myapp")

    resp = await client.post(f"/api/workflows/{wf.id}/cancel")
    assert resp.status_code == 400
    assert "must be running" in resp.json()["detail"]


async def test_api_task_shows_workflow_context(client, db):
    """GET /api/tasks/{id} includes workflow_id and workflow_step."""
    resp = await client.post("/api/tasks", json={
        "title": "Workflow task", "repo": "myapp",
    })
    task_id = resp.json()["id"]

    # Link it to a workflow
    await db.update_task_fields(task_id, workflow_id=1, workflow_step=0)

    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["workflow_id"] == 1
    assert data["workflow_step"] == 0


async def test_api_workflow_with_steps_response(client, db):
    """Workflow API response includes step details."""
    wf = await db.create_workflow(name="code_review", title="Review", repo="myapp")
    step = await db.create_workflow_step(
        workflow_id=wf.id, step_index=0, agent_type="coder",
        output_key="code_changes",
    )
    await db.update_workflow_step_status(step.id, "completed", output_data="changes")

    resp = await client.get(f"/api/workflows/{wf.id}")
    data = resp.json()

    assert len(data["steps"]) == 1
    assert data["steps"][0]["agent_type"] == "coder"
    assert data["steps"][0]["status"] == "completed"
    assert data["steps"][0]["output_key"] == "code_changes"
