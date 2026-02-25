# Factory Agent Farm Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build an agent orchestration platform on a VPS where AI agents are commanded via Telegram (Openclaw) and tracked via Plane.so kanban board.

**Architecture:** Python FastAPI orchestrator bridges Plane.so (webhooks + SDK), Openclaw (HTTP API), and Claude Agent SDK agents running in isolated git worktrees. All deployed on root@reitti.6a.fi at /opt/factory/.

**Tech Stack:** Python 3.12, FastAPI, SQLite (aiosqlite), Claude Agent SDK, Plane.so (Docker), Nginx, systemd.

**VPS State:** Ubuntu 24.04, 8GB RAM, 4 cores, Node.js v24.13.0 (nvm), Docker 29.1.3 + Compose v5.0.0, Nginx on 80/443, Openclaw on port 18789 with token auth. No Claude Code or gh CLI installed yet.

---

## Phase 1: VPS Prerequisites

### Task 1: Install GitHub CLI on VPS

**Files:** None (system install)

**Step 1: Install gh CLI**

Run on VPS via SSH:
```bash
ssh root@reitti.6a.fi "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main' | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && apt update && apt install gh -y"
```

**Step 2: Verify installation**

```bash
ssh root@reitti.6a.fi "gh --version"
```
Expected: `gh version X.Y.Z`

**Step 3: Authenticate gh**

```bash
ssh root@reitti.6a.fi "gh auth login"
```
Note: This requires interactive auth. Use `gh auth login --with-token` if a token is available, or generate a GitHub PAT and store it.

**Step 4: Commit** — N/A (system install, nothing to commit)

---

### Task 2: Install Claude Code on VPS

**Files:** None (system install)

**Step 1: Install Claude Code via npm**

```bash
ssh root@reitti.6a.fi 'source /root/.nvm/nvm.sh && npm install -g @anthropic-ai/claude-code'
```

**Step 2: Verify installation**

```bash
ssh root@reitti.6a.fi 'source /root/.nvm/nvm.sh && claude --version'
```
Expected: Version output like `Claude Code vX.Y.Z`

**Step 3: Set up API key**

The ANTHROPIC_API_KEY should already be available since Openclaw uses Anthropic. Check if it's in the environment, otherwise add to `/opt/factory/.env`:
```bash
ssh root@reitti.6a.fi "echo 'ANTHROPIC_API_KEY=sk-ant-...' >> /opt/factory/.env"
```

**Step 4: Test Claude Code headless mode**

```bash
ssh root@reitti.6a.fi 'source /root/.nvm/nvm.sh && cd /tmp && claude -p "What is 2+2?" --output-format json'
```
Expected: JSON response with result containing "4".

**Step 5: Commit** — N/A (system install)

---

### Task 3: Create project scaffold on VPS

**Files:**
- Create: `/opt/factory/orchestrator/pyproject.toml`
- Create: `/opt/factory/orchestrator/src/factory/__init__.py`
- Create: `/opt/factory/orchestrator/src/factory/main.py`
- Create: `/opt/factory/orchestrator/tests/__init__.py`
- Create: `/opt/factory/config.yml`
- Create: `/opt/factory/.env.example`
- Create: `/opt/factory/.gitignore`

**Step 1: Create directory structure**

```bash
ssh root@reitti.6a.fi "mkdir -p /opt/factory/orchestrator/src/factory /opt/factory/orchestrator/tests /opt/factory/repos /opt/factory/worktrees /opt/factory/prompts"
```

**Step 2: Create pyproject.toml**

Create `/opt/factory/orchestrator/pyproject.toml`:
```toml
[project]
name = "factory"
version = "0.1.0"
description = "Agent farm orchestrator"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.34.0",
    "aiosqlite>=0.21.0",
    "httpx>=0.28.0",
    "pyyaml>=6.0",
    "pydantic>=2.10.0",
    "pydantic-settings>=2.7.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.25.0",
    "httpx>=0.28.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/factory"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**Step 3: Create minimal FastAPI app**

Create `/opt/factory/orchestrator/src/factory/__init__.py`:
```python
```

Create `/opt/factory/orchestrator/src/factory/main.py`:
```python
from fastapi import FastAPI

app = FastAPI(title="Factory", description="Agent farm orchestrator")


@app.get("/health")
async def health():
    return {"status": "ok"}
```

Create `/opt/factory/orchestrator/tests/__init__.py`:
```python
```

**Step 4: Create config.yml**

Create `/opt/factory/config.yml`:
```yaml
max_concurrent_agents: 3
agent_timeout_minutes: 30

plane:
  base_url: "https://plane.reitti.6a.fi"
  api_key: ""
  workspace_slug: "factory"
  project_id: ""

orchestrator:
  host: "0.0.0.0"
  port: 8100
  auth_token: ""

repos: {}

agent_templates:
  coder:
    system_prompt_file: "prompts/coder.md"
    allowed_tools:
      - "Read"
      - "Edit"
      - "Bash"
      - "Glob"
      - "Grep"
    timeout_minutes: 30
  reviewer:
    system_prompt_file: "prompts/reviewer.md"
    allowed_tools:
      - "Read"
      - "Glob"
      - "Grep"
    timeout_minutes: 15
  researcher:
    system_prompt_file: "prompts/researcher.md"
    allowed_tools:
      - "WebSearch"
      - "WebFetch"
      - "Read"
    timeout_minutes: 20
  devops:
    system_prompt_file: "prompts/devops.md"
    allowed_tools:
      - "Bash"
      - "Read"
      - "Edit"
    timeout_minutes: 15
