import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from factory.config import AgentTemplateConfig, Config, RepoConfig
from factory.db import Database
from factory.deps import get_db, get_orchestrator
from factory.main import app
from factory.models import TaskCreate, TaskStatus
from factory.orchestrator import Orchestrator


# --- Unit tests for clarification extraction ---


def test_extract_clarification_from_output():
    output = '{"type": "clarification_needed", "question": "Which database should I use?"}'
    result = Orchestrator._extract_clarification(output)
    assert result == "Which database should I use?"


def test_extract_clarification_embedded_in_text():
    output = (
        'Some text before\n'
        '{"type": "clarification_needed", "question": "Should I use PostgreSQL or MySQL?"}\n'
        'Some text after'
    )
    result = Orchestrator._extract_clarification(output)
    assert result == "Should I use PostgreSQL or MySQL?"


def test_extract_clarification_returns_none_for_normal_output():
    output = "## Summary\nFixed the login bug by increasing timeout."
    result = Orchestrator._extract_clarification(output)
    assert result is None


def test_extract_clarification_returns_none_for_other_json():
    output = '{"type": "result", "data": "some result"}'
    result = Orchestrator._extract_clarification(output)
    assert result is None


# --- Orchestrator integration tests ---


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_handle_clarification_updates_status(MockRunner, MockRepoMgr):
    db = Database(":memory:")
    await db.initialize()

    config = Config(
        repos={"myapp": RepoConfig(url="git@github.com:user/myapp.git")},
        agent_templates={"coder": AgentTemplateConfig(
            allowed_tools=["Read", "Edit", "Bash"],
        )},
    )

    orch = Orchestrator(db=db, config=config)
    orch.plane = None  # No Plane configured
    orch.notifier = None

    task = await db.create_task(TaskCreate(
        title="Fix bug", repo="myapp", agent_type="coder",
    ))
    await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

    await orch._handle_clarification(task.id, "Which database should I use?")

    updated = await db.get_task(task.id)
    assert updated.status == TaskStatus.WAITING_FOR_INPUT
    assert updated.clarification_context != ""

    context = json.loads(updated.clarification_context)
    assert context["pending_question"] == "Which database should I use?"
    assert len(context["history"]) == 1
    assert context["history"][0]["question"] == "Which database should I use?"

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_handle_success_detects_clarification(MockRunner, MockRepoMgr):
    db = Database(":memory:")
    await db.initialize()

    config = Config(
        repos={"myapp": RepoConfig(url="git@github.com:user/myapp.git")},
        agent_templates={"coder": AgentTemplateConfig(
            allowed_tools=["Read", "Edit", "Bash"],
        )},
    )

    orch = Orchestrator(db=db, config=config)
    orch.plane = None
    orch.notifier = None

    task = await db.create_task(TaskCreate(
        title="Fix bug", repo="myapp", agent_type="coder",
    ))
    await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

    output = '{"type": "clarification_needed", "question": "What API version?"}'
    await orch._handle_success(task.id, output)

    updated = await db.get_task(task.id)
    assert updated.status == TaskStatus.WAITING_FOR_INPUT

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_handle_success_skips_when_already_waiting_for_input(MockRunner, MockRepoMgr):
    """Regression test: when mid-stream clarification was already handled by
    _handle_clarification_and_stop, _handle_success must NOT post the same
    clarification to Plane again (duplicate question bug)."""
    db = Database(":memory:")
    await db.initialize()

    config = Config(
        repos={"myapp": RepoConfig(url="git@github.com:user/myapp.git")},
        agent_templates={"coder": AgentTemplateConfig(
            allowed_tools=["Read", "Edit", "Bash"],
        )},
    )

    mock_runner = MockRunner.return_value
    mock_runner.cancel_agent = AsyncMock()

    orch = Orchestrator(db=db, config=config)
    orch.runner = mock_runner
    mock_plane = MagicMock()
    mock_plane.add_comment = AsyncMock()
    mock_plane.update_issue_state = AsyncMock()
    orch.plane = mock_plane
    orch.notifier = None

    task = await db.create_task(TaskCreate(
        title="Fix bug", repo="myapp", agent_type="coder",
        plane_issue_id="issue-123",
    ))
    await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

    question = "Which database should I use?"
    output = f'{{"type": "clarification_needed", "question": "{question}"}}'

    # Simulate mid-stream clarification detection (path 1):
    # _handle_clarification_and_stop calls _handle_clarification then cancels agent
    await orch._handle_clarification(task.id, question)

    # At this point the question has been posted to Plane once
    assert mock_plane.add_comment.call_count == 1

    # Simulate agent completion after cancel (path 2) — this must NOT post again
    await orch._handle_success(task.id, output)

    # Verify Plane comment was NOT posted a second time
    assert mock_plane.add_comment.call_count == 1

    # Task should still be waiting for input
    updated = await db.get_task(task.id)
    assert updated.status == TaskStatus.WAITING_FOR_INPUT

    # Clarification context should have exactly one history entry, not two
    context = json.loads(updated.clarification_context)
    assert len(context["history"]) == 1
    assert context["history"][0]["question"] == question

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_handle_success_still_detects_clarification_when_not_waiting(MockRunner, MockRepoMgr):
    """Ensure _handle_success still handles clarification when it was NOT
    already detected mid-stream (agent outputs it only at exit)."""
    db = Database(":memory:")
    await db.initialize()

    config = Config(
        repos={"myapp": RepoConfig(url="git@github.com:user/myapp.git")},
        agent_templates={"coder": AgentTemplateConfig(
            allowed_tools=["Read", "Edit", "Bash"],
        )},
    )

    orch = Orchestrator(db=db, config=config)
    orch.plane = None
    orch.notifier = None

    task = await db.create_task(TaskCreate(
        title="Fix bug", repo="myapp", agent_type="coder",
    ))
    await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

    # Clarification only in final output — no mid-stream detection
    output = '{"type": "clarification_needed", "question": "What API version?"}'
    await orch._handle_success(task.id, output)

    updated = await db.get_task(task.id)
    assert updated.status == TaskStatus.WAITING_FOR_INPUT
    context = json.loads(updated.clarification_context)
    assert context["pending_question"] == "What API version?"

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_resume_task(MockRunner, MockRepoMgr):
    db = Database(":memory:")
    await db.initialize()

    config = Config(
        repos={"myapp": RepoConfig(url="git@github.com:user/myapp.git")},
        agent_templates={"coder": AgentTemplateConfig(
            allowed_tools=["Read", "Edit", "Bash"],
        )},
    )

    mock_runner = MockRunner.return_value
    mock_runner.can_accept_task = True
    mock_runner.start_agent = AsyncMock(return_value=True)

    orch = Orchestrator(db=db, config=config)
    orch.runner = mock_runner
    orch.plane = None
    orch.notifier = None

    task = await db.create_task(TaskCreate(
        title="Fix bug", repo="myapp", agent_type="coder",
    ))
    await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)
    await db.update_task_fields(task.id, branch_name="agent/task-1-fix-bug")

    # Simulate clarification
    await orch._handle_clarification(task.id, "Which database?")

    # Now resume with user response
    result = await orch.resume_task(task.id, "Use PostgreSQL")

    assert result is True
    mock_runner.start_agent.assert_called_once()

    # Check the prompt includes clarification history
    call_kwargs = mock_runner.start_agent.call_args
    prompt = call_kwargs.kwargs.get("prompt") or call_kwargs[1].get("prompt") or call_kwargs[0][1]
    assert "Which database?" in prompt
    assert "Use PostgreSQL" in prompt

    updated = await db.get_task(task.id)
    assert updated.status == TaskStatus.IN_PROGRESS

    # Check clarification context updated with response
    context = json.loads(updated.clarification_context)
    assert context["history"][0]["response"] == "Use PostgreSQL"
    assert "pending_question" not in context

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_resume_task_wrong_status(MockRunner, MockRepoMgr):
    db = Database(":memory:")
    await db.initialize()

    config = Config()
    orch = Orchestrator(db=db, config=config)

    task = await db.create_task(TaskCreate(
        title="Fix bug", repo="myapp", agent_type="coder",
    ))

    result = await orch.resume_task(task.id, "some response")
    assert result is False  # Task is QUEUED, not WAITING_FOR_INPUT

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_poll_waiting_tasks(MockRunner, MockRepoMgr):
    db = Database(":memory:")
    await db.initialize()

    config = Config(
        repos={"myapp": RepoConfig(url="git@github.com:user/myapp.git")},
        agent_templates={"coder": AgentTemplateConfig(
            allowed_tools=["Read", "Edit", "Bash"],
        )},
    )

    mock_runner = MockRunner.return_value
    mock_runner.can_accept_task = True
    mock_runner.start_agent = AsyncMock(return_value=True)

    orch = Orchestrator(db=db, config=config)
    orch.runner = mock_runner
    orch.notifier = None

    # Set up a mock Plane client
    mock_plane = MagicMock()
    mock_plane.get_comments = AsyncMock(return_value=[
        {
            "comment_html": "<p>Use PostgreSQL</p>",
            "created_at": "2099-01-01T00:00:00Z",
        }
    ])
    mock_plane.add_comment = AsyncMock()
    mock_plane.update_issue_state = AsyncMock()
    orch.plane = mock_plane

    task = await db.create_task(TaskCreate(
        title="Fix bug", repo="myapp", agent_type="coder",
        plane_issue_id="issue-123",
    ))
    await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)
    await db.update_task_fields(task.id, branch_name="agent/task-1-fix-bug")

    # Simulate a pending clarification
    context = {
        "history": [{"question": "Which database?", "asked_at": "2024-01-01T00:00:00Z"}],
        "pending_question": "Which database?",
        "asked_at": "2024-01-01T00:00:00Z",
    }
    await db.update_task_fields(task.id, clarification_context=json.dumps(context))
    await db.update_task_status(task.id, TaskStatus.WAITING_FOR_INPUT)

    await orch.poll_waiting_tasks()

    # Agent should have been restarted
    mock_runner.start_agent.assert_called_once()
    updated = await db.get_task(task.id)
    assert updated.status == TaskStatus.IN_PROGRESS

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_poll_ignores_old_comments(MockRunner, MockRepoMgr):
    db = Database(":memory:")
    await db.initialize()

    config = Config()

    orch = Orchestrator(db=db, config=config)
    orch.notifier = None

    mock_plane = MagicMock()
    mock_plane.get_comments = AsyncMock(return_value=[
        {
            "comment_html": "<p>Old comment</p>",
            "created_at": "2023-01-01T00:00:00Z",  # Before asked_at
        }
    ])
    mock_plane.add_comment = AsyncMock()
    orch.plane = mock_plane

    task = await db.create_task(TaskCreate(
        title="Fix bug", repo="myapp", agent_type="coder",
        plane_issue_id="issue-123",
    ))

    context = {
        "history": [{"question": "Which database?", "asked_at": "2024-01-01T00:00:00Z"}],
        "pending_question": "Which database?",
        "asked_at": "2024-01-01T00:00:00Z",
    }
    await db.update_task_fields(task.id, clarification_context=json.dumps(context))
    await db.update_task_status(task.id, TaskStatus.WAITING_FOR_INPUT)

    await orch.poll_waiting_tasks()

    # Task should still be waiting - no new comments
    updated = await db.get_task(task.id)
    assert updated.status == TaskStatus.WAITING_FOR_INPUT

    await db.close()


