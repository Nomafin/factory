"""Tests for the preview environments API and dashboard UI.

Tests the Docker container listing, teardown endpoints, and frontend
integration for the preview environments management page.
"""

import json
import subprocess
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from factory.api import _list_factory_containers, _remove_container
from factory.config import Config
from factory.db import Database
from factory.deps import get_config, get_db, get_orchestrator
from factory.main import app
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


# ── _list_factory_containers unit tests ─────────────────────────────────


DOCKER_PS_OUTPUT_LINE = (
    "abc123def456\tfactory-task-42-app-1\tUp 5 minutes (healthy)\t"
    "0.0.0.0:3000->3000/tcp\t"
    "factory.task-id=42,factory.repo=acme/webapp,factory.env-type=test,"
    "factory.created={created}\t"
    "2024-01-15 10:00:00 +0000 UTC"
)


class TestListFactoryContainers:
    """Tests for the _list_factory_containers helper function."""

    @patch("factory.api.subprocess.run")
    def test_returns_containers_from_docker(self, mock_run):
        """Should parse docker ps output into container dicts."""
        created = str(int(time.time()) - 300)
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=DOCKER_PS_OUTPUT_LINE.format(created=created),
        )

        containers = _list_factory_containers()

        assert len(containers) == 1
        c = containers[0]
        assert c["container_id"] == "abc123def456"
        assert c["name"] == "factory-task-42-app-1"
        assert c["task_id"] == "42"
        assert c["env_type"] == "test"
        assert c["repo"] == "acme/webapp"
        assert c["health"] == "healthy"
        assert c["url"] == "https://task-42.preview.factory.6a.fi"
        assert c["created_ts"] == created
        assert c["age_seconds"] >= 299  # approximately 300s

    @patch("factory.api.subprocess.run")
    def test_returns_empty_on_docker_failure(self, mock_run):
        """Should return empty list when docker command fails."""
        mock_run.return_value = MagicMock(returncode=1, stderr="daemon error")

        containers = _list_factory_containers()
        assert containers == []

    @patch("factory.api.subprocess.run")
    def test_returns_empty_on_timeout(self, mock_run):
        """Should return empty list when docker command times out."""
        mock_run.side_effect = subprocess.TimeoutExpired("docker", 15)

        containers = _list_factory_containers()
        assert containers == []

    @patch("factory.api.subprocess.run")
    def test_returns_empty_on_docker_not_found(self, mock_run):
        """Should return empty list when docker is not installed."""
        mock_run.side_effect = FileNotFoundError("docker not found")

        containers = _list_factory_containers()
        assert containers == []

    @patch("factory.api.subprocess.run")
    def test_returns_empty_on_no_output(self, mock_run):
        """Should return empty list when docker returns no containers."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        containers = _list_factory_containers()
        assert containers == []

    @patch("factory.api.subprocess.run")
    def test_parses_multiple_containers(self, mock_run):
        """Should parse multiple container lines."""
        created = str(int(time.time()))
        line1 = (
            "aaa111\tapp-1\tUp 5 minutes (healthy)\t3000/tcp\t"
            f"factory.task-id=42,factory.env-type=test,factory.created={created}\t"
            "2024-01-15 10:00:00 +0000 UTC"
        )
        line2 = (
            "bbb222\tapp-2\tUp 2 hours\t8080/tcp\t"
            f"factory.task-id=43,factory.env-type=preview,factory.pr-number=15,factory.created={created}\t"
            "2024-01-15 08:00:00 +0000 UTC"
        )
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"{line1}\n{line2}",
        )

        containers = _list_factory_containers()
        assert len(containers) == 2
        assert containers[0]["task_id"] == "42"
        assert containers[0]["env_type"] == "test"
        assert containers[1]["task_id"] == "43"
        assert containers[1]["env_type"] == "preview"

    @patch("factory.api.subprocess.run")
    def test_preview_url_for_pr(self, mock_run):
        """Preview containers should use pr-N hostname."""
        created = str(int(time.time()))
        line = (
            "abc123\tpr-app-1\tUp 1 hour\t3000/tcp\t"
            f"factory.task-id=42,factory.env-type=preview,factory.pr-number=15,factory.created={created}\t"
            "2024-01-15 10:00:00 +0000 UTC"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=line)

        containers = _list_factory_containers()
        assert len(containers) == 1
        assert containers[0]["url"] == "https://pr-15.preview.factory.6a.fi"

    @patch("factory.api.subprocess.run")
    def test_health_states(self, mock_run):
        """Should parse various Docker health states."""
        created = str(int(time.time()))
        lines = []
        statuses = [
            ("Up 5 minutes (healthy)", "healthy"),
            ("Up 1 minute (health: starting)", "starting"),
            ("Up 10 minutes (unhealthy)", "unhealthy"),
            ("Up 2 hours", "running"),
            ("Exited (0) 5 minutes ago", "stopped"),
            ("Created", "created"),
        ]
        for i, (status, _) in enumerate(statuses):
            lines.append(
                f"id{i}\tname-{i}\t{status}\t3000/tcp\t"
                f"factory.task-id={i},factory.env-type=test,factory.created={created}\t"
                "2024-01-15 10:00:00"
            )

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="\n".join(lines),
        )

        containers = _list_factory_containers()
        assert len(containers) == len(statuses)
        for i, (_, expected_health) in enumerate(statuses):
            assert containers[i]["health"] == expected_health, (
                f"Container {i}: expected health={expected_health}, "
                f"got {containers[i]['health']} for status='{statuses[i][0]}'"
            )

    @patch("factory.api.subprocess.run")
    def test_skips_malformed_lines(self, mock_run):
        """Should skip lines with too few tab-separated fields."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="incomplete\tline\n",
        )

        containers = _list_factory_containers()
        assert containers == []

    @patch("factory.api.subprocess.run")
    def test_handles_empty_created_ts(self, mock_run):
        """Should handle missing created timestamp gracefully."""
        line = (
            "abc123\tapp-1\tUp 5 minutes\t3000/tcp\t"
            "factory.task-id=42,factory.env-type=test\t"
            "2024-01-15 10:00:00"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=line)

        containers = _list_factory_containers()
        assert len(containers) == 1
        assert containers[0]["age_seconds"] == 0
        assert containers[0]["created_ts"] == ""


