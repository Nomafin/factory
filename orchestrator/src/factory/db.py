from datetime import datetime, timezone

import aiosqlite

from factory.models import (
    AgentHandoff, HandoffCreate,
    Message, MessageCreate, MessageType,
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
    plane_sequence_id INTEGER,
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
    iteration INTEGER DEFAULT 0,
    max_iterations INTEGER DEFAULT 3,
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
    loop_to TEXT DEFAULT '',
    prompt_template TEXT DEFAULT '',
    output_data TEXT DEFAULT '',
    iteration INTEGER DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    FOREIGN KEY (workflow_id) REFERENCES workflows(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS agent_handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_task_id INTEGER NOT NULL,
    to_task_id INTEGER,
    workflow_id INTEGER,
    output_type TEXT DEFAULT 'general',
    content TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (from_task_id) REFERENCES tasks(id),
    FOREIGN KEY (to_task_id) REFERENCES tasks(id),
    FOREIGN KEY (workflow_id) REFERENCES workflows(id)
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    recipient TEXT,
    task_id INTEGER,
    workflow_id INTEGER,
    message TEXT NOT NULL,
    message_type TEXT NOT NULL DEFAULT 'info',
    reply_to INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (workflow_id) REFERENCES workflows(id),
    FOREIGN KEY (reply_to) REFERENCES agent_messages(id)
);
"""

MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN clarification_context TEXT DEFAULT '';",
    "ALTER TABLE tasks ADD COLUMN workflow_id INTEGER;",
    "ALTER TABLE tasks ADD COLUMN workflow_step INTEGER;",
    "ALTER TABLE workflows ADD COLUMN iteration INTEGER DEFAULT 0;",
    "ALTER TABLE workflows ADD COLUMN max_iterations INTEGER DEFAULT 3;",
    "ALTER TABLE workflow_steps ADD COLUMN loop_to TEXT DEFAULT '';",
    "ALTER TABLE workflow_steps ADD COLUMN prompt_template TEXT DEFAULT '';",
    "ALTER TABLE tasks ADD COLUMN preview_url TEXT DEFAULT '';",
    "ALTER TABLE tasks ADD COLUMN plane_sequence_id INTEGER;",
]


def _row_to_task(row: aiosqlite.Row) -> Task:
    keys = row.keys()
    return Task(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        repo=row["repo"],
        agent_type=row["agent_type"],
        status=TaskStatus(row["status"]),
        plane_issue_id=row["plane_issue_id"],
        plane_sequence_id=row["plane_sequence_id"] if "plane_sequence_id" in keys else None,
        branch_name=row["branch_name"],
        pr_url=row["pr_url"],
        preview_url=row["preview_url"] if "preview_url" in keys else "",
        error=row["error"],
        clarification_context=row["clarification_context"] or "",
        workflow_id=row["workflow_id"] if "workflow_id" in keys else None,
        workflow_step=row["workflow_step"] if "workflow_step" in keys else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
    )


def _row_to_workflow(row: aiosqlite.Row) -> Workflow:
    keys = row.keys()
    return Workflow(
        id=row["id"],
        name=row["name"],
        title=row["title"],
        description=row["description"],
        repo=row["repo"],
        status=WorkflowStatus(row["status"]),
        current_step=row["current_step"],
        iteration=row["iteration"] if "iteration" in keys else 0,
        max_iterations=row["max_iterations"] if "max_iterations" in keys else 3,
        plane_issue_id=row["plane_issue_id"],
        error=row["error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
    )


def _row_to_workflow_step(row: aiosqlite.Row) -> WorkflowStep:
    keys = row.keys()
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
        loop_to=row["loop_to"] if "loop_to" in keys else "",
        prompt_template=row["prompt_template"] if "prompt_template" in keys else "",
        output_data=row["output_data"] or "",
        iteration=row["iteration"] if "iteration" in keys else 0,
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
    )


def _row_to_handoff(row: aiosqlite.Row) -> AgentHandoff:
    return AgentHandoff(
        id=row["id"],
        from_task_id=row["from_task_id"],
        to_task_id=row["to_task_id"],
        workflow_id=row["workflow_id"],
        output_type=row["output_type"] or "general",
        content=row["content"] or "",
        summary=row["summary"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
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
            """INSERT INTO tasks (title, description, repo, agent_type, plane_issue_id, plane_sequence_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task.title, task.description, task.repo, task.agent_type, task.plane_issue_id, task.plane_sequence_id, now),
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

    async def find_previous_task_with_pr(self, plane_issue_id: str) -> Task | None:
        """Find a previous task for the same Plane issue that has a PR.

        Used to detect revision tasks: if a previous task for this issue
        already created a PR, the new task is a revision that should push
        to the existing branch instead of creating a new one.
        """
        if not plane_issue_id:
            return None
        cursor = await self._db.execute(
            """SELECT * FROM tasks
               WHERE plane_issue_id = ?
                 AND pr_url != ''
                 AND status IN ('in_review', 'done', 'failed')
               ORDER BY created_at DESC LIMIT 1""",
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
        max_iterations: int = 3,
    ) -> Workflow:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._db.execute(
            """INSERT INTO workflows (name, title, description, repo, plane_issue_id, max_iterations, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, title, description, repo, plane_issue_id, max_iterations, now),
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
        loop_to: str = "", prompt_template: str = "",
    ) -> WorkflowStep:
        cursor = await self._db.execute(
            """INSERT INTO workflow_steps
               (workflow_id, step_index, agent_type, input_key, output_key, condition, loop_to, prompt_template)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (workflow_id, step_index, agent_type, input_key, output_key, condition, loop_to, prompt_template),
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

    async def increment_workflow_iteration(self, workflow_id: int) -> int:
        """Increment the iteration count and return the new value."""
        await self._db.execute(
            "UPDATE workflows SET iteration_count = iteration_count + 1 WHERE id = ?",
            (workflow_id,),
        )
        await self._db.commit()
        cursor = await self._db.execute(
            "SELECT iteration_count FROM workflows WHERE id = ?", (workflow_id,),
        )
        row = await cursor.fetchone()
        return row["iteration_count"] if row else 0

    async def reset_step_for_loop(self, step_id: int, iteration: int):
        """Reset a workflow step so it can run again in a loop iteration."""
        await self._db.execute(
            """UPDATE workflow_steps
               SET status = 'pending', task_id = NULL, output_data = '',
                   iteration = ?, started_at = NULL, completed_at = NULL
               WHERE id = ?""",
            (iteration, step_id),
        )
        await self._db.commit()

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

    # ── Handoff operations ──────────────────────────────────────────────

    async def create_handoff(self, handoff: HandoffCreate) -> AgentHandoff:
        """Create a new agent handoff record."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._db.execute(
            """INSERT INTO agent_handoffs
               (from_task_id, to_task_id, workflow_id, output_type, content, summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                handoff.from_task_id, handoff.to_task_id, handoff.workflow_id,
                handoff.output_type, handoff.content, handoff.summary, now,
            ),
        )
        await self._db.commit()
        return await self.get_handoff(cursor.lastrowid)

    async def get_handoff(self, handoff_id: int) -> AgentHandoff | None:
        cursor = await self._db.execute(
            "SELECT * FROM agent_handoffs WHERE id = ?", (handoff_id,),
        )
        row = await cursor.fetchone()
        return _row_to_handoff(row) if row else None

    async def get_handoffs_for_task(self, task_id: int) -> list[AgentHandoff]:
        """Get all handoffs where *to_task_id* matches (inputs for a task)."""
        cursor = await self._db.execute(
            "SELECT * FROM agent_handoffs WHERE to_task_id = ? ORDER BY created_at",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_handoff(row) for row in rows]

    async def get_handoffs_from_task(self, task_id: int) -> list[AgentHandoff]:
        """Get all handoffs produced by a task."""
        cursor = await self._db.execute(
            "SELECT * FROM agent_handoffs WHERE from_task_id = ? ORDER BY created_at",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_handoff(row) for row in rows]

    async def get_handoffs_for_workflow(self, workflow_id: int) -> list[AgentHandoff]:
        """Get all handoffs within a workflow."""
        cursor = await self._db.execute(
            "SELECT * FROM agent_handoffs WHERE workflow_id = ? ORDER BY created_at",
            (workflow_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_handoff(row) for row in rows]

    async def link_handoff_to_task(self, handoff_id: int, to_task_id: int) -> AgentHandoff:
        """Set the to_task_id on an existing handoff (when next task is created)."""
        await self._db.execute(
            "UPDATE agent_handoffs SET to_task_id = ? WHERE id = ?",
            (to_task_id, handoff_id),
        )
        await self._db.commit()
        return await self.get_handoff(handoff_id)

    # ── Message board operations ───────────────────────────────────────────

    async def create_message(self, msg: MessageCreate) -> Message:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._db.execute(
            """INSERT INTO agent_messages
               (sender, recipient, task_id, workflow_id, message, message_type, reply_to, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg.sender,
                msg.recipient,
                msg.task_id,
                msg.workflow_id,
                msg.message,
                msg.message_type.value,
                msg.reply_to,
                now,
            ),
        )
        await self._db.commit()
        return await self.get_message(cursor.lastrowid)

    async def get_message(self, message_id: int) -> Message | None:
        cursor = await self._db.execute(
            "SELECT * FROM agent_messages WHERE id = ?", (message_id,)
        )
        row = await cursor.fetchone()
        return _row_to_message(row) if row else None

    async def list_messages(
        self,
        task_id: int | None = None,
        workflow_id: int | None = None,
        sender: str | None = None,
        message_type: str | None = None,
        since: str | None = None,
        before: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Message]:
        conditions = []
        params: list = []

        if task_id is not None:
            conditions.append("task_id = ?")
            params.append(task_id)
        if workflow_id is not None:
            conditions.append("workflow_id = ?")
            params.append(workflow_id)
        if sender:
            conditions.append("sender = ?")
            params.append(sender)
        if message_type:
            conditions.append("message_type = ?")
            params.append(message_type)
        if since:
            conditions.append("created_at >= ?")
            params.append(since)
        if before:
            conditions.append("created_at <= ?")
            params.append(before)

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"SELECT * FROM agent_messages{where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await self._db.execute(query, params)
        rows = await cursor.fetchall()
        return [_row_to_message(row) for row in rows]

    async def search_messages(self, query: str, limit: int = 50) -> list[Message]:
        cursor = await self._db.execute(
            """SELECT * FROM agent_messages
               WHERE message LIKE ?
               ORDER BY created_at DESC LIMIT ?""",
            (f"%{query}%", limit),
        )
        rows = await cursor.fetchall()
        return [_row_to_message(row) for row in rows]

    async def get_thread(self, parent_id: int) -> list[Message]:
        """Get a message and all its replies."""
        cursor = await self._db.execute(
            """SELECT * FROM agent_messages
               WHERE id = ? OR reply_to = ?
               ORDER BY created_at ASC""",
            (parent_id, parent_id),
        )
        rows = await cursor.fetchall()
        return [_row_to_message(row) for row in rows]


def _row_to_message(row: aiosqlite.Row) -> Message:
    return Message(
        id=row["id"],
        sender=row["sender"],
        recipient=row["recipient"],
        task_id=row["task_id"],
        workflow_id=row["workflow_id"],
        message=row["message"],
        message_type=MessageType(row["message_type"]),
        reply_to=row["reply_to"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