# --- API tests ---


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
    return orch


@pytest.fixture
async def client(db, mock_orchestrator):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_resume_endpoint(client, db, mock_orchestrator):
    # Create a task and set it to waiting
    resp = await client.post("/api/tasks", json={
        "title": "Fix bug", "repo": "myapp",
    })
    task_id = resp.json()["id"]
    await db.update_task_status(task_id, TaskStatus.WAITING_FOR_INPUT)

    resp = await client.post(f"/api/tasks/{task_id}/resume", json={
        "response": "Use PostgreSQL",
    })
    assert resp.status_code == 200
    mock_orchestrator.resume_task.assert_called_once_with(task_id, "Use PostgreSQL")


async def test_resume_endpoint_wrong_status(client, db):
    resp = await client.post("/api/tasks", json={
        "title": "Fix bug", "repo": "myapp",
    })
    task_id = resp.json()["id"]

    resp = await client.post(f"/api/tasks/{task_id}/resume", json={
        "response": "Use PostgreSQL",
    })
    assert resp.status_code == 400


async def test_plane_comment_webhook_resumes_task(client, db, mock_orchestrator):
    resp = await client.post("/api/tasks", json={
        "title": "Fix bug", "repo": "myapp", "plane_issue_id": "issue-abc",
    })
    task_id = resp.json()["id"]
    await db.update_task_status(task_id, TaskStatus.WAITING_FOR_INPUT)

    webhook_payload = {
        "event": "comment",
        "action": "created",
        "data": {
            "issue": "issue-abc",
            "comment_html": "<p>Use PostgreSQL please</p>",
        },
    }
    resp = await client.post("/api/webhooks/plane", json=webhook_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resumed"
    mock_orchestrator.resume_task.assert_called_once_with(task_id, "Use PostgreSQL please")


async def test_plane_comment_webhook_ignores_non_waiting_task(client, db):
    resp = await client.post("/api/tasks", json={
        "title": "Fix bug", "repo": "myapp", "plane_issue_id": "issue-xyz",
    })

    webhook_payload = {
        "event": "comment",
        "action": "created",
        "data": {
            "issue": "issue-xyz",
            "comment_html": "<p>Some comment</p>",
        },
    }
    resp = await client.post("/api/webhooks/plane", json=webhook_payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


# --- DB tests for clarification_context ---


async def test_db_clarification_context():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(
        title="Fix bug", repo="myapp", agent_type="coder",
    ))
    assert task.clarification_context == ""

    context = json.dumps({"question": "Which DB?", "asked_at": "2024-01-01T00:00:00Z"})
    await db.update_task_fields(task.id, clarification_context=context)

    updated = await db.get_task(task.id)
    assert updated.clarification_context == context

    await db.close()
