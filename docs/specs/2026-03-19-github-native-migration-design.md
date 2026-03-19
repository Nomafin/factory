# GitHub-Native Migration Design

**Date:** 2026-03-19
**Status:** Approved
**Branch:** `github-native` (experimental, `main` preserved as rollback)

## Overview

Migrate Factory from Plane.so + VPS-based Claude Code CLI agents to GitHub-native equivalents: GitHub Projects v2 for kanban tracking, Claude Code GitHub Action for agent execution, and a slimmed-down Factory service for memory, preview environments, and Telegram notifications.

## Architecture

```
Openclaw / Human
       │
       ▼
GitHub Issues (auto-added to Project board as "Queued")
       │
       ▼ (projects_v2_item webhook)
Factory VPS (slim)
  ├── Resolves status via GraphQL
  ├── Dispatches Claude Code Action via workflow_dispatch
  ├── Updates board columns via GraphQL
  └── Sends Telegram notifications
       │
       ▼
GitHub Actions Runner (Claude Code Action)
  ├── Connects to Factory MCP memory server
  ├── Reads CLAUDE.md for project rules
  ├── Creates branch, commits, opens PR
  └── Stores memory via MCP on completion
       │
       ▼
PR opened → Factory creates Docker preview env
PR merged → Built-in Project automation → "Done"
```

## Task Lifecycle

1. **Creation** — Openclaw creates GitHub issue via `gh` CLI, or human creates issue in GitHub. Built-in Project automation auto-adds to board with "Queued" status.

2. **Trigger** — `projects_v2_item` webhook fires on status change to "Queued". Factory receives it, resolves status via GraphQL, dispatches Claude Code Action via GitHub workflow_dispatch API. Factory updates board to "In Progress" and sends Telegram notification.

3. **Execution** — Claude Code Action runs on GitHub runner. Connects to Factory's MCP memory server for recall/store. CLAUDE.md provides project instructions. Agent creates branch, commits, opens PR.

4. **Success** — Action completes → GitHub sends `workflow_run` webhook (action `completed`, conclusion `success`) to Factory. Factory correlates the run back to the original issue via the workflow dispatch inputs, moves board to "In Review", sends Telegram notification with PR link.

5. **Failure** — Action fails → `workflow_run` webhook arrives with conclusion `failure`. Factory moves board to "Failed", sends Telegram notification with error from the run logs.

6. **Preview** — PR opened → Factory creates Docker preview environment (unchanged from current).

7. **Done** — PR merged → built-in Project automation moves to "Done". Factory tears down preview environment.

## GitHub Projects v2 Board

**Columns (status field):**
- Queued
- In Progress
- In Review
- Done
- Failed

**Built-in automations:**
- New issue added → status "Queued"
- PR merged → status "Done"
- Issue closed → status "Done"

**Programmatic updates (Factory via GraphQL):**
- Queued → In Progress (when agent dispatched)
- In Progress → In Review (when agent completes)
- In Progress → Failed (when agent fails)

## GitHub Actions Workflow

File: `.github/workflows/claude.yml`

Triggered by `workflow_dispatch` from Factory (not directly by project events, since `projects_v2_item` triggers must live in the org `.github` repo). Factory acts as the bridge.

The workflow:
- Checks out the repo
- Runs `anthropics/claude-code-action@v1`
- Authenticates via `claude setup-token` OAuth token stored as a GitHub Actions secret (`CLAUDE_CODE_OAUTH_TOKEN`). This uses the Claude Max subscription instead of per-token API billing. The Action supports this via the `anthropic_api_key` input which accepts OAuth tokens.
- Connects to Factory MCP memory server via `--mcp-config`
- CLAUDE.md and `.claude/rules/` provide project context
- Agent creates branch, commits, opens PR linking to the issue

Also configure a second trigger for `@claude` mentions on issues/PRs for ad-hoc requests. These bypass Factory entirely — the Action runs directly in response to the comment, no board updates or Telegram notifications. This is intentional: ad-hoc mentions are lightweight interactions that don't need orchestration.

```yaml
on:
  workflow_dispatch:
    inputs:
      issue_number:
        required: true
        type: number
  issue_comment:
    types: [created]
```

## Factory VPS (Slim)

### What stays (modified)

**api.py** — Gutted. New endpoints:
- `POST /webhooks/github` — receives multiple GitHub event types, routed by `X-GitHub-Event` header:
  - `projects_v2_item` (action `edited`) — status change on board → dispatch agent or ignore
  - `workflow_run` (action `completed`) — agent finished → update board, send Telegram
  - `pull_request` (action `opened`/`closed`/`merged`) — manage preview environments
  - `push` (ref `refs/heads/main`) — auto-deploy (unchanged)
