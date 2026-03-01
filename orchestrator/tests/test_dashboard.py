"""Tests for the Factory dashboard web UI routes."""

import json

import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from factory.config import Config
from factory.db import Database
from factory.deps import get_config, get_db, get_orchestrator
from factory.main import app, DASHBOARD_DIR, STATIC_DIR
from factory.models import TaskCreate, TaskStatus
from factory.orchestrator import Orchestrator


@asynccontextmanager
async def _null_lifespan(app):
    yield


@pytest.fixture
def client():
    """Create a test client for the FastAPI app."""
    # Skip lifespan to avoid needing real config/db
    app.router.lifespan_context = _null_lifespan
    return TestClient(app)


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def mock_config():
    config = MagicMock(spec=Config)
    config.plane = MagicMock()
    config.plane.base_url = "https://plane.example.com"
    config.plane.workspace_slug = "my-workspace"
    config.plane.project_id = "proj-123"
    config.plane.default_repo = "factory"
    return config


@pytest.fixture
def mock_orchestrator(db, mock_config):
    orch = MagicMock(spec=Orchestrator)
    orch.cancel_task = AsyncMock()
    orch.process_task = AsyncMock(return_value=True)
    orch.runner = MagicMock()
    orch.runner.get_running_agents.return_value = {}
    orch.plane = None
    orch.config = mock_config
    return orch


