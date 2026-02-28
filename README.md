# Factory

AI agent orchestrator that manages autonomous Claude-powered agents to execute tasks on GitHub repositories. Factory receives tasks via a REST API or Plane webhooks, spins up isolated workspaces, runs Claude Code CLI agents, and reports results back.

## Features

- **Task queue** with concurrent agent execution (configurable limit)
- **Multi-agent workflows** with conditional steps, loops, and automatic iteration
- **Coder-reviewer collaboration** — automatic code review cycles with revision loops
- **Agent clarification system** — agents can pause to ask users questions mid-task
- **Agent message board** — real-time inter-agent communication with web UI
- **Agent handoffs** — context passing between agents in workflows
- **Five agent types**: coder, reviewer, researcher, devops, and coder_revision
- **Timeout enforcement** — watchdog kills stuck agents (total + idle timeouts)
- **Plane integration** — webhook-driven task creation and status updates
- **Docker test/preview environments** for pre-PR testing with automatic cleanup
- **Preview URL notifications** — Telegram alerts when preview environments are deployed
- **Isolated workspaces** via git worktrees
- **Agent memory** via SurrealDB with vector search
- **Telegram notifications** for task events
- **SQLite database** for persistence

## Architecture

```
Plane webhook / REST API
        │
        ▼
   ┌──────────┐     ┌────────────┐     ┌──────────────┐
   │   API    │────▶│Orchestrator│────▶│ AgentRunner  │
   │ (FastAPI)│     │            │     │(Claude Code) │
   └──────────┘     └─────┬──────┘     └──────────────┘
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
   ┌────▼───┐     ┌──────▼──────┐    ┌─────▼─────┐
   │   DB   │     │   Workflows │    │  Message  │
   │(SQLite)│     │  & Handoffs │    │   Board   │
   └────────┘     └─────────────┘    └───────────┘
```

## Quick Start

```bash
# Clone and install
git clone https://github.com/your-org/factory.git
cd factory/orchestrator
pip install -e .

# Configure
cp .env.example .env
# Edit .env with your API keys

# Edit config.yml with your settings

# Run
uvicorn factory.main:app --host 0.0.0.0 --port 8100
```

## Project Structure

```
factory/
├── config.yml                 # Main configuration
├── .env.example               # Environment variable template
├── AGENTS.md                  # Agent guide (Docker envs, communication)
├── prompts/                   # Agent system prompts
│   ├── coder.md              # Feature implementation
│   ├── coder_revision.md     # Code revision after review
│   ├── reviewer.md           # Code review
│   ├── researcher.md         # Research tasks
│   ├── devops.md             # Infrastructure tasks
│   └── templates/            # Reference templates
│       └── docker-compose.preview.yml
├── orchestrator/
│   ├── src/factory/
│   │   ├── main.py           # FastAPI app + static files
│   │   ├── api.py            # REST API routes
│   │   ├── orchestrator.py   # Core task & workflow processing
│   │   ├── runner.py         # Agent process management + timeouts
│   │   ├── workspace.py      # Git worktree management
│   │   ├── memory.py         # SurrealDB agent memory
│   │   ├── db.py             # SQLite persistence
│   │   ├── models.py         # Pydantic models
│   │   ├── config.py         # Configuration classes
│   │   ├── plane.py          # Plane integration
│   │   └── notifier.py       # Telegram notifications
│   ├── static/
│   │   └── messages.html     # Message board web UI
│   └── tests/
└── docs/
    └── plans/                # Design documents
```

## Docker Test Environments

Agents can spin up Docker environments to test changes before creating PRs. See [AGENTS.md](AGENTS.md) for the full guide.

**Quick overview:**

```python
# In agent code — spin up a test environment
from docker_toolkit import spin_up_test_env, tear_down_test_env

url = spin_up_test_env("docker-compose.yml", service_port=3000)
# Run tests against the URL, then clean up
tear_down_test_env()
```

- Test environments are ephemeral and cleaned up automatically on task completion
- Preview environments persist until the associated PR is merged/closed
- All environments get public URLs via Traefik (e.g., `https://task-42.preview.factory.6a.fi`)
- Preview URL is automatically sent to Telegram when environment is ready
- A reference `docker-compose.yml` template is at `prompts/templates/docker-compose.preview.yml`

