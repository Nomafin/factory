"""Tests for the analytics and metrics dashboard feature."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from factory.config import Config
from factory.db import Database
from factory.deps import get_config, get_db, get_orchestrator
from factory.main import app
from factory.models import TaskCreate, TaskStatus, WorkflowStatus
from factory.orchestrator import Orchestrator


@asynccontextmanager
async def _null_lifespan(app):
    yield


@pytest.fixture
def client():
    """Create a test client for the FastAPI app."""
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


# ── Analytics API endpoint tests ──────────────────────────────────────


class TestAnalyticsEndpoint:
    """Test the /api/analytics endpoint returns correct aggregated data."""

    async def test_analytics_empty(self, api_client):
        """Analytics endpoint returns zeros when no tasks exist."""
        resp = await api_client.get("/api/analytics")
        assert resp.status_code == 200
        data = resp.json()

        assert data["summary"]["total_tasks"] == 0
        assert data["summary"]["success_rate"] == 0
        assert data["summary"]["failure_rate"] == 0
        assert data["summary"]["done"] == 0
        assert data["summary"]["failed"] == 0

    async def test_analytics_with_tasks(self, api_client, db):
        """Analytics endpoint computes correct metrics from tasks."""
        t1 = await db.create_task(TaskCreate(title="Done task", repo="r"))
        await db.update_task_status(t1.id, TaskStatus.IN_PROGRESS)
        await db.update_task_status(t1.id, TaskStatus.DONE)

        t2 = await db.create_task(TaskCreate(title="Failed task", repo="r"))
        await db.update_task_status(t2.id, TaskStatus.IN_PROGRESS)
        await db.update_task_status(t2.id, TaskStatus.FAILED)

        t3 = await db.create_task(TaskCreate(title="Queued task", repo="r"))

        resp = await api_client.get("/api/analytics")
        assert resp.status_code == 200
        data = resp.json()

        assert data["summary"]["total_tasks"] == 3
        assert data["summary"]["done"] == 1
        assert data["summary"]["failed"] == 1
        assert data["summary"]["queued"] == 1

    async def test_analytics_success_rate(self, api_client, db):
        """Analytics correctly calculates success and failure rates."""
        for i in range(3):
            t = await db.create_task(TaskCreate(title=f"Done {i}", repo="r"))
            await db.update_task_status(t.id, TaskStatus.DONE)

        t_fail = await db.create_task(TaskCreate(title="Failed", repo="r"))
        await db.update_task_status(t_fail.id, TaskStatus.FAILED)

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        assert data["summary"]["success_rate"] == 75.0
        assert data["summary"]["failure_rate"] == 25.0

    async def test_analytics_status_breakdown(self, api_client, db):
        """Analytics returns correct status breakdown counts."""
        t1 = await db.create_task(TaskCreate(title="Done", repo="r"))
        await db.update_task_status(t1.id, TaskStatus.DONE)

        t2 = await db.create_task(TaskCreate(title="Failed", repo="r"))
        await db.update_task_status(t2.id, TaskStatus.FAILED)

        await db.create_task(TaskCreate(title="Queued 1", repo="r"))
        await db.create_task(TaskCreate(title="Queued 2", repo="r"))

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        assert data["status_breakdown"]["done"] == 1
        assert data["status_breakdown"]["failed"] == 1
        assert data["status_breakdown"]["queued"] == 2

    async def test_analytics_duration_metrics(self, api_client, db):
        """Analytics computes duration statistics for completed tasks."""
        t = await db.create_task(TaskCreate(title="Timed task", repo="r"))
        now = datetime.now(timezone.utc)
        started = (now - timedelta(minutes=30)).isoformat()
        completed = now.isoformat()
        await db.update_task_fields(t.id, status="done", started_at=started, completed_at=completed)

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        assert data["duration"]["sample_count"] == 1
        assert data["duration"]["avg_minutes"] > 0
        assert data["duration"]["min_minutes"] > 0
        assert data["duration"]["max_minutes"] > 0
        assert data["duration"]["median_minutes"] > 0

    async def test_analytics_duration_empty_when_no_completed(self, api_client, db):
        """Duration metrics show zeros when no tasks have been completed."""
        await db.create_task(TaskCreate(title="Queued", repo="r"))

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        assert data["duration"]["sample_count"] == 0
        assert data["duration"]["avg_minutes"] == 0

    async def test_analytics_agent_performance(self, api_client, db):
        """Analytics breaks down performance by agent type."""
        t1 = await db.create_task(TaskCreate(title="Coder done", repo="r", agent_type="coder"))
        await db.update_task_status(t1.id, TaskStatus.DONE)

        t2 = await db.create_task(TaskCreate(title="Coder fail", repo="r", agent_type="coder"))
        await db.update_task_status(t2.id, TaskStatus.FAILED)

        t3 = await db.create_task(TaskCreate(title="Reviewer done", repo="r", agent_type="reviewer"))
        await db.update_task_status(t3.id, TaskStatus.DONE)

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        agents = {a["agent_type"]: a for a in data["agent_performance"]}
        assert "coder" in agents
        assert "reviewer" in agents
        assert agents["coder"]["total"] == 2
        assert agents["coder"]["done"] == 1
        assert agents["coder"]["failed"] == 1
        assert agents["coder"]["success_rate"] == 50.0
        assert agents["reviewer"]["total"] == 1
        assert agents["reviewer"]["done"] == 1
        assert agents["reviewer"]["success_rate"] == 100.0

    async def test_analytics_daily_trends(self, api_client, db):
        """Analytics returns 30 days of trend data."""
        await db.create_task(TaskCreate(title="Today task", repo="r"))

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        assert len(data["daily_trends"]) == 30
        for day in data["daily_trends"]:
            assert "date" in day
            assert "created" in day
            assert "completed" in day
            assert "failed" in day

    async def test_analytics_daily_trends_today_has_count(self, api_client, db):
        """Tasks created today appear in the daily trends."""
        await db.create_task(TaskCreate(title="Task 1", repo="r"))
        await db.create_task(TaskCreate(title="Task 2", repo="r"))

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        today = data["daily_trends"][-1]
        assert today["created"] == 2

    async def test_analytics_workflow_metrics(self, api_client, db):
        """Analytics returns workflow metrics."""
        wf1 = await db.create_workflow("code_review", "WF1", repo="r")
        await db.update_workflow_status(wf1.id, WorkflowStatus.COMPLETED)

        wf2 = await db.create_workflow("code_review", "WF2", repo="r")
        await db.update_workflow_status(wf2.id, WorkflowStatus.FAILED)

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        assert data["workflows"]["total"] == 2
        assert data["workflows"]["completed"] == 1
        assert data["workflows"]["failed"] == 1
        assert data["workflows"]["success_rate"] == 50.0

    async def test_analytics_workflow_metrics_empty(self, api_client):
        """Workflow metrics handle zero workflows gracefully."""
        resp = await api_client.get("/api/analytics")
        data = resp.json()

        assert data["workflows"]["total"] == 0
        assert data["workflows"]["success_rate"] == 0

    async def test_analytics_multiple_agent_types(self, api_client, db):
        """Analytics handles many different agent types."""
        for agent_type in ["coder", "reviewer", "researcher", "devops"]:
            t = await db.create_task(TaskCreate(
                title=f"{agent_type} task", repo="r", agent_type=agent_type
            ))
            await db.update_task_status(t.id, TaskStatus.DONE)

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        agent_types = [a["agent_type"] for a in data["agent_performance"]]
        assert "coder" in agent_types
        assert "reviewer" in agent_types
        assert "researcher" in agent_types
        assert "devops" in agent_types

    async def test_analytics_all_cancelled(self, api_client, db):
        """Analytics handles edge case where all tasks are cancelled."""
        for i in range(3):
            t = await db.create_task(TaskCreate(title=f"Cancelled {i}", repo="r"))
            await db.update_task_status(t.id, TaskStatus.CANCELLED)

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        assert data["summary"]["total_tasks"] == 3
        assert data["summary"]["cancelled"] == 3
        assert data["summary"]["success_rate"] == 0
        assert data["summary"]["failure_rate"] == 0

    async def test_analytics_response_structure(self, api_client):
        """Analytics response has all expected top-level keys."""
        resp = await api_client.get("/api/analytics")
        data = resp.json()

        assert "summary" in data
        assert "status_breakdown" in data
        assert "duration" in data
        assert "agent_performance" in data
        assert "daily_trends" in data
        assert "workflows" in data

    async def test_analytics_duration_multiple_tasks(self, api_client, db):
        """Duration stats computed correctly across multiple completed tasks."""
        now = datetime.now(timezone.utc)

        t1 = await db.create_task(TaskCreate(title="Short task", repo="r"))
        await db.update_task_fields(
            t1.id, status="done",
            started_at=(now - timedelta(minutes=10)).isoformat(),
            completed_at=now.isoformat(),
        )

        t2 = await db.create_task(TaskCreate(title="Long task", repo="r"))
        await db.update_task_fields(
            t2.id, status="done",
            started_at=(now - timedelta(minutes=30)).isoformat(),
            completed_at=now.isoformat(),
        )

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        assert data["duration"]["sample_count"] == 2
        assert 19 <= data["duration"]["avg_minutes"] <= 21
        assert 9 <= data["duration"]["min_minutes"] <= 11
        assert 29 <= data["duration"]["max_minutes"] <= 31

    async def test_analytics_agent_avg_duration(self, api_client, db):
        """Agent performance includes average duration for completed tasks."""
        now = datetime.now(timezone.utc)
        t = await db.create_task(TaskCreate(title="Timed coder", repo="r", agent_type="coder"))
        await db.update_task_fields(
            t.id, status="done",
            started_at=(now - timedelta(minutes=15)).isoformat(),
            completed_at=now.isoformat(),
        )

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        agents = {a["agent_type"]: a for a in data["agent_performance"]}
        assert agents["coder"]["avg_duration"] > 0


# ── Frontend content tests for analytics ──────────────────────────────


class TestAnalyticsFrontend:
    """Test that frontend files contain analytics features."""

    def test_html_includes_chart_js(self, client):
        """index.html should include the Chart.js CDN script."""
        r = client.get("/")
        assert "chart.js" in r.text or "chart.umd" in r.text

    def test_js_has_render_analytics(self, client):
        """JS should have the renderAnalytics function."""
        r = client.get("/static/dashboard/app.js")
        assert "renderAnalytics" in r.text

    def test_js_has_chart_rendering(self, client):
        """JS should have chart rendering logic."""
        r = client.get("/static/dashboard/app.js")
        assert "renderAnalyticsCharts" in r.text
        assert "statusChart" in r.text
        assert "trendsChart" in r.text
        assert "agentChart" in r.text

    def test_js_uses_analytics_api(self, client):
        """JS should fetch from /api/analytics endpoint."""
        r = client.get("/static/dashboard/app.js")
        assert "/analytics" in r.text

    def test_js_has_chart_colors(self, client):
        """JS should define chart color mappings for task statuses."""
        r = client.get("/static/dashboard/app.js")
        assert "chartColors" in r.text

    def test_js_has_duration_formatter(self, client):
        """JS should have a duration formatting function."""
        r = client.get("/static/dashboard/app.js")
        assert "formatDuration" in r.text

    def test_js_destroys_charts_on_refresh(self, client):
        """JS should destroy old charts before re-rendering."""
        r = client.get("/static/dashboard/app.js")
        assert "destroyAnalyticsCharts" in r.text
        assert "_analyticsCharts" in r.text

    def test_js_has_agent_performance_table(self, client):
        """JS should render an agent performance table."""
        r = client.get("/static/dashboard/app.js")
        assert "Agent Performance" in r.text
        assert "agent_performance" in r.text

    def test_js_has_duration_metrics_section(self, client):
        """JS should render duration metrics section."""
        r = client.get("/static/dashboard/app.js")
        assert "Duration Metrics" in r.text
        assert "analytics-metric" in r.text

    def test_js_has_workflow_metrics(self, client):
        """JS should render workflow metrics section."""
        r = client.get("/static/dashboard/app.js")
        assert "Workflow Metrics" in r.text

    def test_js_has_trend_chart(self, client):
        """JS should render a trends chart with created/completed/failed datasets."""
        r = client.get("/static/dashboard/app.js")
        assert "Task Trends" in r.text
        assert "daily_trends" in r.text

    def test_js_has_status_distribution_chart(self, client):
        """JS should render a status distribution donut chart."""
        r = client.get("/static/dashboard/app.js")
        assert "Task Status Distribution" in r.text
        assert "doughnut" in r.text

    def test_js_has_status_breakdown_bars(self, client):
        """JS should render status breakdown with progress bars."""
        r = client.get("/static/dashboard/app.js")
        assert "Status Breakdown" in r.text
        assert "analytics-status-bar" in r.text

    def test_js_has_recent_activity(self, client):
        """JS should render recent activity section."""
        r = client.get("/static/dashboard/app.js")
        assert "Recent Activity" in r.text

    def test_css_has_analytics_chart_styles(self, client):
        """CSS should have styles for analytics charts."""
        r = client.get("/static/dashboard/styles.css")
        assert "analytics-charts-row" in r.text
        assert "analytics-chart-container" in r.text
        assert "analytics-chart-card" in r.text

    def test_css_has_analytics_metrics_styles(self, client):
        """CSS should have styles for analytics metric cards."""
        r = client.get("/static/dashboard/styles.css")
        assert "analytics-metrics-grid" in r.text
        assert "analytics-metric-value" in r.text
        assert "analytics-metric-label" in r.text

    def test_css_has_analytics_status_bar_styles(self, client):
        """CSS should have styles for analytics status bars."""
        r = client.get("/static/dashboard/styles.css")
        assert "analytics-status-bars" in r.text
        assert "analytics-status-bar-fill" in r.text
        assert "analytics-status-row" in r.text

    def test_css_has_agent_type_badge(self, client):
        """CSS should have agent type badge styling."""
        r = client.get("/static/dashboard/styles.css")
        assert "agent-type-badge" in r.text

    def test_css_has_responsive_analytics(self, client):
        """CSS should have responsive breakpoints for analytics charts."""
        r = client.get("/static/dashboard/styles.css")
        assert "analytics-charts-row" in r.text

    def test_css_has_analytics_table_styles(self, client):
        """CSS should have analytics table styles."""
        r = client.get("/static/dashboard/styles.css")
        assert "analytics-table" in r.text


# ── Analytics data edge cases ─────────────────────────────────────────


class TestAnalyticsEdgeCases:
    """Test edge cases in analytics data computation."""

    async def test_analytics_single_task_median(self, api_client, db):
        """Median works correctly with a single completed task."""
        now = datetime.now(timezone.utc)
        t = await db.create_task(TaskCreate(title="Solo", repo="r"))
        await db.update_task_fields(
            t.id, status="done",
            started_at=(now - timedelta(minutes=20)).isoformat(),
            completed_at=now.isoformat(),
        )

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        assert data["duration"]["sample_count"] == 1
        assert abs(data["duration"]["median_minutes"] - data["duration"]["avg_minutes"]) < 1

    async def test_analytics_even_count_median(self, api_client, db):
        """Median calculation works for even number of completed tasks."""
        now = datetime.now(timezone.utc)

        for minutes in [10, 20, 30, 40]:
            t = await db.create_task(TaskCreate(title=f"{minutes}m task", repo="r"))
            await db.update_task_fields(
                t.id, status="done",
                started_at=(now - timedelta(minutes=minutes)).isoformat(),
                completed_at=now.isoformat(),
            )

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        assert data["duration"]["sample_count"] == 4
        assert 24 <= data["duration"]["median_minutes"] <= 26

    async def test_analytics_in_progress_tasks_excluded_from_duration(self, api_client, db):
        """In-progress tasks are not included in duration calculations."""
        t = await db.create_task(TaskCreate(title="Running", repo="r"))
        await db.update_task_status(t.id, TaskStatus.IN_PROGRESS)

        resp = await api_client.get("/api/analytics")
        data = resp.json()

        assert data["duration"]["sample_count"] == 0
        assert data["summary"]["in_progress"] == 1

    async def test_analytics_large_task_count(self, api_client, db):
        """Analytics handles many tasks without errors."""
        for i in range(50):
            t = await db.create_task(TaskCreate(title=f"Task {i}", repo="r"))
            if i % 3 == 0:
                await db.update_task_status(t.id, TaskStatus.DONE)
            elif i % 3 == 1:
                await db.update_task_status(t.id, TaskStatus.FAILED)

        resp = await api_client.get("/api/analytics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["total_tasks"] == 50

    async def test_analytics_repeated_calls(self, api_client, db):
        """Multiple rapid calls to analytics endpoint work (simulates polling)."""
        await db.create_task(TaskCreate(title="Poll test", repo="r"))

        for _ in range(5):
            resp = await api_client.get("/api/analytics")
            assert resp.status_code == 200
