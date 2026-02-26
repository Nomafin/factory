# Factory Agent Farm Design

## Overview

An agent orchestration platform running on a VPS (root@reitti.6a.fi) that enables commanding and monitoring multiple AI agents through Telegram (via Openclaw) and a project management UI (Plane.so).

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│  Telegram    │────>│   Openclaw Bot    │────>│             │
│  (user)      │<────│   (existing)      │<────│             │
└─────────────┘     └──────────────────┘     │             │
                                              │ Orchestrator │
┌─────────────┐     ┌──────────────────┐     │   (Python)   │
│  Plane.so   │────>│   Webhooks        │────>│             │
│  (kanban UI) │<────│                   │<────│             │
└─────────────┘     └──────────────────┘     │             │
                                              └──────┬──────┘
                                                     │
                              ┌───────────┬──────────┼──────────┐
                              v           v          v          v
                         ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
                         │Agent 1 │ │Agent 2 │ │Agent 3 │ │Agent N │
                         │(claude)│ │(claude)│ │(claude)│ │(claude)│
                         │worktree│ │worktree│ │worktree│ │worktree│
                         └────────┘ └────────┘ └────────┘ └────────┘
```

Three main components, all on the VPS:

1. **Plane.so** (self-hosted via Docker Compose) - kanban board + task tracking
2. **Orchestrator** (custom Python FastAPI service) - the brain that manages agents
3. **Agents** (Claude Agent SDK instances) - each runs in an isolated git worktree

## Orchestrator

A Python FastAPI service that serves as the central hub.

### Inbound interfaces

- **HTTP API** - Openclaw calls this to submit tasks and query status. Configured as a custom tool in Openclaw pointing at `http://localhost:8000/api/tasks`.
- **Plane webhooks** - When issues are created or moved in Plane, webhooks fire to the orchestrator. State changes trigger agent actions (e.g., issue moved to "Queued" spawns an agent).

### Agent management

- Maintains a queue of pending tasks
- Spawns Claude Agent SDK instances (Python), each in its own process
- Limits concurrency (max 3 agents given VPS resources)
- Each agent gets an isolated git worktree cloned from the target GitHub repo
- Monitors agent progress and enforces timeouts (default 30 min)

### Outbound updates

- Updates Plane issues via the Python SDK (state changes, comments with agent output)
- Sends status updates to Openclaw via callback URL (relayed to Telegram)
- On agent completion: creates a PR on GitHub, updates Plane issue, notifies via Telegram

### Data storage

- SQLite database for task queue, agent sessions, and logs

### API endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /api/tasks` | Create a new task (from Openclaw or direct) |
| `GET /api/tasks` | List tasks and their status |
| `GET /api/tasks/{id}` | Task detail + agent logs |
| `POST /api/tasks/{id}/cancel` | Cancel a running agent |
| `POST /api/webhooks/plane` | Receive Plane webhook events |
| `GET /api/agents` | List running agents and their status |

## Agent Lifecycle

### 1. Task creation

User creates an issue in Plane (kanban board) or messages Openclaw on Telegram. Orchestrator receives the task and queues it.

### 2. Agent setup

- Orchestrator checks concurrency limit (max 3 agents)
- Clones target GitHub repo (or pulls if cached in `/opt/factory/repos/`)
- Creates a git worktree + new branch: `agent/task-{id}-{slug}`
- Updates Plane issue to "In Progress"

### 3. Agent execution

- Spawns a Claude Agent SDK process pointed at the worktree directory
- Agent gets a system prompt with context: task description, repo conventions, constraints
- Agent works autonomously - reads code, edits files, runs tests
- Orchestrator periodically captures output and posts comments to Plane issue
- Openclaw gets periodic status updates relayed to Telegram

### 4. Completion

- Agent finishes: orchestrator commits changes, pushes branch, creates GitHub PR
- Plane issue moves to "In Review" with PR link
- Telegram notification sent via Openclaw

### 5. Failure handling

- Agent errors or hits timeout: orchestrator kills it
- Plane issue moves to "Failed" with error logs in comments
- Telegram notification sent
- Worktree preserved for debugging

## Agent Templates