**Infrastructure Setup:** To set up Traefik, nginx, SSL certificates, and cleanup automation on your server, see [docs/infrastructure-setup.md](docs/infrastructure-setup.md).

## Prerequisites

- Python 3.12+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and on PATH
- GitHub personal access token
- Anthropic API key
- Docker (required for test/preview environments and optional SurrealDB agent memory)

## Installation

### 1. Clone and Install Dependencies

```bash
git clone https://github.com/your-org/factory.git
cd factory/orchestrator
python -m venv ../.venv
source ../.venv/bin/activate
pip install -e .
```

### 2. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your keys:

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...

# Optional - for API authentication
FACTORY_AUTH_TOKEN=your-secret-token

# Optional - for Plane integration
PLANE_API_KEY=plane_api_...

# Optional - for agent memory (SurrealDB)
SURREALDB_URL=ws://localhost:8200/rpc
SURREALDB_USER=root
SURREALDB_PASS=your-password

# Optional - for vector search in agent memory
OPENAI_API_KEY=sk-...
```

### 3. Configure Factory

Edit `config.yml`:

```yaml
# Agent concurrency and timeouts
max_concurrent_agents: 3
agent_timeout_minutes: 60        # Kill agent after 60 min total
agent_activity_timeout_minutes: 15  # Kill agent after 15 min idle

# Repositories agents can work on
repos:
  my-app:
    url: "https://github.com/your-org/my-app.git"
    default_agent: "coder"

# Agent configurations
agent_templates:
  coder:
    system_prompt_file: "prompts/coder.md"
    allowed_tools: ["Read", "Edit", "Bash", "Glob", "Grep"]
    timeout_minutes: 60
  reviewer:
    system_prompt_file: "prompts/reviewer.md"
    allowed_tools: ["Read", "Glob", "Grep"]
    timeout_minutes: 30
  researcher:
    system_prompt_file: "prompts/researcher.md"
    allowed_tools: ["WebSearch", "WebFetch", "Read"]
    timeout_minutes: 30
  devops:
    system_prompt_file: "prompts/devops.md"
    allowed_tools: ["Bash", "Read", "Edit"]
    timeout_minutes: 30

# Message board for inter-agent communication
message_board:
  enabled: true
  telegram_forward: true          # Forward messages to Telegram
  telegram_chat_id: ""            # Separate chat (optional, uses main if empty)
  forward_types: ["error", "question", "handoff"]

# Multi-step workflows
workflows:
  code_review:
    max_iterations: 3             # Max revision cycles
    steps:
      - agent: coder
        output: initial_code
      - agent: reviewer
        input: initial_code
        output: review
      - agent: coder
        input: review
        output: revision
        condition: "has_issues"   # Only run if review has issues
        loop_to: review           # Loop back for another review
        prompt_template: "prompts/coder_revision.md"

# Telegram notifications (optional)
telegram:
  bot_token: "your-bot-token"
  chat_id: "your-chat-id"

# Plane integration (optional)
plane:
  base_url: "https://plane.example.com"
  api_key: "plane_api_..."
  workspace_slug: "your-workspace"
  project_id: "project-uuid"
  default_repo: "my-app"
  states:
    queued: "state-uuid"
    in_progress: "state-uuid"
    in_review: "state-uuid"
    done: "state-uuid"
    failed: "state-uuid"
    cancelled: "state-uuid"
```

### 4. Set Up SurrealDB (Optional)

Agent memory requires SurrealDB:

```bash
# Generate password
SURREALDB_PASS=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)
echo "SurrealDB password: $SURREALDB_PASS"

# Create data directory
mkdir -p surrealdb

# Start SurrealDB
docker run -d --name surrealdb --restart always \
  -p 8200:8000 \
  -v $(pwd)/surrealdb:/data \
  surrealdb/surrealdb:latest start \
  --user root --pass "$SURREALDB_PASS" \
  rocksdb:/data/factory.db

