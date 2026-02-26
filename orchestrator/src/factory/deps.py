from pathlib import Path

from factory.config import Config, load_config
from factory.db import Database
from factory.orchestrator import Orchestrator

_db: Database | None = None
_orchestrator: Orchestrator | None = None


async def init_services(config_path: str, db_path: str):
    global _db, _orchestrator
    config = load_config(Path(config_path))
    _db = Database(db_path)
    await _db.initialize()
    _orchestrator = Orchestrator(db=_db, config=config)


async def shutdown_services():
    global _db
    if _db:
        await _db.close()


def get_db() -> Database:
    assert _db is not None, "Database not initialized"
    return _db


def get_orchestrator() -> Orchestrator:
    assert _orchestrator is not None, "Orchestrator not initialized"
    return _orchestrator