- `GET /health` — health check

**memory.py** — Unchanged. SurrealDB vector memory with OpenAI embeddings.

**notifier.py** — Unchanged. Telegram notifications.

**db.py** — Simplified. Fresh schema on the experimental branch:
- `issue_runs` table: maps GitHub issue number → workflow run ID, repo, status, timestamps
- `preview_envs` table: maps PR number → preview URL, container ID, status
- Drop old `tasks`, `task_logs`, `workflows`, `workflow_steps`, `handoffs`, `messages` tables

**config.py** — Simplified. Remove Plane config, agent templates. Add GitHub Projects config (project ID, field IDs, status option IDs).

### What's new

**mcp_server.py** — MCP server exposing SurrealDB memory as tools:
- `memory_recall(repo, query, limit)` — vector search with BM25 fallback
- `memory_store(task_id, repo, agent_type, title, description, outcome, summary, error)` — store memory with embedding

Integrated into the FastAPI app as an SSE endpoint (`/mcp/sse`). No separate process — reuses the existing app lifecycle, SurrealDB connection, and auth middleware. The Claude Code Action connects to it via `--mcp-config` pointing to the public URL.

**github_projects.py** — GraphQL client for GitHub Projects v2:
- `get_item_status(item_node_id)` — resolve current status
- `update_item_status(item_id, status_option_id)` — move between columns
- `add_issue_to_project(issue_node_id)` — add issue to board
- `dispatch_workflow(repo, workflow_id, inputs)` — trigger Claude Code Action

### What gets removed

- `orchestrator.py` — agent management, prompt building, clarification system
- `runner.py` — subprocess management, timeouts
- `workspace.py` — worktree management (GitHub runners handle checkout)
- `plane.py` — Plane webhook parsing + API client
- `deps.py` — most service wiring
- `prompts.py` — prompt loading (replaced by CLAUDE.md in repos)
- Agent templates, workflow engine, handoffs, message board
- `revision_context.py` — coupled to orchestrator, no longer needed
- `auth.py` — Plane OAuth, no longer needed

**Stays (unchanged):**
- `docker_toolkit.py` — preview environments, still used by PR webhook handler

## Openclaw Changes

Update Openclaw's TOOLS.md to replace Factory API calls with GitHub CLI:

```
- **Create task:** gh issue create --repo Nomafin/{repo} --title "..." --body "..."
- **List tasks:** gh issue list --repo Nomafin/{repo} --state open
- **Check task:** gh issue view {number} --repo Nomafin/{repo}
```

No Factory API dependency for task creation. Openclaw talks directly to GitHub.

## MCP Memory Server

The Claude Code Action needs to connect to Factory's memory. Options:

- Factory exposes an MCP-compatible SSE endpoint (e.g., `https://plane.6a.fi/factory/mcp/`)
- The Action's workflow configures `--mcp-config` pointing to this URL
- Authentication via a shared token in the Action's secrets

The MCP server wraps the existing `AgentMemory` class:
- Tool `memory_recall` → calls `memory.recall(repo, query)`
- Tool `memory_store` → calls `memory.store(...)`

## CLAUDE.md

Each repo gets a `CLAUDE.md` replacing the current system prompts:

```markdown
# Project Rules

## When working on issues
- Create a branch named `agent/issue-{number}-{slug}`
- Commit with descriptive messages
- Open a PR linking to the issue with `Closes #{number}`
- End your response with a Summary and Changes section

## Code standards
[repo-specific rules currently in prompts/*.md]
```

## Branch Strategy

- Create `github-native` branch from current `main`
- All migration work happens on this branch
- VPS switches to `github-native` for testing
- If successful, merge to `main`
- If not, switch VPS back to `main`

## Config Changes

New config.yml structure:

```yaml
github:
  project_id: "PVT_..."       # GitHub Project node ID
  status_field_id: "PVTSSF_..." # Status field node ID
  statuses:
    queued: "option-id"
    in_progress: "option-id"
    in_review: "option-id"
    done: "option-id"
    failed: "option-id"
  workflow_id: "claude.yml"    # Workflow to dispatch
  default_org: "Nomafin"

telegram:
  bot_token: "..."
  chat_id: "..."

surrealdb:
  url: "ws://localhost:8200/rpc"
  user: "root"
  password: "..."

mcp:
  auth_token: "..."           # Token for MCP endpoint auth
```

## Environment Variables

```
GITHUB_TOKEN=ghp_...          # With project scope + workflow dispatch
OPENAI_API_KEY=sk-...         # For embeddings
SURREALDB_URL=ws://localhost:8200/rpc
SURREALDB_USER=root
SURREALDB_PASS=...
MCP_AUTH_TOKEN=...            # Shared secret for MCP endpoint
```
