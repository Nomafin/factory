# Factory

AI agent orchestrator that manages autonomous Claude-powered agents to execute tasks on GitHub repositories. Factory receives tasks via a REST API or Plane webhooks, spins up isolated workspaces, runs Claude Code CLI agents, and reports results back.

## Features

- **Task queue** with concurrent agent execution (configurable limit)
- **Four agent types**: coder, reviewer, researcher, and devops — each with tailored system prompts and tool access
- **Plane integration** for issue tracking — webhook-driven task creation and status updates
- **Isolated workspaces** via git worktrees so agents work on separate branches without interference
- **Agent memory** via SurrealDB — agents learn from past task outcomes using full-text search (vector search planned)
- **SQLite database** for task persistence and logging
- **REST API** for task management and monitoring

## Architecture

```
Plane webhook / REST API
        │
        ▼
   ┌──────────┐     ┌────────────┐     ┌──────────────┐
   │   API     │────▶│Orchestrator│────▶│ AgentRunner   │
   │ (FastAPI) │     │            │     │ (Claude Code) │
   └──────────┘     └─────┬──────┘     └──────────────┘
                          │
                ┌─────────┼──────────┐
                │         │          │
           ┌────▼───┐ ┌───▼─────┐ ┌─▼──────────┐
           │   DB   │ │  Repo   │ │AgentMemory │
           │(SQLite)│ │ Manager │ │(SurrealDB) │
           └────────┘ └─────────┘ └────────────┘
```

## Project Structure

```
factory/
├── config.yml                 # Main configuration
├── .env.example               # Environment variable template
├── prompts/                   # Agent system prompts
│   ├── coder.md
│   ├── reviewer.md
│   ├── researcher.md
│   └── devops.md
└── orchestrator/              # Python package
    ├── pyproject.toml
    ├── src/factory/
    │   ├── main.py            # FastAPI app entry point
    │   ├── api.py             # REST API routes + webhooks
    │   ├── orchestrator.py    # Core task processing
    │   ├── runner.py          # Claude Code CLI process management
    │   ├── workspace.py       # Git clone and worktree management
    │   ├── memory.py          # Agent memory (SurrealDB)
    │   ├── plane.py           # Plane webhook parsing + API client
    │   ├── notifier.py        # Telegram notifications
    │   ├── db.py              # SQLite schema and queries
    │   ├── deps.py            # FastAPI dependency injection
    │   ├── models.py          # Pydantic models
    │   └── config.py          # Configuration classes
    └── tests/
```

## Prerequisites

- Python 3.12+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and available on `PATH`
- A GitHub personal access token
- An Anthropic API key
- Docker (for SurrealDB — optional, memory features degrade gracefully)

## Setup

1. **Clone the repository and install dependencies:**

   ```bash
   cd orchestrator
   pip install -e .
   ```

2. **Configure environment variables** by copying `.env.example` to `.env`:

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   GITHUB_TOKEN=ghp_...
   FACTORY_AUTH_TOKEN=your-secret-token
   PLANE_API_KEY=pl_...
   ```

3. **Edit `config.yml`** to define your repositories, agent templates, concurrency limits, and Plane integration settings.

4. **Set up SurrealDB** (optional — agent memory):

   ```bash
   # Generate a password
   SURREALDB_PASS=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)

   # Create data directory and start SurrealDB
   mkdir -p surrealdb
   docker run -d --name surrealdb --restart always \
     -p 8200:8000 \
     -v $(pwd)/surrealdb:/data \
     surrealdb/surrealdb:latest start \
     --user root --pass "$SURREALDB_PASS" \
     rocksdb:/data/factory.db

   # Fix permissions (container runs as UID 65532)
   chown -R 65532:65532 surrealdb

   # Restart to apply permissions
   docker restart surrealdb
   ```

   Add to `.env`:

   ```
   SURREALDB_URL=ws://localhost:8200/rpc
   SURREALDB_USER=root
   SURREALDB_PASS=<your-generated-password>
   ```

   The schema is created automatically on first startup. If the env vars are not set or SurrealDB is unreachable, the orchestrator runs normally without memory.

5. **Start the server:**

   ```bash
   cd orchestrator
   uvicorn factory.main:app --host 0.0.0.0 --port 8100
   ```

## Configuration

`config.yml` controls the orchestrator behavior:

```yaml
max_concurrent_agents: 3
agent_timeout_minutes: 30

repos:
  my-repo:
    url: "https://github.com/org/my-repo.git"
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

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/tasks` | Create a task |
| `GET` | `/api/tasks` | List tasks (optional `?status=` filter) |
| `GET` | `/api/tasks/{id}` | Get task details |
| `POST` | `/api/tasks/{id}/run` | Start a queued task |
| `POST` | `/api/tasks/{id}/cancel` | Cancel a running task |
| `GET` | `/api/agents` | List active agents |
| `POST` | `/webhooks/plane` | Plane issue webhook |
| `GET` | `/health` | Health check |

### Create a task

```bash
curl -X POST http://localhost:8100/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Fix login timeout",
    "description": "Increase session timeout from 30s to 60s",
    "repo": "my-repo",
    "agent_type": "coder"
  }'
