import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from factory.api import router
from factory.deps import init_services, shutdown_services

FACTORY_ROOT = Path(os.environ.get("FACTORY_ROOT", Path(__file__).resolve().parents[3]))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_services(
        config_path=str(FACTORY_ROOT / "config.yml"),
        db_path=str(FACTORY_ROOT / "factory.db"),
    )
    yield
    await shutdown_services()


app = FastAPI(title="Factory", description="Agent farm orchestrator", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}