# ── _remove_container unit tests ────────────────────────────────────────


class TestRemoveContainer:
    """Tests for the _remove_container helper function."""

    @patch("factory.api.subprocess.run")
    def test_successful_removal(self, mock_run):
        """Should stop and remove the container."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        result = _remove_container("abc123")

        assert result["status"] == "removed"
        assert result["container_id"] == "abc123"
        assert mock_run.call_count == 2

        # First call: docker stop
        stop_cmd = mock_run.call_args_list[0][0][0]
        assert "stop" in stop_cmd
        assert "abc123" in stop_cmd

        # Second call: docker rm
        rm_cmd = mock_run.call_args_list[1][0][0]
        assert "rm" in rm_cmd
        assert "abc123" in rm_cmd

    @patch("factory.api.subprocess.run")
    def test_removal_failure(self, mock_run):
        """Should return error when docker rm fails."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),  # stop succeeds
            MagicMock(returncode=1, stderr="no such container"),  # rm fails
        ]

        result = _remove_container("abc123")
        assert result["status"] == "error"
        assert "no such container" in result["error"]

    @patch("factory.api.subprocess.run")
    def test_timeout_handling(self, mock_run):
        """Should handle timeout gracefully."""
        mock_run.side_effect = subprocess.TimeoutExpired("docker", 30)

        result = _remove_container("abc123")
        assert result["status"] == "error"
        assert "timed out" in result["error"]

    @patch("factory.api.subprocess.run")
    def test_docker_not_available(self, mock_run):
        """Should handle Docker not being installed."""
        mock_run.side_effect = FileNotFoundError("docker not found")

        result = _remove_container("abc123")
        assert result["status"] == "error"
        assert "not available" in result["error"]

    @patch("factory.api.subprocess.run")
    def test_stop_failure_continues_to_rm(self, mock_run):
        """Should still try rm even if stop fails."""
        mock_run.side_effect = [
            MagicMock(returncode=1, stderr="already stopped"),  # stop fails
            MagicMock(returncode=0, stderr=""),  # rm succeeds
        ]

        result = _remove_container("abc123")
        assert result["status"] == "removed"
        assert mock_run.call_count == 2


# ── API endpoint tests ──────────────────────────────────────────────────


