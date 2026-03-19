# Task Logs & Auto-Discovery Multi-Repo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a task logs polling endpoint with enriched task status, and let Factory work with any GitHub repo without pre-registration.

**Architecture:** Two independent features sharing one commit history. Feature 1 adds `GET /api/tasks/{id}/logs` and a `last_output` field to Task responses. Feature 2 replaces the hard repo validation with a `resolve_repo()` function that supports short names, `owner/repo`, and a configurable default org, with `git ls-remote` validation for unknown repos.

**Tech Stack:** Python, FastAPI, aiosqlite, asyncio subprocess (git), pytest

**Test runner:** `cd /Users/miika/Work/Factory/orchestrator && .venv/bin/python -m pytest tests/ -v`

---

## File Map

### Feature 1 (Logs & Status Enrichment)

| File | Action | Responsibility |
|------|--------|----------------|
| `orchestrator/src/factory/models.py` | Modify | Add `last_output` to Task, add `TaskLog` model |
| `orchestrator/src/factory/db.py` | Modify | Add `get_last_output()`, update `get_logs()` for `since`/`limit` |
| `orchestrator/src/factory/api.py` | Modify | Add logs endpoint, enrich task responses, fix AgentInfo |
| `orchestrator/tests/test_db.py` | Modify | Tests for `get_last_output()`, `get_logs(since, limit)` |
| `orchestrator/tests/test_api.py` | Modify | Tests for logs endpoint, enriched task response, AgentInfo |

### Feature 2 (Auto-Discovery Multi-Repo)

| File | Action | Responsibility |
|------|--------|----------------|
| `orchestrator/src/factory/config.py` | Modify | Add `default_org` to Config |
| `orchestrator/src/factory/workspace.py` | Modify | Add `resolve_repo()` function, add validated repos cache |
| `orchestrator/src/factory/orchestrator.py` | Modify | Replace hard repo validation with `resolve_repo()` |
| `orchestrator/tests/test_workspace.py` | Modify | Tests for `resolve_repo()` |
| `orchestrator/tests/test_orchestrator.py` | Modify | Test that unknown repos go through resolution |

---

## Task 1: Add `last_output` to Task model and `TaskLog` response model

**Files:**
- Modify: `orchestrator/src/factory/models.py`

- [ ] **Step 1: Add `last_output` field to Task model**

In `models.py`, add `last_output` to the Task class (after `clarification_context`):

```python
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
    clarification_context: str = ""
    last_output: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
```

- [ ] **Step 2: Add `TaskLog` response model**

Add below the Task class:

```python
class TaskLog(BaseModel):
    message: str
    timestamp: str


class TaskLogsResponse(BaseModel):
    logs: list[TaskLog]
```

- [ ] **Step 3: Run existing tests to confirm nothing breaks**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All existing tests pass (the new field has a default of None so nothing breaks)

- [ ] **Step 4: Commit**

```bash
git add orchestrator/src/factory/models.py
git commit -m "feat: add last_output field to Task and TaskLog response model"
```

---

## Task 2: Add `get_last_output()` and update `get_logs()` in db.py

**Files:**
- Modify: `orchestrator/src/factory/db.py`
- Modify: `orchestrator/tests/test_db.py`

- [ ] **Step 1: Write failing tests**

Add to `orchestrator/tests/test_db.py`:

```python
async def test_get_last_output():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    await db.add_log(task.id, "First output")
    await db.add_log(task.id, "Second output")

    last = await db.get_last_output(task.id)
    assert last == "Second output"

    await db.close()


async def test_get_last_output_empty():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    last = await db.get_last_output(task.id)
    assert last is None

    await db.close()


async def test_get_last_output_truncated():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    await db.add_log(task.id, "x" * 1000)

    last = await db.get_last_output(task.id)
    assert len(last) == 500

    await db.close()


async def test_get_logs_with_since():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    await db.add_log(task.id, "First output")
    logs_before = await db.get_logs(task.id)
    since = logs_before[0]["timestamp"]

    await db.add_log(task.id, "Second output")

    logs = await db.get_logs(task.id, since=since)
    assert len(logs) == 1
    assert logs[0]["message"] == "Second output"

    await db.close()


async def test_get_logs_with_limit():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    for i in range(10):
        await db.add_log(task.id, f"Output {i}")

    logs = await db.get_logs(task.id, limit=3)
    assert len(logs) == 3
    assert logs[0]["message"] == "Output 0"

    await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_db.py -v`
