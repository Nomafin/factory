from contextlib import asynccontextmanager

from fastapi import FastAPI

from factory.api import router
from factory.deps import init_services, shutdown_services


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


@app.get("/health")
async def health():
    return {"status": "ok"}