```

**Step 5: Create .env.example and .gitignore**

Create `/opt/factory/.env.example`:
```
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...
FACTORY_AUTH_TOKEN=your-secret-token-here
PLANE_API_KEY=pl_...
```

Create `/opt/factory/.gitignore`:
```
.env
__pycache__/
*.pyc
.pytest_cache/
worktrees/
repos/
*.db
.venv/
```

**Step 6: Install dependencies and verify**

```bash
ssh root@reitti.6a.fi "cd /opt/factory/orchestrator && python3 -m venv /opt/factory/.venv && source /opt/factory/.venv/bin/activate && pip install -e '.[dev]'"
```

**Step 7: Run health check test**

Create `/opt/factory/orchestrator/tests/test_health.py`:
```python
from httpx import ASGITransport, AsyncClient

from factory.main import app


async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

```bash
ssh root@reitti.6a.fi "cd /opt/factory/orchestrator && source /opt/factory/.venv/bin/activate && pytest tests/test_health.py -v"
```
Expected: PASS

**Step 8: Commit**

```bash
cd /opt/factory && git add -A && git commit -m "feat: project scaffold with FastAPI health endpoint"
```

---

## Phase 2: Database and Config

### Task 4: Config loading

**Files:**
- Create: `orchestrator/src/factory/config.py`
- Test: `orchestrator/tests/test_config.py`

**Step 1: Write the failing test**

Create `orchestrator/tests/test_config.py`:
```python
import tempfile
from pathlib import Path

from factory.config import load_config


def test_load_config():
    cfg_text = """
max_concurrent_agents: 2
agent_timeout_minutes: 15
plane:
  base_url: "https://plane.example.com"
  api_key: "test-key"
  workspace_slug: "test"
  project_id: "proj-123"
orchestrator:
  host: "0.0.0.0"
  port: 8100
  auth_token: "secret"
repos:
  myapp:
    url: "git@github.com:user/myapp.git"
    default_agent: "coder"
agent_templates:
  coder:
    system_prompt_file: "prompts/coder.md"
    allowed_tools: ["Read", "Edit", "Bash"]
    timeout_minutes: 30
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(cfg_text)
        f.flush()
        config = load_config(Path(f.name))

    assert config.max_concurrent_agents == 2
    assert config.agent_timeout_minutes == 15
    assert config.plane.base_url == "https://plane.example.com"
    assert config.repos["myapp"].url == "git@github.com:user/myapp.git"
    assert config.agent_templates["coder"].allowed_tools == ["Read", "Edit", "Bash"]


def test_load_config_defaults():
    cfg_text = """
plane:
  base_url: "https://plane.example.com"
orchestrator:
  port: 8100
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(cfg_text)
        f.flush()
        config = load_config(Path(f.name))

    assert config.max_concurrent_agents == 3
    assert config.agent_timeout_minutes == 30
```

**Step 2: Run test to verify it fails**

```bash
cd /opt/factory/orchestrator && source /opt/factory/.venv/bin/activate && pytest tests/test_config.py -v
```
Expected: FAIL with `ImportError: cannot import name 'load_config'`

**Step 3: Write implementation**

Create `orchestrator/src/factory/config.py`:
```python
from pathlib import Path

import yaml
from pydantic import BaseModel


class PlaneConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    workspace_slug: str = "factory"
    project_id: str = ""


class OrchestratorConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8100
    auth_token: str = ""


class RepoConfig(BaseModel):
    url: str
    default_agent: str = "coder"


class AgentTemplateConfig(BaseModel):
    system_prompt_file: str = ""
    allowed_tools: list[str] = []
    timeout_minutes: int = 30


class Config(BaseModel):
    max_concurrent_agents: int = 3
    agent_timeout_minutes: int = 30
    plane: PlaneConfig = PlaneConfig()
    orchestrator: OrchestratorConfig = OrchestratorConfig()
    repos: dict[str, RepoConfig] = {}
    agent_templates: dict[str, AgentTemplateConfig] = {}


def load_config(path: Path) -> Config:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return Config(**data)
```

**Step 4: Run test to verify it passes**

```bash
pytest tests/test_config.py -v
```
Expected: PASS (2 tests)

**Step 5: Commit**

```bash
cd /opt/factory && git add -A && git commit -m "feat: config loading with Pydantic models"
```

---

### Task 5: Database schema and operations

**Files:**
- Create: `orchestrator/src/factory/db.py`
- Create: `orchestrator/src/factory/models.py`
- Test: `orchestrator/tests/test_db.py`

**Step 1: Write the failing test**

Create `orchestrator/tests/test_db.py`:
```python
from factory.db import Database
from factory.models import TaskCreate, TaskStatus


async def test_create_and_get_task():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(
        title="Fix login bug",
        description="The login timeout is too short",
        repo="myapp",
        agent_type="coder",
        plane_issue_id="issue-123",
    ))

    assert task.id is not None
    assert task.title == "Fix login bug"
    assert task.status == TaskStatus.QUEUED

    fetched = await db.get_task(task.id)
    assert fetched is not None
    assert fetched.title == "Fix login bug"

    await db.close()


async def test_list_tasks():
    db = Database(":memory:")
    await db.initialize()

    await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    await db.create_task(TaskCreate(title="Task 2", repo="myapp", agent_type="coder"))

    tasks = await db.list_tasks()
    assert len(tasks) == 2

    await db.close()


async def test_update_task_status():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    updated = await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

    assert updated.status == TaskStatus.IN_PROGRESS
    assert updated.started_at is not None

    await db.close()


async def test_list_tasks_by_status():
    db = Database(":memory:")
    await db.initialize()

    await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    t2 = await db.create_task(TaskCreate(title="Task 2", repo="myapp", agent_type="coder"))
    await db.update_task_status(t2.id, TaskStatus.IN_PROGRESS)

    queued = await db.list_tasks(status=TaskStatus.QUEUED)
    assert len(queued) == 1
    assert queued[0].title == "Task 1"

    in_progress = await db.list_tasks(status=TaskStatus.IN_PROGRESS)
    assert len(in_progress) == 1
    assert in_progress[0].title == "Task 2"

    await db.close()
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_db.py -v
```
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write models**