Expected: FAIL — `get_last_output` does not exist, `get_logs` doesn't accept `since`/`limit`

- [ ] **Step 3: Implement `get_last_output()` and update `get_logs()`**

In `db.py`, add `get_last_output` method and update `get_logs`:

```python
async def get_last_output(self, task_id: int) -> str | None:
    cursor = await self._db.execute(
        "SELECT message FROM task_logs WHERE task_id = ? ORDER BY timestamp DESC LIMIT 1",
        (task_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return row["message"][:500]

async def get_logs(self, task_id: int, since: str | None = None, limit: int = 50) -> list[dict]:
    if since:
        cursor = await self._db.execute(
            "SELECT message, timestamp FROM task_logs WHERE task_id = ? AND timestamp > ? ORDER BY timestamp LIMIT ?",
            (task_id, since, limit),
        )
    else:
        cursor = await self._db.execute(
            "SELECT message, timestamp FROM task_logs WHERE task_id = ? ORDER BY timestamp LIMIT ?",
            (task_id, limit),
        )
    rows = await cursor.fetchall()
    return [{"message": row["message"], "timestamp": row["timestamp"]} for row in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_db.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/factory/db.py orchestrator/tests/test_db.py
git commit -m "feat: add get_last_output and logs filtering to database"
```

---

## Task 3: Add logs endpoint, enrich task responses, fix AgentInfo

**Files:**
- Modify: `orchestrator/src/factory/api.py`
- Modify: `orchestrator/tests/test_api.py`

- [ ] **Step 1: Write failing tests**

Add to `orchestrator/tests/test_api.py`:

```python
async def test_get_task_logs(client, db):
    create_resp = await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    task_id = create_resp.json()["id"]

    await db.add_log(task_id, "First output")
    await db.add_log(task_id, "Second output")

    resp = await client.get(f"/api/tasks/{task_id}/logs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["logs"]) == 2
    assert data["logs"][0]["message"] == "First output"


async def test_get_task_logs_not_found(client):
    resp = await client.get("/api/tasks/999/logs")
    assert resp.status_code == 404


async def test_get_task_logs_with_limit(client, db):
    create_resp = await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    task_id = create_resp.json()["id"]

    for i in range(10):
        await db.add_log(task_id, f"Output {i}")

    resp = await client.get(f"/api/tasks/{task_id}/logs?limit=3")
    assert resp.status_code == 200
    assert len(resp.json()["logs"]) == 3


async def test_task_has_last_output(client, db):
    create_resp = await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    task_id = create_resp.json()["id"]

    await db.add_log(task_id, "Some agent output")

    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["last_output"] == "Some agent output"


async def test_task_last_output_none_when_no_logs(client):
    create_resp = await client.post("/api/tasks", json={"title": "Task 1", "repo": "myapp"})
    task_id = create_resp.json()["id"]

    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["last_output"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api.py -v`
Expected: FAIL — endpoint doesn't exist, `last_output` not in response

- [ ] **Step 3: Add logs endpoint and enrich task responses in api.py**

Add import at top of `api.py`:

```python
from factory.models import AgentInfo, Task, TaskCreate, TaskLogsResponse, TaskStatus
```

Add the logs endpoint after the `get_task` endpoint:

```python
@router.get("/tasks/{task_id}/logs", response_model=TaskLogsResponse)
async def get_task_logs(
    task_id: int,
    since: str | None = Query(None),
    limit: int = Query(50),
    db: Database = Depends(get_db),
):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    logs = await db.get_logs(task_id, since=since, limit=limit)
    return TaskLogsResponse(logs=logs)
```