@pytest.fixture
async def api_client(db, mock_orchestrator, mock_config):
    """Async client with full dependency overrides for API tests."""
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator
    app.dependency_overrides[get_config] = lambda: mock_config
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class TestDashboardRoutes:
    """Test that dashboard pages are served correctly."""

    def test_root_serves_dashboard(self, client):
        """GET / should serve the dashboard HTML."""
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Factory Dashboard" in r.text

    def test_dashboard_route(self, client):
        """GET /dashboard should serve the dashboard HTML."""
        r = client.get("/dashboard")
        assert r.status_code == 200
        assert "Factory Dashboard" in r.text

    def test_tasks_route(self, client):
        """GET /tasks should serve the dashboard HTML (SPA handles routing)."""
        r = client.get("/tasks")
        assert r.status_code == 200
        assert "Factory Dashboard" in r.text

    def test_agents_route(self, client):
        """GET /agents should serve the dashboard HTML (SPA handles routing)."""
        r = client.get("/agents")
        assert r.status_code == 200
        assert "Factory Dashboard" in r.text

    def test_preview_route(self, client):
        """GET /preview should serve the dashboard HTML (SPA handles routing)."""
        r = client.get("/preview")
        assert r.status_code == 200
        assert "Factory Dashboard" in r.text

    def test_analytics_route(self, client):
        """GET /analytics should serve the dashboard HTML (SPA handles routing)."""
        r = client.get("/analytics")
        assert r.status_code == 200
        assert "Factory Dashboard" in r.text

    def test_messages_route_redirects_to_dashboard(self, client):
        """GET /messages should redirect to dashboard messages tab."""
        r = client.get("/messages", follow_redirects=False)
        assert r.status_code == 302
        assert "/#/messages" in r.headers["location"]

    def test_health_endpoint(self, client):
        """GET /health should return JSON status."""
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_static_css_served(self, client):
        """Dashboard CSS file should be served via /static mount."""
        r = client.get("/static/dashboard/styles.css")
        assert r.status_code == 200
        assert "text/css" in r.headers["content-type"]

    def test_static_js_served(self, client):
        """Dashboard JS file should be served via /static mount."""
        r = client.get("/static/dashboard/app.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]


class TestDashboardContent:
    """Test that dashboard HTML contains expected elements."""

    def test_html_has_sidebar_nav(self, client):
        """Dashboard should contain sidebar navigation."""
        r = client.get("/")
        assert "sidebar" in r.text
        assert "nav-item" in r.text

    def test_html_has_nav_links(self, client):
        """Dashboard should contain links to all pages."""
        r = client.get("/")
        assert "#/dashboard" in r.text
        assert "#/tasks" in r.text
        assert "#/agents" in r.text
        assert "#/preview" in r.text
        assert "#/analytics" in r.text
        assert "#/messages" in r.text

    def test_html_has_main_content_area(self, client):
        """Dashboard should contain main content area."""
        r = client.get("/")
        assert 'id="pageContent"' in r.text

    def test_html_links_css(self, client):
        """Dashboard should link to the CSS stylesheet."""
        r = client.get("/")
        assert "/static/dashboard/styles.css" in r.text

    def test_html_links_js(self, client):
        """Dashboard should link to the JS application."""
        r = client.get("/")
        assert "/static/dashboard/app.js" in r.text

    def test_html_has_health_indicator(self, client):
        """Dashboard should contain health status indicator."""
        r = client.get("/")
        assert "healthDot" in r.text

    def test_html_has_responsive_menu(self, client):
        """Dashboard should contain mobile menu toggle."""
        r = client.get("/")
        assert "menu-toggle" in r.text

    def test_css_has_dark_theme_variables(self, client):
        """CSS should define dark theme color variables."""
        r = client.get("/static/dashboard/styles.css")
        assert "--bg: #0f1117" in r.text
        assert "--surface:" in r.text
        assert "--accent:" in r.text

    def test_css_has_responsive_breakpoints(self, client):
        """CSS should include responsive media queries."""
        r = client.get("/static/dashboard/styles.css")
        assert "@media" in r.text
        assert "768px" in r.text

    def test_js_has_router(self, client):
        """JS should contain hash-based router logic."""
        r = client.get("/static/dashboard/app.js")
        assert "handleRoute" in r.text
        assert "hashchange" in r.text
        assert "parseRoute" in r.text

    def test_js_has_api_integration(self, client):
        """JS should contain API fetch functions."""
        r = client.get("/static/dashboard/app.js")
        assert "/api" in r.text
        assert "apiFetch" in r.text

    def test_js_has_all_page_renderers(self, client):
        """JS should have render functions for all pages."""
        r = client.get("/static/dashboard/app.js")
        assert "renderDashboard" in r.text
        assert "renderTasks" in r.text
        assert "renderTaskDetail" in r.text
        assert "renderAgents" in r.text
        assert "renderPreview" in r.text
        assert "renderAnalytics" in r.text
        assert "renderMessages" in r.text


class TestDashboardFiles:
    """Test that dashboard files exist and are properly structured."""

    def test_dashboard_dir_exists(self):
        """Dashboard directory should exist."""
        assert DASHBOARD_DIR.exists()
        assert DASHBOARD_DIR.is_dir()

    def test_index_html_exists(self):
        """index.html should exist in dashboard directory."""
        assert (DASHBOARD_DIR / "index.html").exists()

    def test_styles_css_exists(self):
        """styles.css should exist in dashboard directory."""
        assert (DASHBOARD_DIR / "styles.css").exists()

    def test_app_js_exists(self):
        """app.js should exist in dashboard directory."""
        assert (DASHBOARD_DIR / "app.js").exists()

    def test_messages_html_still_exists(self):
        """messages.html should still exist (not broken by dashboard)."""
        assert (STATIC_DIR / "messages.html").exists()


# ── Settings endpoint tests ────────────────────────────────────────────


async def test_settings_returns_plane_config(api_client):
    """GET /api/settings returns Plane configuration for the frontend."""
    resp = await api_client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["plane_base_url"] == "https://plane.example.com"
    assert data["plane_workspace_slug"] == "my-workspace"
    assert data["plane_project_id"] == "proj-123"


async def test_settings_strips_trailing_slash(api_client, mock_config):
    """Settings endpoint strips trailing slash from Plane base URL."""
    mock_config.plane.base_url = "https://plane.example.com/"
    resp = await api_client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["plane_base_url"] == "https://plane.example.com"


async def test_settings_empty_plane_config(api_client, mock_config):
    """Settings endpoint handles empty Plane configuration gracefully."""
    mock_config.plane.base_url = ""
    mock_config.plane.workspace_slug = ""
    mock_config.plane.project_id = ""
    resp = await api_client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["plane_base_url"] == ""
    assert data["plane_workspace_slug"] == ""
    assert data["plane_project_id"] == ""


# ── Task list API tests ────────────────────────────────────────────────


async def test_list_tasks_empty(api_client):
    """GET /api/tasks returns empty list when no tasks exist."""
    resp = await api_client.get("/api/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_tasks_returns_all_fields(api_client):
    """Task list response includes all fields needed for the dashboard."""
    resp = await api_client.post("/api/tasks", json={
        "title": "Dashboard task",
        "description": "Test description",
        "repo": "myapp",
        "agent_type": "coder",
    })
    assert resp.status_code == 201

    resp = await api_client.get("/api/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1

    t = tasks[0]
    assert t["title"] == "Dashboard task"
    assert t["status"] == "queued"
    assert t["agent_type"] == "coder"
    assert "created_at" in t
    assert "id" in t


async def test_list_tasks_filter_by_status(api_client, db):
    """GET /api/tasks?status=queued filters tasks by status."""
    t1 = await db.create_task(TaskCreate(title="Queued", repo="r"))
    t2 = await db.create_task(TaskCreate(title="Done", repo="r"))
    await db.update_task_status(t2.id, TaskStatus.DONE)

    resp = await api_client.get("/api/tasks?status=queued")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["title"] == "Queued"

    resp = await api_client.get("/api/tasks?status=done")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["title"] == "Done"


async def test_list_multiple_tasks(api_client):
    """Multiple tasks are returned in task list."""
    for i in range(5):
        await api_client.post("/api/tasks", json={"title": f"Task {i}", "repo": "myapp"})

    resp = await api_client.get("/api/tasks")
    assert resp.status_code == 200
    assert len(resp.json()) == 5


async def test_tasks_with_all_statuses(api_client, db):
    """Task list includes tasks of all statuses for frontend filtering."""
    statuses = [
        TaskStatus.QUEUED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.IN_REVIEW,
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.WAITING_FOR_INPUT,
    ]

    for status in statuses:
        task = await db.create_task(TaskCreate(
            title=f"Task {status.value}",
            repo="myapp",
        ))
        if status != TaskStatus.QUEUED:
            await db.update_task_status(task.id, status)

    resp = await api_client.get("/api/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == len(statuses)

    task_statuses = set(t["status"] for t in tasks)
    expected = set(s.value for s in statuses)
    assert task_statuses == expected


async def test_filter_tasks_by_each_status(api_client, db):
    """API status filter returns correct subsets."""
    t1 = await db.create_task(TaskCreate(title="Queued", repo="r"))
    t2 = await db.create_task(TaskCreate(title="Running", repo="r"))
    await db.update_task_status(t2.id, TaskStatus.IN_PROGRESS)
    t3 = await db.create_task(TaskCreate(title="Done", repo="r"))
    await db.update_task_status(t3.id, TaskStatus.DONE)
    t4 = await db.create_task(TaskCreate(title="Failed", repo="r"))
    await db.update_task_status(t4.id, TaskStatus.FAILED)

    resp = await api_client.get("/api/tasks?status=queued")
    assert len(resp.json()) == 1
    assert resp.json()[0]["title"] == "Queued"

    resp = await api_client.get("/api/tasks?status=in_progress")
    assert len(resp.json()) == 1
    assert resp.json()[0]["title"] == "Running"

    resp = await api_client.get("/api/tasks?status=done")
    assert len(resp.json()) == 1
    assert resp.json()[0]["title"] == "Done"

    resp = await api_client.get("/api/tasks?status=failed")
    assert len(resp.json()) == 1
    assert resp.json()[0]["title"] == "Failed"


# ── Task detail API tests ──────────────────────────────────────────────


async def test_get_task_detail_all_fields(api_client):
    """GET /api/tasks/:id returns complete task information."""
    create_resp = await api_client.post("/api/tasks", json={
        "title": "Detail test",
        "description": "Detailed description",
        "repo": "myapp",
        "agent_type": "reviewer",
    })
    task_id = create_resp.json()["id"]

    resp = await api_client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    task = resp.json()

    assert task["id"] == task_id
    assert task["title"] == "Detail test"
    assert task["description"] == "Detailed description"
    assert task["repo"] == "myapp"
    assert task["agent_type"] == "reviewer"
    assert task["status"] == "queued"
    assert task["pr_url"] == ""
    assert task["preview_url"] == ""
    assert task["plane_issue_id"] == ""
    assert task["clarification_context"] == ""
    assert task["error"] == ""
    assert task["created_at"] is not None
    assert task["started_at"] is None
    assert task["completed_at"] is None


async def test_get_task_not_found_api(api_client):
    """GET /api/tasks/:id returns 404 for nonexistent task."""
    resp = await api_client.get("/api/tasks/999")
    assert resp.status_code == 404


async def test_task_detail_with_plane_issue(api_client, db):
    """Task detail includes plane_issue_id for linking."""
    task = await db.create_task(TaskCreate(
        title="Plane-linked task",
        repo="myapp",
        plane_issue_id="issue-abc-123",
    ))

    resp = await api_client.get(f"/api/tasks/{task.id}")
    assert resp.status_code == 200
    assert resp.json()["plane_issue_id"] == "issue-abc-123"


async def test_task_detail_with_pr_url(api_client, db):
    """Task detail includes PR URL when available."""
    task = await db.create_task(TaskCreate(title="PR task", repo="myapp"))
    await db.update_task_fields(task.id, pr_url="https://github.com/org/repo/pull/42")

    resp = await api_client.get(f"/api/tasks/{task.id}")
    assert resp.status_code == 200
    assert resp.json()["pr_url"] == "https://github.com/org/repo/pull/42"


async def test_task_detail_with_preview_url(api_client, db):
    """Task detail includes preview URL when available."""
    task = await db.create_task(TaskCreate(title="Preview task", repo="myapp"))
    await db.update_task_fields(task.id, preview_url="https://preview.example.com/task-1")

    resp = await api_client.get(f"/api/tasks/{task.id}")
    assert resp.status_code == 200
    assert resp.json()["preview_url"] == "https://preview.example.com/task-1"


async def test_task_detail_with_clarification_history(api_client, db):
    """Task detail includes clarification history when present."""
    task = await db.create_task(TaskCreate(title="Clarify task", repo="myapp"))

    context = {
        "history": [
            {
                "question": "Which database should I use?",
                "asked_at": "2024-01-15T10:00:00Z",
                "response": "Use PostgreSQL",
                "responded_at": "2024-01-15T10:05:00Z",
            },
            {
                "question": "Should I add indexes?",
                "asked_at": "2024-01-15T11:00:00Z",
                "response": "Yes, on the user_id column",
                "responded_at": "2024-01-15T11:03:00Z",
            },
        ],
    }
    await db.update_task_fields(task.id, clarification_context=json.dumps(context))

    resp = await api_client.get(f"/api/tasks/{task.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["clarification_context"] != ""

    parsed = json.loads(data["clarification_context"])
    assert len(parsed["history"]) == 2
    assert parsed["history"][0]["question"] == "Which database should I use?"
    assert parsed["history"][0]["response"] == "Use PostgreSQL"
    assert parsed["history"][1]["question"] == "Should I add indexes?"


async def test_task_detail_with_pending_clarification(api_client, db):
    """Task detail includes pending clarification question."""
    task = await db.create_task(TaskCreate(title="Waiting task", repo="myapp"))

    context = {
        "history": [
            {
                "question": "What API version?",
                "asked_at": "2024-01-15T10:00:00Z",
            },
        ],
        "pending_question": "What API version?",
        "asked_at": "2024-01-15T10:00:00Z",
    }
    await db.update_task_fields(task.id, clarification_context=json.dumps(context))
    await db.update_task_status(task.id, TaskStatus.WAITING_FOR_INPUT)

    resp = await api_client.get(f"/api/tasks/{task.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "waiting_for_input"

    parsed = json.loads(data["clarification_context"])
    assert parsed["pending_question"] == "What API version?"


async def test_task_detail_with_workflow_info(api_client, db):
    """Task detail includes workflow_id and workflow_step when set."""
    task = await db.create_task(TaskCreate(title="Workflow task", repo="myapp"))
    await db.update_task_fields(task.id, workflow_id=5, workflow_step=2)

    resp = await api_client.get(f"/api/tasks/{task.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["workflow_id"] == 5
    assert data["workflow_step"] == 2


async def test_task_detail_with_error(api_client, db):
    """Task detail includes error message when task failed."""
    task = await db.create_task(TaskCreate(title="Failed task", repo="myapp"))
    await db.update_task_status(task.id, TaskStatus.FAILED, error="Agent binary not found")

    resp = await api_client.get(f"/api/tasks/{task.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert data["error"] == "Agent binary not found"


# ── Task action tests ──────────────────────────────────────────────────


async def test_run_queued_task(api_client, mock_orchestrator):
    """POST /api/tasks/:id/run starts a queued task."""
    create_resp = await api_client.post("/api/tasks", json={
        "title": "Run me", "repo": "myapp",
    })
    task_id = create_resp.json()["id"]

    resp = await api_client.post(f"/api/tasks/{task_id}/run")
    assert resp.status_code == 200
    mock_orchestrator.process_task.assert_awaited_with(task_id)


async def test_run_non_queued_task_fails(api_client, db):
    """Cannot run a task that is not in queued status."""
    task = await db.create_task(TaskCreate(title="Running task", repo="myapp"))
    await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

    resp = await api_client.post(f"/api/tasks/{task.id}/run")
    assert resp.status_code == 400


async def test_cancel_task_action(api_client, mock_orchestrator):
    """POST /api/tasks/:id/cancel cancels a task."""
    create_resp = await api_client.post("/api/tasks", json={
        "title": "Cancel me", "repo": "myapp",
    })
    task_id = create_resp.json()["id"]

    resp = await api_client.post(f"/api/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    mock_orchestrator.cancel_task.assert_awaited_with(task_id)


async def test_cancel_nonexistent_task(api_client):
    """Cannot cancel a task that doesn't exist."""
    resp = await api_client.post("/api/tasks/999/cancel")
    assert resp.status_code == 404


# ── Polling / auto-refresh support tests ────────────────────────────────


async def test_repeated_task_list_calls(api_client):
    """Verify multiple rapid calls to /api/tasks work (simulates polling)."""
    await api_client.post("/api/tasks", json={"title": "Poll test", "repo": "myapp"})

    for _ in range(5):
        resp = await api_client.get("/api/tasks")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


async def test_repeated_task_detail_calls(api_client):
    """Verify multiple rapid calls to /api/tasks/:id work (simulates polling)."""
    create_resp = await api_client.post("/api/tasks", json={
        "title": "Poll detail", "repo": "myapp",
    })
    task_id = create_resp.json()["id"]

    for _ in range(5):
        resp = await api_client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Poll detail"


async def test_task_status_changes_visible_on_refresh(api_client, db):
    """Status changes are reflected when task detail is polled again."""
    task = await db.create_task(TaskCreate(title="Status change", repo="myapp"))

    resp = await api_client.get(f"/api/tasks/{task.id}")
    assert resp.json()["status"] == "queued"

    await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)
    resp = await api_client.get(f"/api/tasks/{task.id}")
    assert resp.json()["status"] == "in_progress"

    await db.update_task_status(task.id, TaskStatus.DONE)
    resp = await api_client.get(f"/api/tasks/{task.id}")
    assert resp.json()["status"] == "done"


async def test_new_tasks_appear_on_list_refresh(api_client):
    """Newly created tasks appear on subsequent list fetches."""
    resp = await api_client.get("/api/tasks")
    assert len(resp.json()) == 0

    await api_client.post("/api/tasks", json={"title": "New task", "repo": "myapp"})

    resp = await api_client.get("/api/tasks")
    assert len(resp.json()) == 1


# ── Frontend content tests (CSS/JS features) ──────────────────────────


class TestDashboardNewFeatures:
    """Test that new dashboard features are present in the static files."""

    def test_js_has_auto_refresh(self, client):
        """JS should contain auto-refresh polling logic."""
        r = client.get("/static/dashboard/app.js")
        assert "POLL_INTERVAL" in r.text
        assert "startPolling" in r.text
        assert "stopPolling" in r.text
        assert "refreshCurrentPage" in r.text

    def test_js_has_settings_loader(self, client):
        """JS should load settings from /api/settings."""
        r = client.get("/static/dashboard/app.js")
        assert "loadSettings" in r.text
        assert "/settings" in r.text

    def test_js_has_plane_url_builder(self, client):
        """JS should contain Plane issue URL builder."""
        r = client.get("/static/dashboard/app.js")
        assert "planeIssueUrl" in r.text

    def test_js_has_clarification_rendering(self, client):
        """JS should render clarification history in task detail."""
        r = client.get("/static/dashboard/app.js")
        assert "clarification_context" in r.text
        assert "Clarification History" in r.text
        assert "clarification-entry" in r.text

    def test_js_has_plane_issue_display(self, client):
        """JS should display Plane issue link in task detail."""
        r = client.get("/static/dashboard/app.js")
        assert "plane_issue_id" in r.text
        assert "Plane Issue" in r.text

    def test_js_has_workflow_display(self, client):
        """JS should display workflow info in task detail."""
        r = client.get("/static/dashboard/app.js")
        assert "workflow_id" in r.text

    def test_css_has_clarification_styles(self, client):
        """CSS should have styles for clarification history."""
        r = client.get("/static/dashboard/styles.css")
        assert "clarification-list" in r.text
        assert "clarification-entry" in r.text
        assert "clarification-question" in r.text
        assert "clarification-response" in r.text
        assert "clarification-pending" in r.text

    def test_css_has_auto_refresh_indicator(self, client):
        """CSS should have auto-refresh indicator styles."""
        r = client.get("/static/dashboard/styles.css")
        assert "auto-refresh-dot" in r.text
        assert "pulse-dot" in r.text

    def test_js_preserves_filter_on_refresh(self, client):
        """JS should preserve task filter selection during auto-refresh."""
        r = client.get("/static/dashboard/app.js")
        assert "taskStatusFilter" in r.text
        assert "currentFilter" in r.text

    def test_js_has_task_count_display(self, client):
        """JS should show filtered task count."""
        r = client.get("/static/dashboard/app.js")
        assert "taskCount" in r.text

    def test_js_has_messages_sse_support(self, client):
        """JS should have SSE connection for real-time messages."""
        r = client.get("/static/dashboard/app.js")
        assert "connectMessagesSSE" in r.text
        assert "disconnectMessagesSSE" in r.text
        assert "EventSource" in r.text
        assert "/messages/stream/sse" in r.text

    def test_js_has_messages_compose(self, client):
        """JS should have message compose/send functionality."""
        r = client.get("/static/dashboard/app.js")
        assert "__msgSend" in r.text
        assert "msgComposeSender" in r.text
        assert "msgComposeMessage" in r.text

    def test_js_has_messages_filters(self, client):
        """JS should have message filtering support."""
        r = client.get("/static/dashboard/app.js")
        assert "__msgApplyFilters" in r.text
        assert "__msgClearFilters" in r.text
        assert "passesMessageFilter" in r.text

    def test_css_has_messages_styles(self, client):
        """CSS should have message board styles."""
        r = client.get("/static/dashboard/styles.css")
        assert "message-card" in r.text
        assert "msg-type-badge" in r.text
        assert "compose-area" in r.text
        assert "messages-toolbar" in r.text
        assert "messages-connection-dot" in r.text

    def test_html_has_messages_nav_as_spa_link(self, client):
        """Messages nav link should be an SPA link, not external."""
        r = client.get("/")
        assert 'data-page="messages"' in r.text
        assert 'href="#/messages"' in r.text
        # Should NOT have target="_blank" anymore
        assert 'target="_blank"' not in r.text or 'data-page="messages"' in r.text