Create `orchestrator/src/factory/models.py`:
```python
from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class TaskStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    repo: str = ""
    agent_type: str = "coder"
    plane_issue_id: str = ""


class Task(BaseModel):
    id: int
    title: str
    description: str = ""
    repo: str = ""
    agent_type: str = "coder"
    status: TaskStatus = TaskStatus.QUEUED
    plane_issue_id: str = ""
    branch_name: str = ""
    pr_url: str = ""
    error: str = ""
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class AgentInfo(BaseModel):
    task_id: int
    task_title: str
    agent_type: str
    repo: str
    status: str
    started_at: datetime | None = None
    pid: int | None = None
```

**Step 4: Write database implementation**

Create `orchestrator/src/factory/db.py`:
```python
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
```

**Step 5: Run tests**

```bash
pytest tests/test_db.py -v
```
Expected: PASS (4 tests)

**Step 6: Commit**

```bash
cd /opt/factory && git add -A && git commit -m "feat: database schema and task CRUD operations"
```

---

## Phase 3: Task API

### Task 6: REST API endpoints

**Files:**
- Create: `orchestrator/src/factory/api.py`
- Create: `orchestrator/src/factory/deps.py`
- Modify: `orchestrator/src/factory/main.py`
- Test: `orchestrator/tests/test_api.py`

**Step 1: Write the failing test**

Create `orchestrator/tests/test_api.py`:
```python
import pytest
from httpx import ASGITransport, AsyncClient

from factory.db import Database
from factory.deps import get_db
from factory.main import app


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
async def client(db):
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_create_task(client):
    resp = await client.post("/api/tasks", json={
        "title": "Fix login bug",
        "description": "Timeout is too short",
        "repo": "myapp",
        "agent_type": "coder",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Fix login bug"
    assert data["status"] == "queued"
    assert data["id"] is not None


async def test_list_tasks(client):
    await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    await client.post("/api/tasks", json={"title": "Task 2", "repo": "myapp"})

    resp = await client.get("/api/tasks")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_get_task(client):
    create_resp = await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    task_id = create_resp.json()["id"]

    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Task 1"


async def test_get_task_not_found(client):
    resp = await client.get("/api/tasks/999")
    assert resp.status_code == 404


async def test_cancel_task(client):
    create_resp = await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    task_id = create_resp.json()["id"]

    resp = await client.post(f"/api/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


async def test_list_agents_empty(client):
    resp = await client.get("/api/agents")
    assert resp.status_code == 200
    assert resp.json() == []
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_api.py -v
```
Expected: FAIL with `ImportError`

**Step 3: Write deps module**

Create `orchestrator/src/factory/deps.py`:
```python
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
```

**Step 4: Write API routes**

Create `orchestrator/src/factory/api.py`:
```python
from fastapi import APIRouter, Depends, HTTPException

from factory.db import Database
from factory.deps import get_db
from factory.models import AgentInfo, Task, TaskCreate, TaskStatus

router = APIRouter(prefix="/api")

# In-memory agent tracking (will be managed by the agent runner later)
_running_agents: dict[int, AgentInfo] = {}


@router.post("/tasks", response_model=Task, status_code=201)
async def create_task(body: TaskCreate, db: Database = Depends(get_db)):
    return await db.create_task(body)


@router.get("/tasks", response_model=list[Task])
async def list_tasks(status: TaskStatus | None = None, db: Database = Depends(get_db)):
    return await db.list_tasks(status=status)


@router.get("/tasks/{task_id}", response_model=Task)
async def get_task(task_id: int, db: Database = Depends(get_db)):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/tasks/{task_id}/cancel", response_model=Task)
async def cancel_task(task_id: int, db: Database = Depends(get_db)):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return await db.update_task_status(task_id, TaskStatus.CANCELLED)


@router.get("/agents", response_model=list[AgentInfo])
async def list_agents():
    return list(_running_agents.values())
```

**Step 5: Update main.py to include router**

Replace `orchestrator/src/factory/main.py`:
```python
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
```

**Step 6: Run tests**

```bash
pytest tests/ -v
```
Expected: ALL PASS (test_health, test_config, test_db, test_api — ~11 tests)

**Step 7: Commit**

```bash
cd /opt/factory && git add -A && git commit -m "feat: REST API endpoints for tasks and agents"
```

---

## Phase 4: Agent Runner

### Task 7: Repo and worktree management

**Files:**
- Create: `orchestrator/src/factory/workspace.py`
- Test: `orchestrator/tests/test_workspace.py`

**Step 1: Write the failing test**

Create `orchestrator/tests/test_workspace.py`:
```python
import subprocess

from factory.workspace import RepoManager


async def test_clone_repo(tmp_path):
    # Create a bare repo to clone from
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)

    # Create initial commit in a temp working copy
    temp_work = tmp_path / "temp_work"
    subprocess.run(["git", "clone", str(origin), str(temp_work)], check=True, capture_output=True)
    (temp_work / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=str(temp_work), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@test.com", "commit", "-m", "init"],
        cwd=str(temp_work), check=True, capture_output=True,
    )
    subprocess.run(["git", "push"], cwd=str(temp_work), check=True, capture_output=True)

    repos_dir = tmp_path / "repos"
    worktrees_dir = tmp_path / "worktrees"
    repos_dir.mkdir()
    worktrees_dir.mkdir()

    mgr = RepoManager(repos_dir=repos_dir, worktrees_dir=worktrees_dir)

    # Clone
    repo_path = await mgr.ensure_repo("testrepo", str(origin))
    assert repo_path.exists()
    assert (repo_path / ".git").exists()

    # Create worktree
    wt_path = await mgr.create_worktree("testrepo", "agent/task-1-test")
    assert wt_path.exists()
    assert (wt_path / "README.md").exists()

    # Cleanup worktree
    await mgr.remove_worktree("testrepo", wt_path)
    assert not wt_path.exists()
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_workspace.py -v
```
Expected: FAIL with `ImportError`