Add a helper to enrich tasks with `last_output`:

```python
async def _enrich_task(task: Task, db: Database) -> Task:
    task.last_output = await db.get_last_output(task.id)
    return task
```

Update `get_task` endpoint to enrich:

```python
@router.get("/tasks/{task_id}", response_model=Task)
async def get_task(task_id: int, db: Database = Depends(get_db)):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return await _enrich_task(task, db)
```

Update `list_tasks` endpoint to enrich:

```python
@router.get("/tasks", response_model=list[Task])
async def list_tasks(status: TaskStatus | None = None, db: Database = Depends(get_db)):
    tasks = await db.list_tasks(status=status)
    return [await _enrich_task(t, db) for t in tasks]
```

Update `create_task` endpoint — enrich the returned task:

```python
# At the end of create_task, before return:
return await _enrich_task(task, db)
```

Fix `list_agents` to populate fields from DB:

```python
@router.get("/agents", response_model=list[AgentInfo])
async def list_agents(
    orch: Orchestrator = Depends(get_orchestrator),
    db: Database = Depends(get_db),
):
    agents = orch.runner.get_running_agents()
    result = []
    for a in agents.values():
        task = await db.get_task(a.task_id)
        result.append(AgentInfo(
            task_id=a.task_id,
            task_title=task.title if task else "",
            agent_type=task.agent_type if task else "",
            repo=task.repo if task else "",
            status="running",
            started_at=a.started_at,
            pid=a.process.pid if a.process else None,
        ))
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_api.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run all tests**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add orchestrator/src/factory/api.py orchestrator/tests/test_api.py
git commit -m "feat: add task logs endpoint and enrich task responses"
```

---

## Task 4: Add `default_org` to Config

**Files:**
- Modify: `orchestrator/src/factory/config.py`

- [ ] **Step 1: Add `default_org` field**

In `config.py`, add `default_org` to the Config class (before `repos`):

```python
class Config(BaseModel):
    max_concurrent_agents: int = 3
    agent_timeout_minutes: int = 30
    default_org: str = ""
    plane: PlaneConfig = PlaneConfig()
    orchestrator: OrchestratorConfig = OrchestratorConfig()
    repos: dict[str, RepoConfig] = {}
    telegram: TelegramConfig = TelegramConfig()
    agent_templates: dict[str, AgentTemplateConfig] = {}
    surrealdb: SurrealDBConfig = SurrealDBConfig()
```

- [ ] **Step 2: Run existing tests**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: ALL PASS (new field has default)

- [ ] **Step 3: Commit**

```bash
git add orchestrator/src/factory/config.py
git commit -m "feat: add default_org config for auto-discovery repos"
```

---

## Task 5: Add `resolve_repo()` function to workspace.py

**Files:**
- Modify: `orchestrator/src/factory/workspace.py`
- Modify: `orchestrator/tests/test_workspace.py`

- [ ] **Step 1: Write failing tests**

Add to `orchestrator/tests/test_workspace.py`:

```python
import pytest
from factory.config import RepoConfig
from factory.workspace import resolve_repo


def test_resolve_repo_from_config():
    repos = {"factory": RepoConfig(url="https://github.com/Nomafin/factory.git", default_agent="coder")}
    url, settings = resolve_repo("factory", repos, "Nomafin")
    assert url == "https://github.com/Nomafin/factory.git"
    assert settings.default_agent == "coder"


def test_resolve_repo_owner_slash_name():
    url, settings = resolve_repo("other-org/some-repo", {}, "Nomafin")
    assert url == "https://github.com/other-org/some-repo.git"
    assert settings.default_agent == "coder"


def test_resolve_repo_short_name_with_default_org():
    url, settings = resolve_repo("myapp", {}, "Nomafin")
    assert url == "https://github.com/Nomafin/myapp.git"
    assert settings.default_agent == "coder"


def test_resolve_repo_short_name_no_default_org():
    with pytest.raises(ValueError, match="Cannot resolve repo"):
        resolve_repo("myapp", {}, "")


def test_resolve_repo_config_overrides_default_org():
    repos = {"myapp": RepoConfig(url="https://github.com/custom/myapp.git", default_agent="reviewer")}
    url, settings = resolve_repo("myapp", repos, "Nomafin")
    assert url == "https://github.com/custom/myapp.git"
    assert settings.default_agent == "reviewer"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workspace.py::test_resolve_repo_from_config -v`
