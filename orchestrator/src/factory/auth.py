"""Plane OAuth 2.0 authentication for the Factory dashboard.

Implements the Authorization Code flow:
1. /auth/login  -> redirect to Plane consent screen
2. /auth/callback -> exchange code for tokens, create session
3. /auth/logout -> clear session
4. /auth/me -> return current user info
"""

import hashlib
import hmac
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from factory.config import OAuthConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# In-memory session store: session_id -> session_data
_sessions: dict[str, dict] = {}

# OAuth config is injected via dependency
_oauth_config: OAuthConfig | None = None


def init_oauth(config: OAuthConfig) -> None:
    """Initialise the OAuth module with configuration."""
    global _oauth_config
    _oauth_config = config


def get_oauth_config() -> OAuthConfig:
    """Dependency that returns the current OAuth config."""
    if _oauth_config is None:
        raise HTTPException(status_code=500, detail="OAuth not configured")
    return _oauth_config


def _get_session_secret() -> str:
    """Return session secret from config or env."""
    if _oauth_config and _oauth_config.session_secret:
        return _oauth_config.session_secret
    return os.environ.get("SESSION_SECRET", "factory-default-session-secret")


def _sign_session_id(session_id: str) -> str:
    """Sign a session ID with HMAC to prevent tampering."""
    secret = _get_session_secret()
    sig = hmac.new(secret.encode(), session_id.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{session_id}.{sig}"


def _verify_session_id(signed: str) -> str | None:
    """Verify a signed session ID. Returns the raw ID or None."""
    if "." not in signed:
        return None
    session_id, sig = signed.rsplit(".", 1)
    expected = _sign_session_id(session_id)
    if hmac.compare_digest(signed, expected):
        return session_id
    return None


def get_current_session(factory_session: str | None = Cookie(None)) -> dict | None:
    """Extract and verify the current session from cookies."""
    if not factory_session:
        return None
    session_id = _verify_session_id(factory_session)
    if not session_id:
        return None
    session = _sessions.get(session_id)
    if not session:
        return None
    return session


def require_auth(factory_session: str | None = Cookie(None)) -> dict:
    """Dependency that requires a valid authenticated session."""
    session = get_current_session(factory_session)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session


def is_oauth_enabled() -> bool:
    """Check whether OAuth is configured and enabled."""
    if _oauth_config is None:
        return False
    client_id = _oauth_config.client_id or os.environ.get("PLANE_OAUTH_CLIENT_ID", "")
    return bool(client_id)


def _effective_client_id() -> str:
    assert _oauth_config is not None
    return _oauth_config.client_id or os.environ.get("PLANE_OAUTH_CLIENT_ID", "")


def _effective_client_secret() -> str:
    assert _oauth_config is not None
    return _oauth_config.client_secret or os.environ.get("PLANE_OAUTH_CLIENT_SECRET", "")


def _effective_redirect_uri() -> str:
    assert _oauth_config is not None
    return _oauth_config.redirect_uri or os.environ.get("PLANE_OAUTH_REDIRECT_URI", "")


# -- Routes ------------------------------------------------------------------


@router.get("/login")
async def login(config: OAuthConfig = Depends(get_oauth_config)):
    """Redirect the user to the Plane OAuth consent screen."""
    client_id = _effective_client_id()
    redirect_uri = _effective_redirect_uri()
    if not client_id or not redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="OAuth client_id and redirect_uri must be configured",
        )

    state = secrets.token_urlsafe(32)
    _sessions[f"state:{state}"] = {"state": state, "created_at": time.time()}

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": config.scopes,
        "state": state,
    }
    authorize_url = f"{config.authorize_url}?{urlencode(params)}"
    return RedirectResponse(url=authorize_url, status_code=302)