```

### Run a task

```bash
curl -X POST http://localhost:8100/api/tasks/1/run
```

## Task Lifecycle

```
queued → in_progress → in_review → done
                    └→ failed
queued → cancelled
```

1. **Queued** — task created via API or Plane webhook
2. **In Progress** — agent workspace prepared, Claude Code CLI running
3. **In Review** — agent finished successfully, branch ready for review
4. **Done** — task completed
5. **Failed** — agent exited with an error
6. **Cancelled** — task cancelled before or during execution

## Agent Memory

When SurrealDB is configured, agents build persistent memory across tasks:

- **After each task** — the orchestrator stores the task title, repo, outcome (success/failed), and a summary of the agent's output
- **Before each task** — the orchestrator queries SurrealDB for relevant past memories (matched by repo + full-text search on the task description) and injects them into the agent's prompt

This lets agents learn from previous runs: what approaches worked, what failed, and repo-specific patterns.

**Search strategy:**
- With `OPENAI_API_KEY` set: vector similarity search (OpenAI `text-embedding-3-small`, 1536d) with BM25 fallback
- Without `OPENAI_API_KEY`: BM25 full-text search only

To enable vector search, add to `.env`:

```
OPENAI_API_KEY=sk-...
```

Old memories stored without embeddings are still findable via BM25 fallback.

## Plane Integration

Factory integrates with [Plane](https://plane.so) (self-hosted or cloud) for issue tracking. When connected, issues moved to "Queued" in Plane automatically trigger agent runs, and the orchestrator updates issue states and posts progress comments as agents work.

### Setup

1. **Get your Plane API key** from your Plane instance under Settings > API Tokens.

2. **Find your project's state IDs.** Each Plane project has workflow states with UUIDs. You need the IDs for the states Factory will use. You can find them via the Plane API:

   ```bash
   curl -H "X-API-Key: $PLANE_API_KEY" \
     https://your-plane.example.com/api/v1/workspaces/YOUR_SLUG/projects/PROJECT_ID/states/
   ```

3. **Add the Plane section to `config.yml`:**

   ```yaml
   plane:
     base_url: "https://your-plane.example.com"
     api_key: "plane_api_..."
     workspace_slug: "your-workspace"
     project_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
     default_repo: "my-repo"  # fallback when issue has no repo: label
     states:
       queued: "state-uuid-for-queued"
       in_progress: "state-uuid-for-in-progress"
       in_review: "state-uuid-for-in-review"
       done: "state-uuid-for-done"
       failed: "state-uuid-for-failed"
       cancelled: "state-uuid-for-cancelled"
   ```

4. **Set up a webhook in Plane** pointing to your Factory instance:

   - URL: `https://your-domain.com/factory/webhooks/plane` (or `http://localhost:8100/api/webhooks/plane` for local dev)
   - Trigger on: Issue events

### How it works

- **Issue created/moved to "Queued"** — Factory creates a task and starts an agent
- **Issue moved to "Cancelled"** — Factory cancels the running agent
- **Agent starts** — Plane issue moves to "In Progress", comment posted with branch name
- **Agent posts progress** — periodic comments with agent output summaries
- **Agent succeeds** — issue moves to "In Review", comment with PR link
- **Agent fails** — issue moves to "Failed", comment with error details

### Issue labels

Use Plane labels to control which repo and agent type are used:

- `repo:my-repo` — target repository (must match a key in `config.yml` repos)
- `coder` / `reviewer` / `researcher` / `devops` — agent type (defaults to `coder`)

If no `repo:` label is set, the `default_repo` from config is used.

## Telegram Notifications

Factory can send real-time notifications to a Telegram chat when agents start, complete, or fail.

### Setup

1. **Create a Telegram bot** via [@BotFather](https://t.me/BotFather):
   - Send `/newbot` and follow the prompts
   - Copy the bot token (e.g. `1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ`)

2. **Get your chat ID:**
   - Add the bot to your group chat (or start a DM with it)
   - Send a message in the chat
   - Fetch updates to find the chat ID:
     ```bash
     curl https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
     ```
   - Look for `"chat":{"id":...}` in the response — that's your chat ID

3. **Add to `config.yml`:**

   ```yaml
   telegram:
     bot_token: "1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ"
     chat_id: "your-chat-id"
   ```

### Notifications sent

| Event | Message |
|-------|---------|
| Agent started | Task title + branch name |
| Agent completed | Task title + PR URL |
| Agent failed | Task title + error snippet |
| Agent cancelled | Task title |
| Orphaned task recovered | Task title (on orchestrator restart) |

Both Plane and Telegram are optional — if their config sections are empty or missing, the orchestrator works without them.

## Agent Types

| Agent | Role | Tools |
|-------|------|-------|
| **Coder** | Implements features and fixes bugs | Read, Edit, Bash, Glob, Grep |
| **Reviewer** | Reviews code for quality and correctness | Read, Glob, Grep |
| **Researcher** | Gathers and analyzes information | WebSearch, WebFetch, Read |
| **DevOps** | Manages infrastructure and deployment | Bash, Read, Edit |

## Testing

```bash
cd orchestrator
pip install -e ".[dev]"
pytest
```

## License

See [LICENSE](LICENSE) for details.
