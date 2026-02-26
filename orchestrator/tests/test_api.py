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


async def test_list_agents_empty(client):
    resp = await client.get("/api/agents")
    assert resp.status_code == 200
    assert resp.json() == []