# Fix permissions
chown -R 65532:65532 surrealdb
docker restart surrealdb
```

Add to `.env`:
```
SURREALDB_URL=ws://localhost:8200/rpc
SURREALDB_USER=root
SURREALDB_PASS=<your-password>
```

### 5. Start the Server

```bash
cd orchestrator
uvicorn factory.main:app --host 0.0.0.0 --port 8100
```

For production, use a process manager:

```bash
# Using systemd
sudo tee /etc/systemd/system/factory.service << EOF
[Unit]
Description=Factory Agent Orchestrator
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/factory
Environment=PATH=/opt/factory/.venv/bin
EnvironmentFile=/opt/factory/.env
ExecStart=/opt/factory/.venv/bin/uvicorn factory.main:app --host 0.0.0.0 --port 8100
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable factory
sudo systemctl start factory
```

### 6. Set Up Reverse Proxy (Optional)

For HTTPS access, use nginx:

```nginx
server {
    server_name factory.example.com;

    location /api/messages/stream/sse {
        proxy_pass http://localhost:8100;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;
    }

    location / {
        proxy_pass http://localhost:8100;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    listen 443 ssl;
    ssl_certificate /etc/letsencrypt/live/factory.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/factory.example.com/privkey.pem;
}
```

## API Reference

### Tasks

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/tasks` | Create a task |
| `GET` | `/api/tasks` | List tasks (`?status=queued\|in_progress\|done\|failed`) |
| `GET` | `/api/tasks/{id}` | Get task details |
| `POST` | `/api/tasks/{id}/run` | Start a queued task |
| `POST` | `/api/tasks/{id}/cancel` | Cancel a running task |
| `GET` | `/api/tasks/{id}/handoffs` | Get handoffs for a task |

#### Create and Run a Task

```bash
# Create task
curl -X POST http://localhost:8100/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Add user authentication",
    "description": "Implement JWT-based auth with login/logout endpoints",
    "repo": "my-app",
    "agent_type": "coder"
  }'

# Run task (if not using auto_run)
curl -X POST http://localhost:8100/api/tasks/1/run

# Or create and run in one call
curl -X POST "http://localhost:8100/api/tasks?auto_run=true" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Fix login bug",
    "description": "Session expires too quickly",
    "repo": "my-app",
    "agent_type": "coder"
  }'
```

### Workflows

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/workflows` | Create a workflow |
| `GET` | `/api/workflows` | List workflows |
| `GET` | `/api/workflows/{id}` | Get workflow details |
| `POST` | `/api/workflows/{id}/cancel` | Cancel a workflow |
| `POST` | `/api/workflows/code_review` | Start code review workflow |
| `GET` | `/api/workflows/{id}/handoffs` | Get handoffs in a workflow |

#### Start a Code Review Workflow

The code review workflow automatically:
1. Runs a coder agent to implement the feature
2. Runs a reviewer agent to review the code
3. If issues found, runs coder again with revision prompt
4. Loops until approved or max iterations reached

```bash
curl -X POST http://localhost:8100/api/workflows/code_review \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Implement caching layer",
    "description": "Add Redis caching for API responses",
    "repo": "my-app"
  }'
```

### Messages (Agent Communication)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/messages` | Post a message |
| `GET` | `/api/messages` | List messages (with filters) |
| `GET` | `/api/messages/{id}` | Get a message |
| `GET` | `/api/messages/{id}/thread` | Get message thread |
| `GET` | `/api/messages/stream/sse` | Real-time SSE stream |

#### Web UI

Access the message board at: `http://localhost:8100/messages`

#### Query Messages

```bash
# List recent messages
curl http://localhost:8100/api/messages?limit=20

# Filter by task
curl http://localhost:8100/api/messages?task_id=5

# Filter by type
curl http://localhost:8100/api/messages?message_type=question

# Search messages
curl http://localhost:8100/api/messages?search=authentication
```

### Handoffs

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/handoffs` | Create a handoff |
| `GET` | `/api/handoffs/{id}` | Get handoff details |

### Agents

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/agents` | List running agents |

### Other

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/webhooks/plane` | Plane webhook receiver |

## Workflows

Workflows orchestrate multiple agents working together on a task.

### Workflow Definition

```yaml
workflows:
  my_workflow:
    max_iterations: 3
    steps:
      - agent: coder
        output: code              # Store output with this key
      
      - agent: reviewer
        input: code               # Use previous step's output
        output: review
      
      - agent: coder
        input: review
        output: revision
        condition: "has_issues"   # Only run if condition met
        loop_to: review           # Loop back to this step
        prompt_template: "prompts/coder_revision.md"