**Step 3: Write implementation**

Create `orchestrator/src/factory/workspace.py`:
```python
import asyncio
import shutil
from pathlib import Path


class RepoManager:
    def __init__(self, repos_dir: Path, worktrees_dir: Path):
        self.repos_dir = repos_dir
        self.worktrees_dir = worktrees_dir

    async def _run(self, *args: str, cwd: str | Path | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Command {args} failed: {stderr.decode()}")
        return stdout.decode().strip()

    async def ensure_repo(self, name: str, url: str) -> Path:
        repo_path = self.repos_dir / name
        if repo_path.exists():
            await self._run("git", "fetch", "--all", cwd=repo_path)
            await self._run("git", "pull", "--ff-only", cwd=repo_path)
        else:
            await self._run("git", "clone", url, str(repo_path))
        return repo_path

    async def create_worktree(self, repo_name: str, branch_name: str) -> Path:
        repo_path = self.repos_dir / repo_name
        slug = branch_name.replace("/", "-")
        wt_path = self.worktrees_dir / slug

        await self._run("git", "worktree", "add", "-b", branch_name, str(wt_path), cwd=repo_path)
        return wt_path

    async def remove_worktree(self, repo_name: str, wt_path: Path):
        repo_path = self.repos_dir / repo_name
        await self._run("git", "worktree", "remove", str(wt_path), "--force", cwd=repo_path)
        if wt_path.exists():
            shutil.rmtree(wt_path)
```

**Step 4: Run tests**

```bash
pytest tests/test_workspace.py -v
```
Expected: PASS

**Step 5: Commit**

```bash
cd /opt/factory && git add -A && git commit -m "feat: repo clone and worktree management"
```

---

### Task 8: Agent runner with Claude Code CLI

**Files:**
- Create: `orchestrator/src/factory/runner.py`
- Test: `orchestrator/tests/test_runner.py`

This is the core component that spawns Claude Code processes. We test with mocks since we cannot run real Claude in tests.

**Step 1: Write the failing test**

Create `orchestrator/tests/test_runner.py`:
```python
from unittest.mock import MagicMock
from pathlib import Path

from factory.runner import AgentRunner


async def test_agent_runner_lifecycle():
    runner = AgentRunner(max_concurrent=2)
    assert runner.available_slots == 2
    assert runner.running_count == 0


async def test_agent_runner_concurrency_limit():
    runner = AgentRunner(max_concurrent=1)
    runner._running[1] = MagicMock()
    assert runner.available_slots == 0
    assert runner.can_accept_task is False


async def test_build_claude_command():
    runner = AgentRunner(max_concurrent=2)
    cmd = runner._build_command(
        prompt="Fix the login bug",
        workdir=Path("/tmp/worktree"),
        allowed_tools=["Read", "Edit", "Bash"],
    )
    assert "-p" in cmd
    assert "Fix the login bug" in cmd
    assert "--output-format" in cmd
    assert "stream-json" in cmd
    assert "--allowedTools" in cmd
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_runner.py -v
```
Expected: FAIL with `ImportError`

**Step 3: Write implementation**

Create `orchestrator/src/factory/runner.py`:
```python
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class RunningAgent:
    task_id: int
    process: asyncio.subprocess.Process
    workdir: Path
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AgentRunner:
    def __init__(self, max_concurrent: int = 3, claude_path: str = "claude"):
        self.max_concurrent = max_concurrent
        self.claude_path = claude_path
        self._running: dict[int, RunningAgent] = {}

    @property
    def available_slots(self) -> int:
        return self.max_concurrent - len(self._running)

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def can_accept_task(self) -> bool:
        return self.available_slots > 0

    def get_running_agents(self) -> dict[int, RunningAgent]:
        return dict(self._running)

    def _build_command(
        self,
        prompt: str,
        workdir: Path,
        allowed_tools: list[str],
    ) -> list[str]:
        cmd = [
            self.claude_path,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
        ]
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
        return cmd

    async def start_agent(
        self,
        task_id: int,
        prompt: str,
        workdir: Path,
        allowed_tools: list[str],
        on_output: Callable[[int, str], None] | None = None,
        on_complete: Callable[[int, int, str], None] | None = None,
    ) -> bool:
        if not self.can_accept_task:
            return False

        cmd = self._build_command(prompt, workdir, allowed_tools)
        logger.info("Starting agent for task %d", task_id)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        agent = RunningAgent(task_id=task_id, process=process, workdir=workdir)
        self._running[task_id] = agent

        asyncio.create_task(self._monitor_agent(agent, on_output, on_complete))
        return True

    async def _monitor_agent(
        self,
        agent: RunningAgent,
        on_output: Callable[[int, str], None] | None,
        on_complete: Callable[[int, int, str], None] | None,
    ):
        output_lines = []
        try:
            async for line in agent.process.stdout:
                decoded = line.decode().strip()
                if not decoded:
                    continue
                output_lines.append(decoded)
                try:
                    msg = json.loads(decoded)
                    if msg.get("type") == "assistant" and on_output:
                        content = msg.get("message", {}).get("content", "")
                        if isinstance(content, list):
                            text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                            content = "\n".join(text_parts)
                        if content:
                            on_output(agent.task_id, content)
                except json.JSONDecodeError:
                    pass

            await agent.process.wait()
            returncode = agent.process.returncode

            result_text = ""
            for line in reversed(output_lines):
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "result":
                        result_text = msg.get("result", "")
                        break
                except json.JSONDecodeError:
                    pass

            logger.info("Agent for task %d exited with code %d", agent.task_id, returncode)
            if on_complete:
                on_complete(agent.task_id, returncode, result_text or "\n".join(output_lines[-20:]))

        except Exception as e:
            logger.exception("Error monitoring agent for task %d", agent.task_id)
            if on_complete:
                on_complete(agent.task_id, -1, str(e))
        finally:
            self._running.pop(agent.task_id, None)

    async def cancel_agent(self, task_id: int) -> bool:
        agent = self._running.get(task_id)
        if not agent:
            return False
        agent.process.terminate()
        try:
            await asyncio.wait_for(agent.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            agent.process.kill()
        self._running.pop(task_id, None)
        return True
```

