from datetime import datetime, timezone

import aiosqlite

from factory.models import Task, TaskCreate, TaskStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    repo TEXT DEFAULT '',
    agent_type TEXT DEFAULT 'coder',
    status TEXT DEFAULT 'queued',
    plane_issue_id TEXT DEFAULT '',
    branch_name TEXT DEFAULT '',
    pr_url TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
"""


def _row_to_task(row: aiosqlite.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        repo=row["repo"],
        agent_type=row["agent_type"],
        status=TaskStatus(row["status"]),
        plane_issue_id=row["plane_issue_id"],
        branch_name=row["branch_name"],
        pr_url=row["pr_url"],
        error=row["error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
    )


class Database:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self):
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    async def create_task(self, task: TaskCreate) -> Task:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._db.execute(
            """INSERT INTO tasks (title, description, repo, agent_type, plane_issue_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task.title, task.description, task.repo, task.agent_type, task.plane_issue_id, now),
        )
        await self._db.commit()
        return await self.get_task(cursor.lastrowid)

    async def get_task(self, task_id: int) -> Task | None:
        cursor = await self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        return _row_to_task(row) if row else None

    async def list_tasks(self, status: TaskStatus | None = None) -> list[Task]:
        if status:
            cursor = await self._db.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC", (status.value,)
            )
        else:
            cursor = await self._db.execute("SELECT * FROM tasks ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [_row_to_task(row) for row in rows]

    async def update_task_status(self, task_id: int, status: TaskStatus, error: str = "") -> Task:
        now = datetime.now(timezone.utc).isoformat()
        updates = {"status": status.value}
        if status == TaskStatus.IN_PROGRESS:
            updates["started_at"] = now
        elif status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.IN_REVIEW):
            updates["completed_at"] = now
        if error:
            updates["error"] = error

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [task_id]
        await self._db.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        await self._db.commit()
        return await self.get_task(task_id)

    async def update_task_fields(self, task_id: int, **fields) -> Task:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [task_id]
        await self._db.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        await self._db.commit()
        return await self.get_task(task_id)

    async def add_log(self, task_id: int, message: str):
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO task_logs (task_id, message, timestamp) VALUES (?, ?, ?)",
            (task_id, message, now),
        )
        await self._db.commit()

    async def get_logs(self, task_id: int) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT message, timestamp FROM task_logs WHERE task_id = ? ORDER BY timestamp", (task_id,)
        )
        rows = await cursor.fetchall()
        return [{"message": row["message"], "timestamp": row["timestamp"]} for row in rows]
