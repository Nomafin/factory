from datetime import datetime, timezone

import aiosqlite

from factory.models import (
    Task, TaskCreate, TaskStatus,
    Workflow, WorkflowCreate, WorkflowStatus, WorkflowStep, WorkflowStepDef,
)

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
    clarification_context TEXT DEFAULT '',
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

CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    repo TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    current_step INTEGER DEFAULT 0,
    plane_issue_id TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL,
    step_index INTEGER NOT NULL,
    agent_type TEXT NOT NULL,
    task_id INTEGER,
    status TEXT DEFAULT 'pending',
    input_key TEXT DEFAULT '',
    output_key TEXT DEFAULT '',
    condition TEXT DEFAULT '',
    output_data TEXT DEFAULT '',
    started_at TEXT,
    completed_at TEXT,
    FOREIGN KEY (workflow_id) REFERENCES workflows(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
"""

MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN clarification_context TEXT DEFAULT '';",
    "ALTER TABLE tasks ADD COLUMN workflow_id INTEGER;",
    "ALTER TABLE tasks ADD COLUMN workflow_step INTEGER;",
]


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
        clarification_context=row["clarification_context"] or "",
        workflow_id=row["workflow_id"] if "workflow_id" in row.keys() else None,
        workflow_step=row["workflow_step"] if "workflow_step" in row.keys() else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
    )


def _row_to_workflow(row: aiosqlite.Row) -> Workflow:
    return Workflow(
        id=row["id"],
        name=row["name"],
        title=row["title"],
        description=row["description"],
        repo=row["repo"],
        status=WorkflowStatus(row["status"]),
        current_step=row["current_step"],
        plane_issue_id=row["plane_issue_id"],
        error=row["error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
    )


def _row_to_workflow_step(row: aiosqlite.Row) -> WorkflowStep:
    return WorkflowStep(
        id=row["id"],
        workflow_id=row["workflow_id"],
        step_index=row["step_index"],
        agent_type=row["agent_type"],
        task_id=row["task_id"],
        status=row["status"],
        input_key=row["input_key"],
        output_key=row["output_key"],
        condition=row["condition"],
        output_data=row["output_data"] or "",
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
        await self._apply_migrations()

    async def _apply_migrations(self):
        for sql in MIGRATIONS:
            try:
                await self._db.execute(sql)
                await self._db.commit()
            except Exception:
                pass  # Column already exists

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

    async def find_by_plane_issue_id(self, plane_issue_id: str) -> Task | None:
        cursor = await self._db.execute(
            "SELECT * FROM tasks WHERE plane_issue_id = ? ORDER BY created_at DESC LIMIT 1",
            (plane_issue_id,),
        )
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

    # ── Workflow operations ──────────────────────────────────────────────

    async def create_workflow(
        self, name: str, title: str, description: str = "",
        repo: str = "", plane_issue_id: str = "",
    ) -> Workflow:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._db.execute(
            """INSERT INTO workflows (name, title, description, repo, plane_issue_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, title, description, repo, plane_issue_id, now),
        )
        await self._db.commit()
        return await self.get_workflow(cursor.lastrowid)

    async def get_workflow(self, workflow_id: int) -> Workflow | None:
        cursor = await self._db.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        workflow = _row_to_workflow(row)
        workflow.steps = await self.get_workflow_steps(workflow_id)
        return workflow

    async def list_workflows(self, status: WorkflowStatus | None = None) -> list[Workflow]:
        if status:
            cursor = await self._db.execute(
                "SELECT * FROM workflows WHERE status = ? ORDER BY created_at DESC",
                (status.value,),
            )
        else:
            cursor = await self._db.execute("SELECT * FROM workflows ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        workflows = []
        for row in rows:
            wf = _row_to_workflow(row)
            wf.steps = await self.get_workflow_steps(wf.id)
            workflows.append(wf)
        return workflows

    async def update_workflow_status(
        self, workflow_id: int, status: WorkflowStatus, error: str = "",
    ) -> Workflow:
        now = datetime.now(timezone.utc).isoformat()
        updates = {"status": status.value}
        if status == WorkflowStatus.RUNNING:
            updates["started_at"] = now
        elif status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED):
            updates["completed_at"] = now
        if error:
            updates["error"] = error

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [workflow_id]
        await self._db.execute(f"UPDATE workflows SET {set_clause} WHERE id = ?", values)
        await self._db.commit()
        return await self.get_workflow(workflow_id)

    async def update_workflow_fields(self, workflow_id: int, **fields) -> Workflow:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [workflow_id]
        await self._db.execute(f"UPDATE workflows SET {set_clause} WHERE id = ?", values)
        await self._db.commit()
        return await self.get_workflow(workflow_id)

    async def create_workflow_step(
        self, workflow_id: int, step_index: int, agent_type: str,
        input_key: str = "", output_key: str = "", condition: str = "",
    ) -> WorkflowStep:
        cursor = await self._db.execute(
            """INSERT INTO workflow_steps
               (workflow_id, step_index, agent_type, input_key, output_key, condition)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (workflow_id, step_index, agent_type, input_key, output_key, condition),
        )
        await self._db.commit()
        return await self.get_workflow_step(cursor.lastrowid)

    async def get_workflow_step(self, step_id: int) -> WorkflowStep | None:
        cursor = await self._db.execute("SELECT * FROM workflow_steps WHERE id = ?", (step_id,))
        row = await cursor.fetchone()
        return _row_to_workflow_step(row) if row else None

    async def get_workflow_steps(self, workflow_id: int) -> list[WorkflowStep]:
        cursor = await self._db.execute(
            "SELECT * FROM workflow_steps WHERE workflow_id = ? ORDER BY step_index",
            (workflow_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_workflow_step(row) for row in rows]

    async def update_workflow_step_status(
        self, step_id: int, status: str, **extra_fields,
    ) -> WorkflowStep:
        now = datetime.now(timezone.utc).isoformat()
        updates: dict = {"status": status}
        if status == "running":
            updates["started_at"] = now
        elif status in ("completed", "skipped", "failed"):
            updates["completed_at"] = now
        updates.update(extra_fields)

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [step_id]
        await self._db.execute(f"UPDATE workflow_steps SET {set_clause} WHERE id = ?", values)
        await self._db.commit()
        return await self.get_workflow_step(step_id)

    async def get_step_output(self, workflow_id: int, output_key: str) -> str:
        """Retrieve stored output data from a previous step by its output key."""
        cursor = await self._db.execute(
            """SELECT output_data FROM workflow_steps
               WHERE workflow_id = ? AND output_key = ? AND status = 'completed'
               ORDER BY step_index DESC LIMIT 1""",
            (workflow_id, output_key),
        )
        row = await cursor.fetchone()
        return row["output_data"] if row else ""