@router.get("/callback")
async def callback(
    request: Request,
    config: OAuthConfig = Depends(get_oauth_config),
):
    """Handle the OAuth callback: exchange code for tokens."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        logger.error("OAuth error from Plane: %s", error)
        return RedirectResponse(url="/?auth_error=" + error, status_code=302)

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    # Verify state parameter
    if state:
        state_key = f"state:{state}"
        _sessions.pop(state_key, None)

    # Exchange the code for tokens
    client_id = _effective_client_id()
    client_secret = _effective_client_secret()
    redirect_uri = _effective_redirect_uri()

    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }

    async with httpx.AsyncClient(timeout=30.0) as http:
        try:
            token_resp = await http.post(
                config.token_url,
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_resp.status_code != 200:
                logger.error(
                    "Token exchange failed: %d %s",
                    token_resp.status_code,
                    token_resp.text,
                )
                return RedirectResponse(
                    url="/?auth_error=token_exchange_failed", status_code=302
                )
            tokens = token_resp.json()
        except Exception as e:
            logger.exception("Token exchange request failed: %s", e)
            return RedirectResponse(
                url="/?auth_error=token_exchange_error", status_code=302
            )

        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        expires_in = tokens.get("expires_in", 86400)

        # Fetch user info
        user_info = {}
        try:
            user_resp = await http.get(
                config.userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_resp.status_code == 200:
                user_info = user_resp.json()
        except Exception as e:
            logger.warning("Failed to fetch user info: %s", e)

    # Create session
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + expires_in,
        "expires_in": expires_in,
        "user": {
            "id": user_info.get("id", ""),
            "email": user_info.get("email", ""),
            "display_name": user_info.get("display_name", "")
            or user_info.get("first_name", ""),
            "first_name": user_info.get("first_name", ""),
            "last_name": user_info.get("last_name", ""),
            "avatar": user_info.get("avatar", ""),
        },
    }

    signed_id = _sign_session_id(session_id)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key="factory_session",
        value=signed_id,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=expires_in,
        path="/",
    )
    return response


@router.get("/logout")
async def logout(factory_session: str | None = Cookie(None)):
    """Clear the session and redirect to login."""
    if factory_session:
        session_id = _verify_session_id(factory_session)
        if session_id and session_id in _sessions:
            del _sessions[session_id]

    response = RedirectResponse(url="/auth/login-page", status_code=302)
    response.delete_cookie("factory_session", path="/")
    return response


@router.get("/me")
async def me(session: dict = Depends(require_auth)):
    """Return the currently authenticated user's info."""
    return {
        "authenticated": True,
        "user": session.get("user", {}),
        "expires_at": session.get("expires_at", 0),
    }


@router.get("/status")
async def auth_status(factory_session: str | None = Cookie(None)):
    """Check authentication status without requiring auth."""
    session = get_current_session(factory_session)
    if session and session.get("access_token"):
        return {
            "authenticated": True,
            "user": session.get("user", {}),
            "oauth_enabled": True,
        }
    return {
        "authenticated": False,
        "oauth_enabled": is_oauth_enabled(),
    }


async def refresh_token_if_needed(session: dict) -> bool:
    """Attempt to refresh the access token if expired or close to expiry."""
    if not _oauth_config:
        return False

    expires_at = session.get("expires_at", 0)
    if time.time() < expires_at - 300:
        return True

    refresh_tok = session.get("refresh_token", "")
    if not refresh_tok:
        return False

    client_id = _effective_client_id()
    client_secret = _effective_client_secret()

    token_data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": client_id,
        "client_secret": client_secret,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                _oauth_config.token_url,
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code == 200:
                tokens = resp.json()
                session["access_token"] = tokens.get("access_token", session["access_token"])
                session["refresh_token"] = tokens.get("refresh_token", refresh_tok)
                session["expires_at"] = time.time() + tokens.get("expires_in", 86400)
                return True
            logger.warning("Token refresh failed: %d", resp.status_code)
    except Exception as e:
        logger.warning("Token refresh request failed: %s", e)

    return False


# -- Session cleanup ----------------------------------------------------------


def cleanup_expired_sessions() -> int:
    """Remove expired sessions. Returns count of removed sessions."""
    now = time.time()
    expired = [
        sid
        for sid, data in _sessions.items()
        if not sid.startswith("state:") and data.get("expires_at", 0) < now - 3600
    ]
    expired += [
        sid
        for sid, data in _sessions.items()
        if sid.startswith("state:") and data.get("created_at", 0) < now - 600
    ]
    for sid in expired:
        del _sessions[sid]
    return len(expired)