**Step 4: Run tests**

```bash
pytest tests/test_runner.py -v
```
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
cd /opt/factory && git add -A && git commit -m "feat: agent runner with Claude Code CLI integration"
```

---

## Phase 5: Orchestration Logic

### Task 9: Orchestrator service that ties everything together

**Files:**
- Create: `orchestrator/src/factory/orchestrator.py`
- Test: `orchestrator/tests/test_orchestrator.py`

**Step 1: Write the failing test**

Create `orchestrator/tests/test_orchestrator.py`:
```python
from pathlib import Path
from unittest.mock import AsyncMock, patch

from factory.config import Config, RepoConfig, AgentTemplateConfig
from factory.db import Database
from factory.models import TaskCreate, TaskStatus
from factory.orchestrator import Orchestrator


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_orchestrator_process_task(MockRunner, MockRepoMgr):
    db = Database(":memory:")
    await db.initialize()

    config = Config(
        repos={"myapp": RepoConfig(url="git@github.com:user/myapp.git")},
        agent_templates={"coder": AgentTemplateConfig(
            system_prompt_file="prompts/coder.md",
            allowed_tools=["Read", "Edit", "Bash"],
        )},
    )

    mock_repo_mgr = MockRepoMgr.return_value
    mock_repo_mgr.ensure_repo = AsyncMock(return_value=Path("/tmp/repos/myapp"))
    mock_repo_mgr.create_worktree = AsyncMock(return_value=Path("/tmp/worktrees/test"))

    mock_runner = MockRunner.return_value
    mock_runner.can_accept_task = True
    mock_runner.start_agent = AsyncMock(return_value=True)

    orch = Orchestrator(db=db, config=config)
    orch.repo_manager = mock_repo_mgr
    orch.runner = mock_runner

    task = await db.create_task(TaskCreate(
        title="Fix bug",
        repo="myapp",
        agent_type="coder",
    ))

    await orch.process_task(task.id)

    mock_repo_mgr.ensure_repo.assert_called_once()
    mock_repo_mgr.create_worktree.assert_called_once()
    mock_runner.start_agent.assert_called_once()

    updated = await db.get_task(task.id)
    assert updated.status == TaskStatus.IN_PROGRESS

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_orchestrator_rejects_when_full(MockRunner, MockRepoMgr):
    db = Database(":memory:")
    await db.initialize()

    config = Config()

    mock_runner = MockRunner.return_value
    mock_runner.can_accept_task = False

    orch = Orchestrator(db=db, config=config)
    orch.runner = mock_runner

    task = await db.create_task(TaskCreate(title="Task", repo="myapp", agent_type="coder"))

    result = await orch.process_task(task.id)
    assert result is False

    await db.close()
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_orchestrator.py -v
```
Expected: FAIL with `ImportError`

**Step 3: Write implementation**

Create `orchestrator/src/factory/orchestrator.py`:
```python
import asyncio
import logging
from pathlib import Path

from factory.config import Config
from factory.db import Database
from factory.models import TaskStatus
from factory.runner import AgentRunner
from factory.workspace import RepoManager

logger = logging.getLogger(__name__)

FACTORY_ROOT = Path("/opt/factory")


class Orchestrator:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.repo_manager = RepoManager(
            repos_dir=FACTORY_ROOT / "repos",
            worktrees_dir=FACTORY_ROOT / "worktrees",
        )
        self.runner = AgentRunner(max_concurrent=config.max_concurrent_agents)

    async def process_task(self, task_id: int) -> bool:
        if not self.runner.can_accept_task:
            logger.warning("Cannot accept task %d: no available slots", task_id)
            return False

        task = await self.db.get_task(task_id)
        if not task:
            logger.error("Task %d not found", task_id)
            return False

        repo_config = self.config.repos.get(task.repo)
        if not repo_config:
            await self.db.update_task_status(task_id, TaskStatus.FAILED, error=f"Unknown repo: {task.repo}")
            return False

        template = self.config.agent_templates.get(task.agent_type)
        if not template:
            await self.db.update_task_status(task_id, TaskStatus.FAILED, error=f"Unknown agent type: {task.agent_type}")
            return False

        try:
            await self.repo_manager.ensure_repo(task.repo, repo_config.url)
            branch_name = f"agent/task-{task.id}-{_slugify(task.title)}"
            wt_path = await self.repo_manager.create_worktree(task.repo, branch_name)
            await self.db.update_task_fields(task_id, branch_name=branch_name)
        except Exception as e:
            logger.exception("Failed to set up workspace for task %d", task_id)
            await self.db.update_task_status(task_id, TaskStatus.FAILED, error=str(e))
            return False

        prompt = self._build_prompt(task.title, task.description)

        await self.db.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        started = await self.runner.start_agent(
            task_id=task_id,
            prompt=prompt,
            workdir=wt_path,
            allowed_tools=template.allowed_tools,
            on_output=self._on_agent_output,
            on_complete=self._on_agent_complete,
        )

        if not started:
            await self.db.update_task_status(task_id, TaskStatus.FAILED, error="Failed to start agent")
            return False

        return True

    def _build_prompt(self, title: str, description: str) -> str:
        parts = [f"Task: {title}"]
        if description:
            parts.append(f"\n{description}")
        parts.append("\nWhen done, commit your changes with a descriptive message.")
        return "\n".join(parts)

    def _on_agent_output(self, task_id: int, content: str):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.db.add_log(task_id, content[:1000]))
        except RuntimeError:
            pass

    def _on_agent_complete(self, task_id: int, returncode: int, output: str):
        try:
            loop = asyncio.get_running_loop()
            if returncode == 0:
                loop.create_task(self._handle_success(task_id, output))
            else:
                loop.create_task(
                    self.db.update_task_status(task_id, TaskStatus.FAILED, error=output[:2000])
                )
        except RuntimeError:
            pass

    async def _handle_success(self, task_id: int, output: str):
        await self.db.add_log(task_id, f"Agent completed successfully:\n{output[:2000]}")
        await self.db.update_task_status(task_id, TaskStatus.IN_REVIEW)

    async def cancel_task(self, task_id: int) -> bool:
        cancelled = await self.runner.cancel_agent(task_id)
        if cancelled:
            await self.db.update_task_status(task_id, TaskStatus.CANCELLED)
        return cancelled