```

### Built-in Conditions

- `has_issues` — Review contains blocker or major issues
- `no_issues` — Review has no significant issues
- `review_approved` — Review explicitly approved

### Workflow Lifecycle

```
pending → running → completed
              └──→ failed
              └──→ cancelled
```

## Inter-Agent Communication

Agents can communicate with each other via the message board.

### Posting Messages from Agents

Agents output JSON to post messages:

```json
{"type": "message", "to": "reviewer", "content": "Should I use Redis or Memcached for caching?", "message_type": "question"}
```

### Message Types

| Type | Purpose |
|------|---------|
| `info` | General updates |
| `question` | Questions for other agents |
| `handoff` | Passing work to another agent |
| `status` | Progress updates |
| `error` | Error reports |

### Telegram Forwarding

Configure which message types forward to Telegram:

```yaml
message_board:
  enabled: true
  telegram_forward: true
  forward_types: ["error", "question", "handoff"]
```

## Agent Types

| Agent | Role | Default Tools |
|-------|------|---------------|
| **coder** | Implements features, fixes bugs | Read, Edit, Bash, Glob, Grep |
| **coder_revision** | Revises code based on review feedback | Read, Edit, Bash, Glob, Grep |
| **reviewer** | Reviews code for quality | Read, Glob, Grep |
| **researcher** | Gathers information | WebSearch, WebFetch, Read |
| **devops** | Infrastructure tasks | Bash, Read, Edit |

## Timeout Enforcement

Factory includes a watchdog that prevents stuck agents:

```yaml
agent_timeout_minutes: 60         # Max total runtime
agent_activity_timeout_minutes: 15  # Max time without output
```

The watchdog checks every 30 seconds and terminates agents that exceed either limit.

## Agent Memory

With SurrealDB configured, agents build persistent memory:

- **After tasks** — Stores task outcome, summary, and learnings
- **Before tasks** — Retrieves relevant past memories via search

### Search Strategies

| Configuration | Search Method |
|---------------|---------------|
| With `OPENAI_API_KEY` | Vector similarity (embeddings) + BM25 fallback |
| Without | BM25 full-text search only |

## Plane Integration

Connect to [Plane](https://plane.so) for issue tracking:

1. Issues moved to "Queued" → Factory creates and runs task
2. Agent starts → Issue moves to "In Progress"
3. Agent completes → Issue moves to "In Review" with PR link
4. Agent fails → Issue moves to "Failed" with error

### Issue Labels

- `repo:my-app` — Target repository
- `coder` / `reviewer` / `researcher` / `devops` — Agent type

## Telegram Notifications

Events that trigger notifications:

| Event | Message |
|-------|---------|
| Task started | Title + branch name |
| Task completed | Title + PR URL |
| Task failed | Title + error snippet |
| Workflow iteration | Iteration count |
| Agent message (if configured) | Message content |

## Development

### Running Tests

```bash
cd orchestrator
pip install -e ".[dev]"
pytest
```

### Project Dependencies

```bash
pip install fastapi uvicorn aiosqlite pydantic pyyaml httpx surrealdb
```

## Troubleshooting

### Agent gets stuck

Check the timeout settings in `config.yml`:
```yaml
agent_timeout_minutes: 60
agent_activity_timeout_minutes: 15
```

The watchdog will kill agents exceeding these limits.

### SurrealDB connection fails

Verify SurrealDB is running:
```bash
docker ps | grep surrealdb
curl http://localhost:8200/health
```

Memory features degrade gracefully — Factory works without SurrealDB.

### Plane webhook not triggering

1. Check webhook URL is correct: `/webhooks/plane`
2. Verify Plane API key in config
3. Check Factory logs for webhook payloads

### Messages not appearing

1. Verify `message_board.enabled: true` in config
2. Check the web UI at `/messages`
3. Ensure agents are outputting valid JSON format

## License

See [LICENSE](LICENSE) for details.
