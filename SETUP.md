# Factory — Setup Guide

Step-by-step instructions to get your own Factory instance running.

## Prerequisites

Before you begin, make sure you have:

- **Python 3.12+** — check with `python3 --version`
- **Git** — check with `git --version`
- **Claude Code CLI** — install from [docs.anthropic.com/en/docs/claude-code](https://docs.anthropic.com/en/docs/claude-code), then verify with `claude --version`
- **An Anthropic API key** — get one at [console.anthropic.com](https://console.anthropic.com)
- **A GitHub personal access token** — create one at [github.com/settings/tokens](https://github.com/settings/tokens) with `repo` scope

Optional (for Plane integration):

- A **Plane** instance (self-hosted or cloud) with an API key

---

## Step 1: Clone the repository

```bash
git clone https://github.com/your-org/factory.git
cd factory
```

## Step 2: Set up environment variables

Copy the example and fill in your values:

```bash
cp .env.example .env
```

Edit `.env` with your editor:

```bash
# Required — your Anthropic API key for Claude
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx

# Required — GitHub personal access token with repo scope
GITHUB_TOKEN=ghp_xxxxxxxxxxxx

# Optional — protects the Factory API from unauthorized access
# Generate with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
FACTORY_AUTH_TOKEN=your-secret-token

# Optional — only needed if using Plane integration
PLANE_API_KEY=plane_api_xxxxxxxxxxxx

# Optional — override the root directory (defaults to the repo directory)
# FACTORY_ROOT=/path/to/factory
```

Load the variables into your shell (or use a tool like `direnv`):

```bash
export $(grep -v '^#' .env | xargs)
```

## Step 3: Create your configuration file

Copy the example and customise it:

```bash
cp config.yml.example config.yml
```

### 3a. Add your repositories

Under the `repos` section, add each GitHub repository you want agents to work on:

```yaml
repos:
  my-app:
    url: "https://github.com/your-org/my-app.git"
    default_agent: "coder"
  another-repo:
    url: "https://github.com/your-org/another-repo.git"
    default_agent: "coder"
```

The key (e.g. `my-app`) is the short name you use when creating tasks.

### 3b. Configure agent templates (optional)

The defaults work well out of the box. You can adjust tool access and timeouts:

```yaml
agent_templates:
  coder:
    system_prompt_file: "prompts/coder.md"
    allowed_tools: ["Read", "Edit", "Bash", "Glob", "Grep"]
    timeout_minutes: 30
```

### 3c. Configure Plane integration (optional)

If you use [Plane](https://plane.so) for project management, fill in the `plane` section:

```yaml
plane:
  base_url: "https://your-plane-instance.example.com"
  api_key: ""  # Or set PLANE_API_KEY env var instead
  workspace_slug: "your-workspace"
  project_id: "your-project-uuid"
  states:
    in_progress: "state-uuid"
    in_review: "state-uuid"
    done: "state-uuid"
    failed: "state-uuid"
    cancelled: "state-uuid"
```

To find the state UUIDs, use the Plane API:

```bash
curl -H "X-API-Key: $PLANE_API_KEY" \
  https://your-plane-instance/api/v1/workspaces/your-workspace/projects/your-project-uuid/states/
```

If you don't use Plane, leave the `plane` section with empty values — the integration is disabled automatically when no API key is set.

## Step 4: Install Python dependencies

```bash
cd orchestrator
pip install -e .
```

Or use a virtual environment (recommended):

```bash
python3 -m venv .venv
source .venv/bin/activate
cd orchestrator
pip install -e .
```

## Step 5: Start the server

```bash
cd orchestrator
uvicorn factory.main:app --host 0.0.0.0 --port 8100
```

Verify it's running:

```bash
curl http://localhost:8100/health
# {"status":"ok"}
```

## Step 6: Create and run your first task

Create a task:

```bash
curl -X POST http://localhost:8100/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Add a hello world endpoint",
    "description": "Add a GET /hello endpoint that returns {\"message\": \"hello world\"}",
    "repo": "my-app",
    "agent_type": "coder"
  }'
```

This returns a task object with an `id`. Start it:

```bash
curl -X POST http://localhost:8100/api/tasks/1/run
```

Monitor progress:

```bash
curl http://localhost:8100/api/tasks/1
```

List active agents:

```bash
curl http://localhost:8100/api/agents
```

---

## Optional: Set up Plane webhooks

To automatically create tasks when Plane issues are moved to a "Queued" state:

1. In Plane, go to your project settings > Webhooks
2. Add a new webhook pointing to `http://your-server:8100/api/webhooks/plane`
3. Select "Issue" events

Issues need labels to tell Factory which repo and agent to use:
- Add a label matching a repo key from your config (e.g. `repo:my-app`)
- Add a label matching an agent type (e.g. `coder`, `reviewer`, `researcher`, or `devops`)

When an issue with these labels moves to the "Queued" state, Factory automatically picks it up.

---

## Optional: Running as a system service

To keep Factory running in the background, create a systemd service:

```bash
sudo tee /etc/systemd/system/factory.service << EOF
[Unit]
Description=Factory Agent Orchestrator
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
EnvironmentFile=$(pwd)/.env
ExecStart=$(which uvicorn) factory.main:app --host 0.0.0.0 --port 8100
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now factory
```

---

## Running tests

```bash
cd orchestrator
pip install -e ".[dev]"
pytest
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `claude: command not found` | Install Claude Code CLI and ensure it's on your PATH |
| Tasks stuck in "queued" | Check that `ANTHROPIC_API_KEY` and `GITHUB_TOKEN` are set |
| Plane webhook not working | Verify the webhook URL is reachable from your Plane instance |
| `Permission denied` cloning repos | Check that your `GITHUB_TOKEN` has `repo` scope |
| Agent timeout | Increase `timeout_minutes` in the agent template config |

## Directory layout at runtime

Once running, Factory creates these directories (all gitignored):

```
factory/
├── repos/         # Bare clones of configured repositories
├── worktrees/     # Git worktrees where agents do their work
└── factory.db     # SQLite database for tasks and logs
```
