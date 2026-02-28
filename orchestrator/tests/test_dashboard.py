"""Tests for the Factory dashboard web UI routes."""

import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from factory.main import app, DASHBOARD_DIR, STATIC_DIR


@pytest.fixture
def client():
    """Create a test client for the FastAPI app."""
    # Skip lifespan to avoid needing real config/db
    app.router.lifespan_context = _null_lifespan
    return TestClient(app)


from contextlib import asynccontextmanager


@asynccontextmanager
async def _null_lifespan(app):
    yield


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

    def test_messages_route(self, client):
        """GET /messages should still serve the message board."""
        r = client.get("/messages")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Agent Message Board" in r.text

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