class TestPreviewEnvironmentsAPI:
    """Tests for the preview environments API endpoints."""

    @patch("factory.api._list_factory_containers")
    async def test_list_preview_environments(self, mock_list, api_client):
        """GET /api/preview-environments returns container list."""
        mock_list.return_value = [
            {
                "container_id": "abc123",
                "name": "factory-task-42-app-1",
                "task_id": "42",
                "env_type": "test",
                "repo": "acme/webapp",
                "url": "https://task-42.preview.factory.6a.fi",
                "status": "Up 5 minutes (healthy)",
                "health": "healthy",
                "ports": "3000/tcp",
                "created_at": "2024-01-15 10:00:00",
                "created_ts": "1705312800",
                "age_seconds": 300,
            },
        ]

        resp = await api_client.get("/api/preview-environments")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["container_id"] == "abc123"
        assert data[0]["name"] == "factory-task-42-app-1"
        assert data[0]["task_id"] == "42"
        assert data[0]["env_type"] == "test"
        assert data[0]["url"] == "https://task-42.preview.factory.6a.fi"
        assert data[0]["health"] == "healthy"

    @patch("factory.api._list_factory_containers")
    async def test_list_preview_environments_empty(self, mock_list, api_client):
        """GET /api/preview-environments returns empty list when no containers."""
        mock_list.return_value = []

        resp = await api_client.get("/api/preview-environments")
        assert resp.status_code == 200
        assert resp.json() == []

    @patch("factory.api._list_factory_containers")
    async def test_list_multiple_environments(self, mock_list, api_client):
        """GET /api/preview-environments returns multiple containers."""
        mock_list.return_value = [
            {"container_id": "aaa", "name": "app-1", "task_id": "1",
             "env_type": "test", "repo": "", "url": "", "status": "Up",
             "health": "running", "ports": "", "created_at": "",
             "created_ts": "", "age_seconds": 0},
            {"container_id": "bbb", "name": "app-2", "task_id": "2",
             "env_type": "preview", "repo": "", "url": "", "status": "Up",
             "health": "healthy", "ports": "", "created_at": "",
             "created_ts": "", "age_seconds": 0},
        ]

        resp = await api_client.get("/api/preview-environments")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    @patch("factory.api._remove_container")
    @patch("factory.api._list_factory_containers")
    async def test_delete_preview_environment(self, mock_list, mock_remove, api_client):
        """DELETE /api/preview-environments/:id removes container."""
        mock_list.return_value = [
            {"container_id": "abc123", "name": "app-1", "task_id": "42",
             "env_type": "test", "repo": "", "url": "", "status": "Up",
             "health": "running", "ports": "", "created_at": "",
             "created_ts": "", "age_seconds": 0},
        ]
        mock_remove.return_value = {"status": "removed", "container_id": "abc123"}

        resp = await api_client.delete("/api/preview-environments/abc123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "removed"
        assert data["container_id"] == "abc123"

    @patch("factory.api._list_factory_containers")
    async def test_delete_nonexistent_container(self, mock_list, api_client):
        """DELETE /api/preview-environments/:id returns 404 for unknown container."""
        mock_list.return_value = []

        resp = await api_client.delete("/api/preview-environments/nonexistent")
        assert resp.status_code == 404

    @patch("factory.api._remove_container")
    @patch("factory.api._list_factory_containers")
    async def test_delete_container_docker_error(self, mock_list, mock_remove, api_client):
        """DELETE /api/preview-environments/:id returns 500 on Docker error."""
        mock_list.return_value = [
            {"container_id": "abc123", "name": "app-1", "task_id": "42",
             "env_type": "test", "repo": "", "url": "", "status": "Up",
             "health": "running", "ports": "", "created_at": "",
             "created_ts": "", "age_seconds": 0},
        ]
        mock_remove.return_value = {"status": "error", "error": "Docker daemon unreachable"}

        resp = await api_client.delete("/api/preview-environments/abc123")
        assert resp.status_code == 500
        assert "Docker daemon unreachable" in resp.json()["detail"]

    @patch("factory.api._list_factory_containers")
    async def test_delete_validates_factory_container(self, mock_list, api_client):
        """DELETE only works on containers that are Factory containers."""
        mock_list.return_value = [
            {"container_id": "other123", "name": "other", "task_id": "1",
             "env_type": "test", "repo": "", "url": "", "status": "Up",
             "health": "running", "ports": "", "created_at": "",
             "created_ts": "", "age_seconds": 0},
        ]

        resp = await api_client.delete("/api/preview-environments/abc123")
        assert resp.status_code == 404

    @patch("factory.api._remove_container")
    @patch("factory.api._list_factory_containers")
    async def test_delete_matches_container_id_prefix(self, mock_list, mock_remove, api_client):
        """DELETE should match container IDs by prefix (short IDs)."""
        mock_list.return_value = [
            {"container_id": "abc123def456789", "name": "app-1", "task_id": "42",
             "env_type": "test", "repo": "", "url": "", "status": "Up",
             "health": "running", "ports": "", "created_at": "",
             "created_ts": "", "age_seconds": 0},
        ]
        mock_remove.return_value = {"status": "removed", "container_id": "abc123def456789"}

        resp = await api_client.delete("/api/preview-environments/abc123def456789")
        assert resp.status_code == 200


# ── Frontend content tests ──────────────────────────────────────────────


class TestPreviewEnvironmentsFrontend:
    """Test that dashboard frontend has preview environment features."""

    def test_js_has_preview_api_call(self, client):
        """JS should fetch from /api/preview-environments."""
        r = client.get("/static/dashboard/app.js")
        assert "/preview-environments" in r.text

    def test_js_has_health_badge(self, client):
        """JS should contain health badge rendering."""
        r = client.get("/static/dashboard/app.js")
        assert "healthBadge" in r.text
        assert "health-badge" in r.text

    def test_js_has_env_type_badge(self, client):
        """JS should contain environment type badge rendering."""
        r = client.get("/static/dashboard/app.js")
        assert "envTypeBadge" in r.text
        assert "env-type-badge" in r.text

    def test_js_has_format_age(self, client):
        """JS should contain age formatting function."""
        r = client.get("/static/dashboard/app.js")
        assert "formatAge" in r.text

    def test_js_has_teardown_function(self, client):
        """JS should contain teardown function."""
        r = client.get("/static/dashboard/app.js")
        assert "__teardownEnv" in r.text
        assert "DELETE" in r.text

    def test_js_has_open_url_button(self, client):
        """JS should render Open URL button."""
        r = client.get("/static/dashboard/app.js")
        assert "Open URL" in r.text

    def test_js_has_teardown_button(self, client):
        """JS should render Teardown button."""
        r = client.get("/static/dashboard/app.js")
        assert "Teardown" in r.text

    def test_js_shows_container_info(self, client):
        """JS should display container metadata fields."""
        r = client.get("/static/dashboard/app.js")
        assert "container_id" in r.text
        assert "env_type" in r.text
        assert "age_seconds" in r.text

    def test_js_has_confirm_teardown(self, client):
        """JS should confirm before teardown."""
        r = client.get("/static/dashboard/app.js")
        assert "confirm(" in r.text

    def test_css_has_health_badge_styles(self, client):
        """CSS should have health badge styles."""
        r = client.get("/static/dashboard/styles.css")
        assert "health-badge" in r.text
        assert "health-healthy" in r.text
        assert "health-unhealthy" in r.text
        assert "health-starting" in r.text
        assert "health-stopped" in r.text
        assert "health-running" in r.text

    def test_css_has_env_type_badge_styles(self, client):
        """CSS should have environment type badge styles."""
        r = client.get("/static/dashboard/styles.css")
        assert "env-type-badge" in r.text
        assert "env-type-preview" in r.text
        assert "env-type-test" in r.text

    def test_css_has_preview_card_actions(self, client):
        """CSS should have preview card action bar styles."""
        r = client.get("/static/dashboard/styles.css")
        assert "preview-card-actions" in r.text

    def test_css_has_preview_card_title(self, client):
        """CSS should have preview card title style."""
        r = client.get("/static/dashboard/styles.css")
        assert "preview-card-title" in r.text

    def test_css_has_preview_card_tags(self, client):
        """CSS should have preview card tags style."""
        r = client.get("/static/dashboard/styles.css")
        assert "preview-card-tags" in r.text


# ── Polling / auto-refresh for preview environments ────────────────────


class TestPreviewEnvironmentsPolling:
    """Test that preview environments page supports auto-refresh."""

    @patch("factory.api._list_factory_containers")
    async def test_repeated_list_calls(self, mock_list, api_client):
        """Multiple rapid calls to /api/preview-environments work (simulates polling)."""
        mock_list.return_value = [
            {"container_id": "abc123", "name": "app-1", "task_id": "42",
             "env_type": "test", "repo": "", "url": "", "status": "Up",
             "health": "running", "ports": "", "created_at": "",
             "created_ts": "", "age_seconds": 0},
        ]

        for _ in range(5):
            resp = await api_client.get("/api/preview-environments")
            assert resp.status_code == 200
            assert len(resp.json()) == 1

    def test_js_has_auto_refresh_for_preview(self, client):
        """JS should include auto-refresh indicator on preview page."""
        r = client.get("/static/dashboard/app.js")
        assert "auto-refresh-dot" in r.text
