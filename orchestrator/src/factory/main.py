from contextlib import asynccontextmanager

from fastapi import FastAPI

from factory.api import router
from factory.deps import close_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db("/opt/factory/factory.db")
    yield
    await close_db()


app = FastAPI(title="Factory", description="Agent farm orchestrator", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}