| Type | System prompt | Allowed tools | Use case |
|------|--------------|---------------|----------|
| `coder` | Software engineer | Read, Edit, Bash, Glob, Grep | Feature work, bug fixes |
| `reviewer` | Code reviewer | Read, Glob, Grep (no Edit) | PR reviews |
| `researcher` | Research assistant | WebSearch, WebFetch, Read | Research, documentation |
| `devops` | Sysadmin | Bash, Read, Edit | Server config, deployment |

## Plane Setup

### Custom states

| State Group | State | Meaning |
|-------------|-------|---------|
| Backlog | Backlog | Ideas, not ready for agents |
| Unstarted | Queued | Ready for an agent to pick up |
| Started | In Progress | Agent is working on it |
| Started | In Review | Agent done, PR created, awaiting review |
| Completed | Done | PR merged |
| Cancelled | Failed | Agent failed |
| Cancelled | Cancelled | Manually cancelled |

### Two-way sync

Plane to Orchestrator (webhooks):
- Issue moved to "Queued" triggers agent spawn
- Issue moved to "Cancelled" triggers agent kill

Orchestrator to Plane (Python SDK):
- Agent starts: issue moves to "In Progress"
- Agent posts progress: comments added to issue
- Agent finishes: issue moves to "In Review", PR link added
- Agent fails: issue moves to "Failed", error logs attached

### Issue format convention

```
Title: Fix login timeout on slow connections
Labels: [coder], [repo:myapp]
Description: Users report timeouts when logging in on 3G.
             Look at src/auth/login.ts, the fetch timeout is too short.
```

Labels indicate agent type and target repo. Description becomes part of the agent prompt.

### Openclaw integration

Openclaw configured with HTTP tools pointing at orchestrator API:
- "Fix the login bug in myapp" -> `POST /api/tasks`
- "What are the agents doing?" -> `GET /api/agents`
- "Cancel task 42" -> `POST /api/tasks/42/cancel`

## Deployment

### Directory structure

```
/opt/factory/
├── orchestrator/          # Python FastAPI service
│   ├── src/
│   ├── pyproject.toml
│   └── Dockerfile
├── repos/                 # Cached repo clones
│   └── myapp/
├── worktrees/             # Active agent worktrees
│   └── task-42-fix-login/
├── docker-compose.yml     # Plane + Orchestrator + Postgres + Redis
├── config.yml             # Agent templates, repo mappings, limits
└── .env                   # API keys (ANTHROPIC_API_KEY, GITHUB_TOKEN)
```

### Docker Compose stack

| Service | Purpose | Approx RAM |
|---------|---------|------------|
| `plane-web` | Plane UI | ~512MB |
| `plane-api` | Plane backend | ~512MB |
| `plane-worker` | Plane background jobs | ~256MB |
| `postgres` | Database for Plane + Orchestrator | ~512MB |
| `redis` | Plane cache + task queue | ~128MB |
| `orchestrator` | FastAPI service | ~256MB |

Base footprint: ~2-2.5GB RAM, leaving 5.5-13.5GB for agents.

### Agent execution

Agents run on the host (not in Docker) because they need:
- Access to git repos and worktrees on the filesystem
- Node.js 22+ for Claude Agent SDK
- GitHub CLI (`gh`) for creating PRs

### Configuration

```yaml
max_concurrent_agents: 3
agent_timeout_minutes: 30
github_org: "your-github-username"

repos:
  myapp:
    url: "git@github.com:you/myapp.git"
    default_agent: "coder"

agent_templates:
  coder:
    system_prompt_file: "prompts/coder.md"
    allowed_tools: ["Read", "Edit", "Bash", "Glob", "Grep"]
    timeout_minutes: 30
  reviewer:
    system_prompt_file: "prompts/reviewer.md"
    allowed_tools: ["Read", "Glob", "Grep"]
    timeout_minutes: 15
  researcher:
    system_prompt_file: "prompts/researcher.md"
    allowed_tools: ["WebSearch", "WebFetch", "Read"]
    timeout_minutes: 20
  devops:
    system_prompt_file: "prompts/devops.md"
    allowed_tools: ["Bash", "Read", "Edit"]
    timeout_minutes: 15
```

### Security

- `ANTHROPIC_API_KEY` and `GITHUB_TOKEN` in `.env`, never committed
- Plane behind reverse proxy with HTTPS
- Orchestrator API authenticated with bearer token
- Agents run as non-root user with limited filesystem access
