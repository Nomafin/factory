"""Tests for Plane OAuth 2.0 authentication."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from factory.auth import (
    _sessions,
    _sign_session_id,
    _verify_session_id,
    cleanup_expired_sessions,
    get_current_session,
    init_oauth,
    is_oauth_enabled,
)
from factory.config import OAuthConfig
from factory.deps import get_db, get_orchestrator
from factory.main import app
from factory.db import Database
from factory.orchestrator import Orchestrator


# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def oauth_config():
    return OAuthConfig(
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="http://localhost:8100/auth/callback",
        authorize_url="https://api.plane.so/auth/o/authorize-app/",
        token_url="https://api.plane.so/auth/o/token/",
        userinfo_url="https://api.plane.so/api/v1/users/me/",
        scopes="read write",
        session_secret="test-session-secret",
    )


@pytest.fixture
def setup_oauth(oauth_config):
    init_oauth(oauth_config)
    yield
    # Clean up sessions between tests
    _sessions.clear()
    init_oauth(None)


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
async def client(db, mock_orchestrator, setup_oauth):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def client_no_oauth(db, mock_orchestrator):
    """Client with OAuth disabled (no config)."""
    init_oauth(OAuthConfig())  # Empty config = no client_id = disabled
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()
    _sessions.clear()
    init_oauth(None)


def _create_test_session() -> str:
    """Create a test session and return the signed session cookie value."""
    session_id = "test-session-id"
    _sessions[session_id] = {
        "access_token": "test-access-token",
        "refresh_token": "test-refresh-token",
        "expires_at": time.time() + 86400,
        "expires_in": 86400,
        "user": {
            "id": "user-123",
            "email": "test@example.com",
            "display_name": "Test User",
            "first_name": "Test",
            "last_name": "User",
            "avatar": "",
        },
    }
    return _sign_session_id(session_id)


# -- Unit tests: session signing/verification --------------------------------


class TestSessionSigning:
    def test_sign_and_verify(self, setup_oauth):
        session_id = "test-session-123"
        signed = _sign_session_id(session_id)
        assert "." in signed
        verified = _verify_session_id(signed)
        assert verified == session_id

    def test_verify_invalid_signature(self, setup_oauth):
        signed = "test-session-123.invalidsig"
        result = _verify_session_id(signed)
        assert result is None

    def test_verify_no_dot(self, setup_oauth):
        result = _verify_session_id("noseparator")
        assert result is None

    def test_verify_tampered(self, setup_oauth):
        signed = _sign_session_id("original-id")
        # Replace the session ID part
        tampered = "tampered-id" + signed[signed.index("."):]
        result = _verify_session_id(tampered)
        assert result is None


# -- Unit tests: session management ------------------------------------------


class TestSessionManagement:
    def test_get_current_session_no_cookie(self, setup_oauth):
        result = get_current_session(None)
        assert result is None

    def test_get_current_session_invalid_cookie(self, setup_oauth):
        result = get_current_session("invalid.cookie")
        assert result is None

    def test_get_current_session_valid(self, setup_oauth):
        signed = _create_test_session()
        result = get_current_session(signed)
        assert result is not None
        assert result["access_token"] == "test-access-token"
        assert result["user"]["email"] == "test@example.com"

    def test_get_current_session_nonexistent(self, setup_oauth):
        signed = _sign_session_id("nonexistent-session")
        result = get_current_session(signed)
        assert result is None


# -- Unit tests: OAuth enabled check -----------------------------------------


class TestOAuthEnabled:
    def test_oauth_enabled_with_config(self, setup_oauth):
        assert is_oauth_enabled() is True

    def test_oauth_disabled_no_config(self):
        init_oauth(None)
        assert is_oauth_enabled() is False
        _sessions.clear()

    def test_oauth_disabled_empty_client_id(self):
        init_oauth(OAuthConfig())
        assert is_oauth_enabled() is False
        _sessions.clear()
        init_oauth(None)


# -- Unit tests: session cleanup ---------------------------------------------


class TestSessionCleanup:
    def test_cleanup_expired_sessions(self, setup_oauth):
        # Add an expired session
        _sessions["expired-1"] = {
            "access_token": "old",
            "expires_at": time.time() - 7200,
        }
        # Add a valid session
        _sessions["valid-1"] = {
            "access_token": "new",
            "expires_at": time.time() + 3600,
        }
        # Add an old state entry
        _sessions["state:old-state"] = {
            "state": "old-state",
            "created_at": time.time() - 1200,
        }

        count = cleanup_expired_sessions()
        assert count == 2
        assert "expired-1" not in _sessions
        assert "state:old-state" not in _sessions
        assert "valid-1" in _sessions

    def test_cleanup_no_expired(self, setup_oauth):
        _sessions["valid-1"] = {
            "access_token": "new",
            "expires_at": time.time() + 3600,
        }
        count = cleanup_expired_sessions()
        assert count == 0


# -- Integration tests: auth endpoints ---------------------------------------


class TestAuthEndpoints:
    async def test_login_redirects(self, client):
        resp = await client.get("/auth/login", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "api.plane.so" in location
        assert "client_id=test-client-id" in location
        assert "response_type=code" in location
        assert "redirect_uri=" in location

    async def test_login_includes_state(self, client):
        resp = await client.get("/auth/login", follow_redirects=False)
        location = resp.headers["location"]
        assert "state=" in location

    async def test_callback_missing_code(self, client):
        resp = await client.get("/auth/callback")
        assert resp.status_code == 400

    async def test_callback_error_param(self, client):
        resp = await client.get(
            "/auth/callback?error=access_denied", follow_redirects=False
        )
        assert resp.status_code == 302
        assert "auth_error=access_denied" in resp.headers["location"]

    @patch("factory.auth.httpx.AsyncClient")
    async def test_callback_success(self, mock_httpx_class, client):
        """Test successful callback with mocked token exchange."""
        mock_http = AsyncMock()
        mock_httpx_class.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_httpx_class.return_value.__aexit__ = AsyncMock(return_value=False)

        # Mock token response
        token_response = MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 86400,
        }

        # Mock user info response
        user_response = MagicMock()
        user_response.status_code = 200
        user_response.json.return_value = {
            "id": "user-456",
            "email": "oauth@example.com",
            "display_name": "OAuth User",
            "first_name": "OAuth",
            "last_name": "User",
            "avatar": "https://example.com/avatar.png",
        }

        mock_http.post = AsyncMock(return_value=token_response)
        mock_http.get = AsyncMock(return_value=user_response)

        resp = await client.get(
            "/auth/callback?code=test-auth-code&state=test-state",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"

        # Check that a session cookie was set
        cookies = resp.headers.get_list("set-cookie")
        assert any("factory_session=" in c for c in cookies)

    @patch("factory.auth.httpx.AsyncClient")
    async def test_callback_token_exchange_fails(self, mock_httpx_class, client):
        """Test callback when token exchange returns non-200."""
        mock_http = AsyncMock()
        mock_httpx_class.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_httpx_class.return_value.__aexit__ = AsyncMock(return_value=False)

        token_response = MagicMock()
        token_response.status_code = 400
        token_response.text = "invalid_grant"
        mock_http.post = AsyncMock(return_value=token_response)

        resp = await client.get(
            "/auth/callback?code=bad-code", follow_redirects=False
        )
        assert resp.status_code == 302
        assert "token_exchange_failed" in resp.headers["location"]

    async def test_me_unauthenticated(self, client):
        resp = await client.get("/auth/me")
        assert resp.status_code == 401

    async def test_me_authenticated(self, client):
        signed = _create_test_session()
        resp = await client.get(
            "/auth/me", cookies={"factory_session": signed}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["user"]["email"] == "test@example.com"
        assert data["user"]["display_name"] == "Test User"

    async def test_status_unauthenticated(self, client):
        resp = await client.get("/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False
        assert data["oauth_enabled"] is True

    async def test_status_authenticated(self, client):
        signed = _create_test_session()
        resp = await client.get(
            "/auth/status", cookies={"factory_session": signed}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["user"]["display_name"] == "Test User"

    async def test_logout(self, client):
        signed = _create_test_session()
        resp = await client.get(
            "/auth/logout",
            cookies={"factory_session": signed},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/auth/login-page" in resp.headers["location"]

        # Session should be removed
        assert "test-session-id" not in _sessions

    async def test_logout_clears_cookie(self, client):
        signed = _create_test_session()
        resp = await client.get(
            "/auth/logout",
            cookies={"factory_session": signed},
            follow_redirects=False,
        )
        cookies = resp.headers.get_list("set-cookie")
        # Should clear the cookie (set max-age=0 or expires in past)
        assert any("factory_session=" in c for c in cookies)


# -- Integration tests: route protection ------------------------------------


class TestRouteProtection:
    async def test_dashboard_redirects_when_oauth_enabled(self, client):
        """Protected routes should redirect to login when OAuth is on."""
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/auth/login-page" in resp.headers["location"]

    async def test_dashboard_accessible_when_authenticated(self, client):
        """Protected routes accessible with valid session."""
        signed = _create_test_session()
        resp = await client.get("/", cookies={"factory_session": signed})
        assert resp.status_code == 200

    async def test_tasks_redirects_unauthenticated(self, client):
        resp = await client.get("/tasks", follow_redirects=False)
        assert resp.status_code == 302

    async def test_agents_redirects_unauthenticated(self, client):
        resp = await client.get("/agents", follow_redirects=False)
        assert resp.status_code == 302

    async def test_preview_redirects_unauthenticated(self, client):
        resp = await client.get("/preview", follow_redirects=False)
        assert resp.status_code == 302

    async def test_analytics_redirects_unauthenticated(self, client):
        resp = await client.get("/analytics", follow_redirects=False)
        assert resp.status_code == 302

    async def test_messages_redirects_unauthenticated(self, client):
        resp = await client.get("/messages", follow_redirects=False)
        assert resp.status_code == 302

    async def test_dashboard_alias_redirects_unauthenticated(self, client):
        resp = await client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 302

    async def test_health_not_protected(self, client):
        """Health endpoint should never require auth."""
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    async def test_api_not_protected_by_oauth(self, client):
        """API endpoints should still work (protected by their own auth)."""
        resp = await client.get("/api/tasks")
        assert resp.status_code == 200

    async def test_login_page_accessible(self, client):
        """Login page should be accessible without auth."""
        resp = await client.get("/auth/login-page")
        assert resp.status_code == 200
        assert "Login with Plane" in resp.text

    async def test_dashboard_accessible_without_oauth(self, client_no_oauth):
        """Dashboard accessible when OAuth is not configured."""
        resp = await client_no_oauth.get("/")
        assert resp.status_code == 200

    async def test_tasks_accessible_without_oauth(self, client_no_oauth):
        resp = await client_no_oauth.get("/tasks")
        assert resp.status_code == 200


# -- Integration tests: token refresh ----------------------------------------


class TestTokenRefresh:
    @patch("factory.auth.httpx.AsyncClient")
    async def test_refresh_token_success(self, mock_httpx_class, setup_oauth):
        from factory.auth import refresh_token_if_needed

        mock_http = AsyncMock()
        mock_httpx_class.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_httpx_class.return_value.__aexit__ = AsyncMock(return_value=False)

        refresh_response = MagicMock()
        refresh_response.status_code = 200
        refresh_response.json.return_value = {
            "access_token": "refreshed-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 86400,
        }
        mock_http.post = AsyncMock(return_value=refresh_response)

        session = {
            "access_token": "old-token",
            "refresh_token": "old-refresh-token",
            "expires_at": time.time() - 100,  # Expired
        }

        result = await refresh_token_if_needed(session)
        assert result is True
        assert session["access_token"] == "refreshed-token"
        assert session["refresh_token"] == "new-refresh-token"

    async def test_refresh_not_needed(self, setup_oauth):
        from factory.auth import refresh_token_if_needed

        session = {
            "access_token": "valid-token",
            "refresh_token": "refresh-token",
            "expires_at": time.time() + 3600,  # Still valid
        }

        result = await refresh_token_if_needed(session)
        assert result is True
        assert session["access_token"] == "valid-token"

    async def test_refresh_no_refresh_token(self, setup_oauth):
        from factory.auth import refresh_token_if_needed

        session = {
            "access_token": "old-token",
            "refresh_token": "",
            "expires_at": time.time() - 100,
        }

        result = await refresh_token_if_needed(session)
        assert result is False
