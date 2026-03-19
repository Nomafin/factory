import pytest
from unittest.mock import AsyncMock, MagicMock
from httpx import ASGITransport, AsyncClient

from factory.db import Database
from factory.deps import get_db, get_orchestrator
from factory.main import app
from factory.orchestrator import Orchestrator


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


async def test_create_task(client):
    resp = await client.post("/api/tasks", json={
        "title": "Fix login bug",
        "description": "Timeout is too short",
        "repo": "myapp",
        "agent_type": "coder",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Fix login bug"
    assert data["status"] == "queued"
    assert data["id"] is not None


async def test_create_task_default_repo(client):
    resp = await client.post("/api/tasks", json={
        "title": "Fix something",
    })
    assert resp.status_code == 201
    assert resp.json()["repo"] == "factory"


async def test_list_tasks(client):
    await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    await client.post("/api/tasks", json={"title": "Task 2", "repo": "myapp"})

    resp = await client.get("/api/tasks")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_get_task(client):
    create_resp = await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    task_id = create_resp.json()["id"]

    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Task 1"


async def test_get_task_not_found(client):
    resp = await client.get("/api/tasks/999")
    assert resp.status_code == 404


async def test_cancel_task(client):
    create_resp = await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    task_id = create_resp.json()["id"]

    resp = await client.post(f"/api/tasks/{task_id}/cancel")
    assert resp.status_code == 200


async def test_create_task_auto_run(client, mock_orchestrator):
    resp = await client.post("/api/tasks?auto_run=true", json={
        "title": "Auto-run task",
        "repo": "myapp",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Auto-run task"
    assert data["id"] is not None
    mock_orchestrator.process_task.assert_awaited_once_with(data["id"])


async def test_create_task_auto_run_process_failure(client, mock_orchestrator):
    """Task creation should return 201 even when process_task returns False."""
    mock_orchestrator.process_task = AsyncMock(return_value=False)

    resp = await client.post("/api/tasks?auto_run=true", json={
        "title": "Failing auto-run",
        "repo": "myapp",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Failing auto-run"
    assert data["id"] is not None


async def test_create_task_auto_run_process_exception(client, mock_orchestrator):
    """Task creation should return 201 even when process_task raises an exception."""
    mock_orchestrator.process_task = AsyncMock(side_effect=RuntimeError("agent binary not found"))

    resp = await client.post("/api/tasks?auto_run=true", json={
        "title": "Exception auto-run",
        "repo": "myapp",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Exception auto-run"
    assert data["id"] is not None


async def test_get_task_logs(client, db):
    create_resp = await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    task_id = create_resp.json()["id"]

    await db.add_log(task_id, "First output")
    await db.add_log(task_id, "Second output")

    resp = await client.get(f"/api/tasks/{task_id}/logs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["logs"]) == 2
    assert data["logs"][0]["message"] == "First output"


async def test_get_task_logs_not_found(client):
    resp = await client.get("/api/tasks/999/logs")
    assert resp.status_code == 404


async def test_get_task_logs_with_limit(client, db):
    create_resp = await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    task_id = create_resp.json()["id"]

    for i in range(10):
        await db.add_log(task_id, f"Output {i}")

    resp = await client.get(f"/api/tasks/{task_id}/logs?limit=3")
    assert resp.status_code == 200
    assert len(resp.json()["logs"]) == 3


async def test_task_has_last_output(client, db):
    create_resp = await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    task_id = create_resp.json()["id"]

    await db.add_log(task_id, "Some agent output")

    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["last_output"] == "Some agent output"


async def test_task_last_output_none_when_no_logs(client):
    create_resp = await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    task_id = create_resp.json()["id"]

    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["last_output"] is None


async def test_list_agents_empty(client):
    resp = await client.get("/api/agents")
    assert resp.status_code == 200
    assert resp.json() == []