def _slugify(text: str) -> str:
    return "-".join(text.lower().split()[:5]).replace("/", "-")[:40]
```

**Step 4: Run tests**

```bash
pytest tests/test_orchestrator.py -v
```
Expected: PASS (2 tests)

**Step 5: Commit**

```bash
cd /opt/factory && git add -A && git commit -m "feat: orchestrator service tying together runner, workspace, and db"
```

---

### Task 10: Wire orchestrator into the API

**Files:**
- Modify: `orchestrator/src/factory/main.py`
- Modify: `orchestrator/src/factory/api.py`
- Modify: `orchestrator/src/factory/deps.py`

**Step 1: Update deps.py to include orchestrator**

Replace `orchestrator/src/factory/deps.py`:
```python
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
```

**Step 2: Update api.py with orchestrator endpoints**

Replace `orchestrator/src/factory/api.py`:
```python
from fastapi import APIRouter, Depends, HTTPException, Request

from factory.db import Database
from factory.deps import get_db, get_orchestrator
from factory.models import AgentInfo, Task, TaskCreate, TaskStatus
from factory.orchestrator import Orchestrator
from factory.plane import parse_webhook_event

router = APIRouter(prefix="/api")


@router.post("/tasks", response_model=Task, status_code=201)
async def create_task(body: TaskCreate, db: Database = Depends(get_db)):
    return await db.create_task(body)


@router.get("/tasks", response_model=list[Task])
async def list_tasks(status: TaskStatus | None = None, db: Database = Depends(get_db)):
    return await db.list_tasks(status=status)


@router.get("/tasks/{task_id}", response_model=Task)
async def get_task(task_id: int, db: Database = Depends(get_db)):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/tasks/{task_id}/run", response_model=Task)
async def run_task(task_id: int, db: Database = Depends(get_db), orch: Orchestrator = Depends(get_orchestrator)):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != TaskStatus.QUEUED:
        raise HTTPException(status_code=400, detail=f"Task is {task.status}, must be queued")
    success = await orch.process_task(task_id)
    if not success:
        raise HTTPException(status_code=503, detail="No agent slots available or task setup failed")
    return await db.get_task(task_id)


@router.post("/tasks/{task_id}/cancel", response_model=Task)
async def cancel_task(task_id: int, db: Database = Depends(get_db), orch: Orchestrator = Depends(get_orchestrator)):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await orch.cancel_task(task_id)
    return await db.get_task(task_id)


@router.get("/agents", response_model=list[AgentInfo])
async def list_agents(orch: Orchestrator = Depends(get_orchestrator)):
    agents = orch.runner.get_running_agents()
    return [
        AgentInfo(
            task_id=a.task_id,
            task_title="",
            agent_type="",
            repo="",
            status="running",
            started_at=a.started_at,
            pid=a.process.pid if a.process else None,
        )
        for a in agents.values()
    ]


@router.post("/webhooks/plane")
async def plane_webhook(request: Request, db: Database = Depends(get_db), orch: Orchestrator = Depends(get_orchestrator)):
    payload = await request.json()
    event = parse_webhook_event(payload)

    if event.event_type != "issue":
        return {"status": "ignored"}

    if event.state_name == "Queued" and event.action.value in ("create", "update"):
        task = await db.create_task(TaskCreate(
            title=event.issue_title,
            description=event.description,
            repo=event.repo,
            agent_type=event.agent_type,
            plane_issue_id=event.issue_id,
        ))
        await orch.process_task(task.id)
        return {"status": "task_created", "task_id": task.id}

    if event.state_name == "Cancelled":
        tasks = await db.list_tasks(status=TaskStatus.IN_PROGRESS)
        for task in tasks:
            if task.plane_issue_id == event.issue_id:
                await orch.cancel_task(task.id)
                return {"status": "cancelled", "task_id": task.id}

    return {"status": "ok"}
```

**Step 3: Update main.py lifespan**

Replace `orchestrator/src/factory/main.py`:
```python
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
```

**Step 4: Run all tests**

```bash
pytest tests/ -v
```
Expected: ALL PASS

**Step 5: Commit**

```bash
cd /opt/factory && git add -A && git commit -m "feat: wire orchestrator into API with task run and Plane webhook endpoints"
```

---

## Phase 6: Plane Integration

### Task 11: Plane webhook parser and API client

**Files:**
- Create: `orchestrator/src/factory/plane.py`
- Test: `orchestrator/tests/test_plane.py`

**Step 1: Write the failing test**

Create `orchestrator/tests/test_plane.py`:
```python
from factory.plane import parse_webhook_event, PlaneAction


