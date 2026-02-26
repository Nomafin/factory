from pathlib import Path

from factory.config import Config, load_config
from factory.db import Database

_db: Database | None = None
_config: Config | None = None


async def init_db(db_path: str):
    global _db
    _db = Database(db_path)
    await _db.initialize()


async def close_db():
    global _db
    if _db:
        await _db.close()


def get_db() -> Database:
    assert _db is not None, "Database not initialized"
    return _db