Expected: FAIL — `resolve_repo` does not exist

- [ ] **Step 3: Implement `resolve_repo()`**

Add at the top of `workspace.py` (after imports):

```python
import re
from factory.config import RepoConfig


def resolve_repo(
    name: str,
    config_repos: dict[str, RepoConfig],
    default_org: str,
) -> tuple[str, RepoConfig]:
    """Resolve a repo name to a (url, settings) tuple.

    Resolution order:
    1. Exact match in config_repos
    2. owner/repo format -> GitHub URL
    3. Short name + default_org -> GitHub URL
    4. Raise ValueError
    """
    # 1. Config match
    if name in config_repos:
        cfg = config_repos[name]
        return cfg.url, cfg

    defaults = RepoConfig(url="", default_agent="coder")

    # 2. owner/repo format
    if "/" in name:
        parts = name.split("/", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            url = f"https://github.com/{parts[0]}/{parts[1]}.git"
            return url, defaults

    # 3. Short name with default org
    if default_org:
        url = f"https://github.com/{default_org}/{name}.git"
        return url, defaults

    raise ValueError(
        f"Cannot resolve repo '{name}': not in config, not owner/repo format, "
        f"and no default_org configured"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workspace.py -v`
Expected: ALL PASS

- [ ] **Step 5: Add validated repos cache and `validate_repo()` to RepoManager**

Add to `RepoManager.__init__`:

```python
def __init__(self, repos_dir: Path, worktrees_dir: Path):
    self.repos_dir = repos_dir
    self.worktrees_dir = worktrees_dir
    self._validated_repos: set[str] = set()
```

Add `validate_repo` method:

```python
async def validate_repo(self, name: str, url: str):
    """Verify a repo is accessible via git ls-remote. Caches results."""
    if name in self._validated_repos:
        return
    auth_url = self._auth_url(url)
    try:
        await self._run("git", "ls-remote", "--exit-code", auth_url)
        self._validated_repos.add(name)
    except RuntimeError as e:
        raise ValueError(f"Repo '{name}' at {url} is not accessible: {e}") from e
```

- [ ] **Step 6: Commit**

```bash
git add orchestrator/src/factory/workspace.py orchestrator/tests/test_workspace.py
git commit -m "feat: add resolve_repo for auto-discovery multi-repo"
```

---

## Task 6: Replace hard repo validation in orchestrator.py

**Files:**
- Modify: `orchestrator/src/factory/orchestrator.py`
- Modify: `orchestrator/tests/test_orchestrator.py`

- [ ] **Step 1: Write failing test**

Add to `orchestrator/tests/test_orchestrator.py`. First check what's there:

Read the file to understand the existing test patterns and fixtures. Then add:

```python
async def test_process_task_resolves_unknown_repo(db, config, memory):
    """Unknown repo should be resolved via default_org, not rejected."""
    config.default_org = "Nomafin"
    config.repos = {}  # No pre-configured repos

    orch = Orchestrator(db=db, config=config, memory=memory)
    orch.repo_manager = AsyncMock()
    orch.repo_manager.validate_repo = AsyncMock()
    orch.repo_manager.ensure_repo = AsyncMock(return_value=Path("/tmp/repo"))
    orch.repo_manager.create_worktree = AsyncMock(return_value=Path("/tmp/wt"))
    orch.runner = MagicMock()
    orch.runner.can_accept_task = True
    orch.runner.start_agent = AsyncMock(return_value=True)

    task = await db.create_task(TaskCreate(
        title="Fix bug", repo="myapp", agent_type="coder"
    ))

    result = await orch.process_task(task.id)

    assert result is True
    orch.repo_manager.validate_repo.assert_awaited_once()
    orch.repo_manager.ensure_repo.assert_awaited_once_with("myapp", "https://github.com/Nomafin/myapp.git")
```