def test_parse_issue_create_webhook():
    payload = {
        "event": "issue",
        "action": "create",
        "data": {
            "id": "uuid-123",
            "name": "Fix login bug",
            "description_html": "<p>The timeout is too short</p>",
            "state": {"name": "Queued", "group": "unstarted"},
            "labels": [{"name": "coder"}, {"name": "repo:myapp"}],
        },
    }
    event = parse_webhook_event(payload)
    assert event.event_type == "issue"
    assert event.action == PlaneAction.CREATE
    assert event.issue_title == "Fix login bug"
    assert event.repo == "myapp"
    assert event.agent_type == "coder"
    assert event.state_name == "Queued"


def test_parse_issue_update_to_queued():
    payload = {
        "event": "issue",
        "action": "update",
        "data": {
            "id": "uuid-123",
            "name": "Fix login bug",
            "description_html": "",
            "state": {"name": "Queued", "group": "unstarted"},
            "labels": [],
        },
    }
    event = parse_webhook_event(payload)
    assert event.action == PlaneAction.UPDATE
    assert event.state_name == "Queued"


def test_parse_labels_for_repo_and_agent():
    payload = {
        "event": "issue",
        "action": "create",
        "data": {
            "id": "uuid-456",
            "name": "Review PR",
            "description_html": "",
            "state": {"name": "Queued", "group": "unstarted"},
            "labels": [{"name": "reviewer"}, {"name": "repo:frontend"}],
        },
    }
    event = parse_webhook_event(payload)
    assert event.repo == "frontend"
    assert event.agent_type == "reviewer"
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_plane.py -v
```
Expected: FAIL with `ImportError`

**Step 3: Write implementation**

Create `orchestrator/src/factory/plane.py`:
```python
import hashlib
import hmac
import logging
import re
from enum import Enum

import httpx

logger = logging.getLogger(__name__)

AGENT_TYPES = {"coder", "reviewer", "researcher", "devops"}


class PlaneAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class PlaneEvent:
    def __init__(
        self,
        event_type: str,
        action: PlaneAction,
        issue_id: str = "",
        issue_title: str = "",
        description: str = "",
        state_name: str = "",
        state_group: str = "",
        repo: str = "",
        agent_type: str = "coder",
    ):
        self.event_type = event_type
        self.action = action
        self.issue_id = issue_id
        self.issue_title = issue_title
        self.description = description
        self.state_name = state_name
        self.state_group = state_group
        self.repo = repo
        self.agent_type = agent_type


def parse_webhook_event(payload: dict) -> PlaneEvent:
    data = payload.get("data", {})
    labels = data.get("labels", [])

    repo = ""
    agent_type = "coder"
    for label in labels:
        name = label.get("name", "")
        if name.startswith("repo:"):
            repo = name[5:]
        elif name in AGENT_TYPES:
            agent_type = name

    desc_html = data.get("description_html", "") or ""
    description = re.sub(r"<[^>]+>", "", desc_html).strip()

    state = data.get("state", {})

    return PlaneEvent(
        event_type=payload.get("event", ""),
        action=PlaneAction(payload.get("action", "create")),
        issue_id=data.get("id", ""),
        issue_title=data.get("name", ""),
        description=description,
        state_name=state.get("name", ""),
        state_group=state.get("group", ""),
        repo=repo,
        agent_type=agent_type,
    )


