from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from factory.api import router
from factory.deps import init_services, shutdown_services

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_services(
        config_path="/opt/factory/config.yml",
        db_path="/opt/factory/factory.db",
    )
    yield
    await shutdown_services()


app = FastAPI(title="Factory", description="Agent farm orchestrator", lifespan=lifespan)
app.include_router(router)

# Mount static files
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/messages", response_class=HTMLResponse)
async def messages_page():
    """Serve the agent message board web UI."""
    html_path = STATIC_DIR / "messages.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text())
    return HTMLResponse(content="<h1>Message board not found</h1>", status_code=404)
