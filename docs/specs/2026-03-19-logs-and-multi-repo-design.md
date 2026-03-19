# Task Logs Endpoint & Auto-Discovery Multi-Repo

**Date:** 2026-03-19
**Status:** Approved

## Overview

Two improvements to the Factory orchestrator API:

1. **Task logs endpoint + status enrichment** — expose agent output via REST so Openclaw and other clients can check progress without streaming
2. **Auto-discovery multi-repo** — let Factory work with any accessible GitHub repo without pre-registering it in config.yml

## Feature 1: Task Logs & Status Enrichment

### New endpoint: `GET /api/tasks/{id}/logs`

- **Query params:** `since` (ISO timestamp, optional), `limit` (int, default 50)
- **Response:** `{"logs": [{"message": "...", "timestamp": "..."}]}`
- **Source:** `task_logs` table in SQLite
- **Ordering:** ascending by timestamp (chronological)
- **Filtering:** if `since` is provided, only return logs after that timestamp

### Error handling

If `task_id` does not exist, return 404 (consistent with `GET /api/tasks/{id}`).

### Enriched Task response

Add `last_output: str | None` to the `Task` model:

- Populated from the most recent `task_logs` entry for that task
- Truncated to 500 characters
- Returned in every `GET /api/tasks/{id}` and `GET /api/tasks` response
- Gives Openclaw a quick "what's happening" without fetching all logs

**Population mechanism:** The API layer calls `db.get_last_output(task_id)` separately after fetching the task, and attaches the result to the Task object before returning. For `list_tasks`, the API iterates and attaches. This keeps the DB layer simple (no joins needed) at the cost of extra queries, which is acceptable given the small scale.

### Fix AgentInfo

The `/api/agents` endpoint currently returns empty strings for `task_title`, `agent_type`, and `repo`. Fix by looking up the task from DB (via `db.get_task()`) to populate these fields. The endpoint needs a `db: Database` dependency added.

## Feature 2: Auto-Discovery Multi-Repo

### Config change

Add `default_org: str` to top-level config:

```yaml
default_org: "Nomafin"

repos:
  # Optional overrides for repos that need custom settings
  factory:
    url: "https://github.com/Nomafin/factory.git"
    default_agent: "coder"
```

The `repos:` dict becomes optional. Repos listed there get their custom settings, but unlisted repos work with sensible defaults.

### Repo resolution logic

Implemented as a standalone `resolve_repo()` function in `workspace.py` (not a method on `RepoManager`). Takes `repo_name`, `config_repos` dict, and `default_org` as arguments. Returns a `(url, settings)` tuple. The orchestrator calls it before handing the resolved URL to `RepoManager.ensure_repo()`.

Applied in order:

1. `repo` matches a key in `config.repos` -> use that URL and settings
2. `repo` is `owner/repo` format -> `https://github.com/{owner}/{repo}.git`, default settings
3. `repo` is a short name and `default_org` is set -> `https://github.com/{default_org}/{repo}.git`, default settings
4. Otherwise -> fail with clear error message

### Validation

Before cloning an unknown repo (not in `config.repos`), run `git ls-remote` to verify it exists and the token has access. Return a clear error if not accessible. Cache validated repos in a set on `RepoManager` so repeated tasks against the same repo skip re-validation.

### Orchestrator changes

- Remove the hard validation in `orchestrator.py` (~line 148-156) that rejects repos not in `config.repos`
- Replace with the resolution logic above
- Unconfigured repos use defaults: `default_agent: "coder"`

### Backward compatibility

- config.yml repos work exactly as before
- The `repo` field on tasks, the worktree system, and memory scoping by repo are unchanged

## Files to modify

### Feature 1 (logs)
- `db.py` — add `get_last_output(task_id)` and update `get_logs()` to support `since` param
- `api.py` — add `GET /api/tasks/{id}/logs` endpoint, fix `/api/agents` to populate fields
- `models.py` — add `last_output` to Task, add `TaskLog` response model

### Feature 2 (multi-repo)
- `config.py` — add `default_org` field to Config
- `workspace.py` — add `resolve_repo(name)` method with the resolution logic and `git ls-remote` validation
- `orchestrator.py` — replace hard repo validation with `resolve_repo()` call