def verify_signature(payload_bytes: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class PlaneClient:
    def __init__(self, base_url: str, api_key: str, workspace_slug: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.workspace_slug = workspace_slug
        self._client = httpx.AsyncClient(
            headers={"X-API-Key": api_key},
            timeout=30.0,
        )

    async def update_issue_state(self, project_id: str, issue_id: str, state_id: str):
        url = f"{self.base_url}/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/work-items/{issue_id}/"
        resp = await self._client.patch(url, json={"state": state_id})
        resp.raise_for_status()

    async def add_comment(self, project_id: str, issue_id: str, comment_html: str):
        url = f"{self.base_url}/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/work-items/{issue_id}/comments/"
        resp = await self._client.post(url, json={"comment_html": comment_html})
        resp.raise_for_status()

    async def close(self):
        await self._client.aclose()
```

**Step 4: Run tests**

```bash
pytest tests/test_plane.py -v
```
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
cd /opt/factory && git add -A && git commit -m "feat: Plane webhook parser and API client"
```

---

## Phase 7: Deployment

### Task 12: Deploy Plane.so on VPS

**Files:** None (Docker install on VPS)

**Step 1: Install Plane via Docker**

```bash
ssh root@reitti.6a.fi "curl -fsSL https://prime.plane.so/install/ | sh -"
```

Follow the prompts:
- Domain: `plane.reitti.6a.fi` (or whatever subdomain)
- Mode: Express

Note: Plane's installer will set up its own Docker Compose stack. Check what port it binds to (default 80). Since nginx is already on 80/443, configure Plane to use a different port (e.g., 3000) and proxy via nginx.

**Step 2: Configure nginx reverse proxy**

Add nginx config for Plane. Create `/etc/nginx/sites-available/plane` on VPS and symlink to sites-enabled. Obtain SSL cert with certbot if needed.

**Step 3: Set up Plane workspace**

- Open Plane in browser
- Create workspace "Factory"
- Create project (e.g., "Agent Tasks")
- Configure custom states: Backlog, Queued, In Progress, In Review, Done, Failed, Cancelled
- Create labels: `coder`, `reviewer`, `researcher`, `devops`, plus `repo:X` per repo
- Generate API key from Profile settings
- Configure webhook pointing to `http://localhost:8100/api/webhooks/plane`

**Step 4: Update config.yml with Plane details**

Fill in `plane.api_key`, `plane.project_id`, `plane.workspace_slug` in `/opt/factory/config.yml`.

**Step 5: Verify** — Open Plane in browser and confirm workspace/project/states/webhook exist.

---

### Task 13: Deploy orchestrator on VPS

**Files:**
- Create: `/etc/systemd/system/factory-orchestrator.service`

**Step 1: Create systemd service**

Create `/etc/systemd/system/factory-orchestrator.service` on VPS:
```ini
[Unit]
Description=Factory Agent Orchestrator
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/factory/orchestrator
Environment="PATH=/root/.nvm/versions/node/v24.13.0/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EnvironmentFile=/opt/factory/.env
ExecStart=/opt/factory/.venv/bin/uvicorn factory.main:app --host 0.0.0.0 --port 8100
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Step 2: Enable and start**

```bash
ssh root@reitti.6a.fi "systemctl daemon-reload && systemctl enable factory-orchestrator && systemctl start factory-orchestrator"
```

**Step 3: Verify**

```bash
ssh root@reitti.6a.fi "systemctl status factory-orchestrator && curl -s http://localhost:8100/health"
```
Expected: `{"status":"ok"}`

---

### Task 14: Configure Openclaw integration

**Files:**
- Modify: `/root/.openclaw/workspace/TOOLS.md`

**Step 1: Add Factory tools to Openclaw workspace**

Append to `/root/.openclaw/workspace/TOOLS.md` on VPS:

```markdown
### Factory Agent Farm

The Factory orchestrator runs at http://localhost:8100. Use these endpoints to manage agents:

- Create task: POST http://localhost:8100/api/tasks with JSON body {"title": "...", "description": "...", "repo": "myapp", "agent_type": "coder"}
- List tasks: GET http://localhost:8100/api/tasks
- Get task: GET http://localhost:8100/api/tasks/{id}
- Run task: POST http://localhost:8100/api/tasks/{id}/run
- Cancel task: POST http://localhost:8100/api/tasks/{id}/cancel
- List agents: GET http://localhost:8100/api/agents

When the user asks to fix/build/review something in a repo, create a task and run it.
```

**Step 2: Test via Telegram**

Message Openclaw: "What agents are running?" — it should call GET /api/agents.

---

### Task 15: Create agent prompt templates

**Files:**
- Create: `/opt/factory/prompts/coder.md`
- Create: `/opt/factory/prompts/reviewer.md`
- Create: `/opt/factory/prompts/researcher.md`
- Create: `/opt/factory/prompts/devops.md`

**Step 1: Create prompt files**

Create `/opt/factory/prompts/coder.md`:
```markdown
You are a software engineer working on a codebase. Your job is to implement features, fix bugs, and improve code quality.

Rules:
- Read existing code before making changes
- Follow the project's existing patterns and conventions
- Write tests for new functionality
- Commit your changes with descriptive messages
- If something is unclear, document your assumptions
```

Create `/opt/factory/prompts/reviewer.md`:
```markdown
You are a code reviewer. Review code for quality, correctness, and best practices.

Rules:
- Read the code thoroughly before commenting
- Focus on bugs, security issues, and maintainability
- Be constructive and specific in feedback
```

Create `/opt/factory/prompts/researcher.md`:
```markdown
You are a research assistant. Gather information, analyze findings, and produce summaries.

Rules:
- Search multiple sources for comprehensive coverage
- Verify claims across sources when possible
- Organize findings clearly with sections and bullet points
```

Create `/opt/factory/prompts/devops.md`:
```markdown
You are a systems administrator. Configure servers, deploy services, and maintain infrastructure.

Rules:
- Always check current state before making changes
- Back up configuration before modifying it
- Test changes in a safe way before applying broadly
- Document what you changed and why
```

**Step 2: Commit**

```bash
cd /opt/factory && git add -A && git commit -m "feat: agent prompt templates"
```

---

## Phase 8: End-to-End Verification

### Task 16: Full integration test

**Step 1: Verify all services are running**

```bash
ssh root@reitti.6a.fi "systemctl status factory-orchestrator && curl -s http://localhost:8100/health && docker ps | grep plane"
```

**Step 2: Create a test task via API**

```bash
ssh root@reitti.6a.fi 'curl -s -X POST http://localhost:8100/api/tasks -H "Content-Type: application/json" -d "{\"title\": \"Add README\", \"description\": \"Create a basic README.md\", \"repo\": \"myapp\", \"agent_type\": \"coder\"}"'
```

**Step 3: Trigger the task**

```bash
ssh root@reitti.6a.fi 'curl -s -X POST http://localhost:8100/api/tasks/1/run'
```

**Step 4: Monitor**

```bash
ssh root@reitti.6a.fi 'curl -s http://localhost:8100/api/agents && curl -s http://localhost:8100/api/tasks/1'
```

**Step 5: Verify via Telegram**

Message Openclaw: "What agents are running?"

**Step 6: Verify in Plane**

Check the Plane kanban board — the task should appear and move through states.

---

## Summary

| Phase | Tasks | What is built |
|-------|-------|---------------|
| 1 | 1-3 | VPS prerequisites + project scaffold |
| 2 | 4-5 | Config loading + database |
| 3 | 6 | REST API endpoints |
| 4 | 7-8 | Workspace management + agent runner |
| 5 | 9-10 | Orchestrator service + API wiring |
| 6 | 11 | Plane webhook integration |
| 7 | 12-14 | Deploy Plane, orchestrator, Openclaw config |
| 8 | 15-16 | Prompt templates + E2E verification |