NOTE: Read the existing test file first to match the fixture patterns (db, config, memory setup). Adapt the test to use whatever fixtures exist.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py::test_process_task_resolves_unknown_repo -v`
Expected: FAIL — orchestrator still rejects unknown repos

- [ ] **Step 3: Update `process_task` in orchestrator.py**

Add import at top:

```python
from factory.workspace import RepoManager, resolve_repo
```

Replace the repo validation block (lines 148-156) in `process_task`:

```python
        # Old code to remove:
        # repo_config = self.config.repos.get(task.repo)
        # if not repo_config:
        #     await self.db.update_task_status(...)
        #     ...
        #     return False

        # New code:
        try:
            repo_url, repo_settings = resolve_repo(
                task.repo, self.config.repos, self.config.default_org
            )
        except ValueError as e:
            await self.db.update_task_status(task_id, TaskStatus.FAILED, error=str(e))
            await self._update_plane_state(
                task.plane_issue_id, self.config.plane.states.failed,
                f"Failed: {e}"
            )
            await self._notify(f"\u274c Task failed: {task.title}\n{e}")
            return False

        # Validate unknown repos before cloning
        if task.repo not in self.config.repos:
            try:
                await self.repo_manager.validate_repo(task.repo, repo_url)
            except ValueError as e:
                await self.db.update_task_status(task_id, TaskStatus.FAILED, error=str(e))
                await self._update_plane_state(
                    task.plane_issue_id, self.config.plane.states.failed,
                    f"Failed: {e}"
                )
                await self._notify(f"\u274c Task failed: {task.title}\n{e}")
                return False
```

Also update the `ensure_repo` call to use the resolved URL:

```python
        # Change this line:
        await self.repo_manager.ensure_repo(task.repo, repo_config.url)
        # To:
        await self.repo_manager.ensure_repo(task.repo, repo_url)
```

And update the `template` lookup to use repo_settings for agent type fallback if no template is explicitly set. Keep existing template logic but use `repo_settings.default_agent` as fallback (if needed for future use — for now just ensure the `template` variable references work).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run all tests**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add orchestrator/src/factory/orchestrator.py orchestrator/tests/test_orchestrator.py
git commit -m "feat: auto-discovery multi-repo with resolve_repo"
```

---

## Task 7: Update documentation and deploy

**Files:**
- Modify: `README.md`
- Modify: `.env.example` (no changes needed — already has all required vars)

- [ ] **Step 1: Update README**

Find the multi-repo / configuration section in README.md and add info about:
- `default_org` config option
- Auto-discovery: short names, `owner/repo`, pre-configured repos
- New `GET /api/tasks/{id}/logs` endpoint
- `last_output` field on task responses

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add logs endpoint and auto-discovery repo docs"
```

- [ ] **Step 3: Push and verify deploy**

```bash
git push
```

Check deploy log on VPS: `ssh root@reitti.6a.fi "tail -5 /opt/factory/deploy.log"`

- [ ] **Step 4: Update config.yml on VPS**

Add `default_org: "Nomafin"` to `/opt/factory/config.yml` and restart:

```bash
ssh root@reitti.6a.fi "systemctl restart factory-orchestrator"
```

- [ ] **Step 5: Verify endpoints on VPS**

Test the logs endpoint:
```bash
ssh root@reitti.6a.fi "curl -s http://localhost:8100/api/tasks | head -c 200"
```

Verify `last_output` field appears in task responses.
