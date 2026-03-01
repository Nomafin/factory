from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from factory.api import router
from factory.auth import (
    get_current_session,
    init_oauth,
    is_oauth_enabled,
    router as auth_router,
)
from factory.deps import get_config, init_services, shutdown_services

STATIC_DIR = Path(__file__).parent / "static"
DASHBOARD_DIR = STATIC_DIR / "dashboard"
LOGIN_HTML = STATIC_DIR / "dashboard" / "login.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_services(
        config_path="/opt/factory/config.yml",
        db_path="/opt/factory/factory.db",
    )
    # Initialize OAuth with loaded config
    config = get_config()
    init_oauth(config.oauth)
    yield
    await shutdown_services()


app = FastAPI(title="Factory", description="Agent farm orchestrator", lifespan=lifespan)
app.include_router(router)
app.include_router(auth_router)

# Mount static files
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}


def _serve_dashboard():
    """Read and return the dashboard index.html."""
    html_path = DASHBOARD_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text())
    return HTMLResponse(content="<h1>Dashboard not found</h1>", status_code=404)


def _serve_login_page():
    """Read and return the login page."""
    if LOGIN_HTML.exists():
        return HTMLResponse(content=LOGIN_HTML.read_text())
    # Fallback inline login page
    return HTMLResponse(content="""<!DOCTYPE html>
<html><head><title>Login - Factory</title></head>
<body style="background:#0f1117;color:#e0e0e6;display:flex;align-items:center;justify-content:center;height:100vh;font-family:system-ui">
<div style="text-align:center"><h1>Factory</h1><a href="/auth/login" style="background:#6c8cff;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600">Login with Plane</a></div>
</body></html>""")


def _check_auth(factory_session: str | None) -> bool:
    """Check if the user is authenticated when OAuth is enabled."""
    if not is_oauth_enabled():
        return True  # No OAuth configured, allow access
    session = get_current_session(factory_session)
    return session is not None and bool(session.get("access_token"))


@app.get("/auth/login-page", response_class=HTMLResponse)
async def login_page():
    """Serve the login page."""
    return _serve_login_page()


@app.get("/", response_class=HTMLResponse)
async def dashboard_root(factory_session: str | None = Cookie(None)):
    """Serve the dashboard at the root URL."""
    if not _check_auth(factory_session):
        return RedirectResponse(url="/auth/login-page", status_code=302)
    return _serve_dashboard()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(factory_session: str | None = Cookie(None)):
    """Serve the dashboard (alias)."""
    if not _check_auth(factory_session):
        return RedirectResponse(url="/auth/login-page", status_code=302)
    return _serve_dashboard()


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(factory_session: str | None = Cookie(None)):
    """Serve the dashboard for the tasks route."""
    if not _check_auth(factory_session):
        return RedirectResponse(url="/auth/login-page", status_code=302)
    return _serve_dashboard()


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(factory_session: str | None = Cookie(None)):
    """Serve the dashboard for the agents route."""
    if not _check_auth(factory_session):
        return RedirectResponse(url="/auth/login-page", status_code=302)
    return _serve_dashboard()


@app.get("/preview", response_class=HTMLResponse)
async def preview_page(factory_session: str | None = Cookie(None)):
    """Serve the dashboard for the preview route."""
    if not _check_auth(factory_session):
        return RedirectResponse(url="/auth/login-page", status_code=302)
    return _serve_dashboard()


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(factory_session: str | None = Cookie(None)):
    """Serve the dashboard for the analytics route."""
    if not _check_auth(factory_session):
        return RedirectResponse(url="/auth/login-page", status_code=302)
    return _serve_dashboard()


@app.get("/messages", response_class=HTMLResponse)
async def messages_page(factory_session: str | None = Cookie(None)):
    """Serve the agent message board web UI."""
    if not _check_auth(factory_session):
        return RedirectResponse(url="/auth/login-page", status_code=302)
    html_path = STATIC_DIR / "messages.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text())
    return HTMLResponse(content="<h1>Message board not found</h1>", status_code=404)
