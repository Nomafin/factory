# Factory Agents Guide

This document describes how Factory agents work and how to configure projects to take advantage of agent capabilities.

## Agent Types

| Agent | Role | System Prompt |
|-------|------|---------------|
| **coder** | Implements features, fixes bugs, writes tests | `prompts/coder.md` |
| **coder_revision** | Revises code based on review feedback | `prompts/coder_revision.md` |
| **reviewer** | Reviews code for quality, bugs, and security | `prompts/reviewer.md` |
| **researcher** | Gathers information and analyzes findings | `prompts/researcher.md` |
| **devops** | Infrastructure and configuration tasks | `prompts/devops.md` |

## Docker Test Environments

Factory agents can spin up Docker environments to test changes before creating PRs. This feature uses the `docker_toolkit` module and requires Docker on the host.

### How It Works

1. **Test environments** are ephemeral — spun up for testing, torn down when the task completes
2. **Preview environments** are long-lived — spun up when a PR is created, torn down when the PR is merged or closed
3. All environments get public URLs via Traefik reverse proxy (e.g., `https://task-42.preview.factory.6a.fi`)
4. Containers are labelled with Factory metadata for automatic lifecycle management

### Agent API

Agents use convenience functions from `docker_toolkit`:

```python
from docker_toolkit import spin_up_test_env, tear_down_test_env, spin_up_preview_env

# Ephemeral test environment
url = spin_up_test_env("docker-compose.yml", service_port=3000)
# ... run tests ...
tear_down_test_env()

# Long-lived PR preview
url = spin_up_preview_env(pr_number=15)
```

### Configuring Your Project

To enable Docker environments for your project:

1. **Add a `docker-compose.yml`** (or use the template at `prompts/templates/docker-compose.preview.yml`)

2. **Add Factory labels** to your service (required for cleanup):
   ```yaml
   labels:
     - "factory.task-id=${FACTORY_TASK_ID}"
     - "factory.repo=${FACTORY_REPO}"
     - "factory.env-type=test"
     - "factory.created=${FACTORY_CREATED:-0}"
   ```

3. **Add Traefik labels** for public URL routing:
   ```yaml
   labels:
     - "traefik.enable=true"
     - "traefik.http.routers.app.rule=Host(`${FACTORY_HOSTNAME}`)"
     - "traefik.http.routers.app.entrypoints=websecure"
     - "traefik.http.routers.app.tls=true"
     - "traefik.http.services.app.loadbalancer.server.port=${FACTORY_SERVICE_PORT}"
   ```

4. **Join the factory-preview network**:
   ```yaml
   networks:
     factory-preview:
       external: true
   ```

5. **Expose a health endpoint** (default: `/health`) so Factory can confirm the environment is ready.

### Environment Variables

Factory injects these environment variables into `docker compose up`:

| Variable | Description | Example |
|----------|-------------|---------|
| `FACTORY_TASK_ID` | The task ID | `42` |
| `FACTORY_REPO` | Repository name | `acme/webapp` |
| `FACTORY_HOSTNAME` | Public hostname for routing | `task-42.preview.factory.6a.fi` |
| `FACTORY_SERVICE_PORT` | Port the service listens on | `3000` |
| `FACTORY_CREATED` | Unix timestamp of creation | `1709145600` |

### Container Labels

Factory uses labels to identify and manage containers:

| Label | Description |
|-------|-------------|
| `factory.task-id` | Task identifier |
| `factory.repo` | Repository name |
| `factory.env-type` | `test` or `preview` |
| `factory.created` | Unix timestamp of creation |
| `factory.pr-number` | PR number (preview only) |

### Automatic Label Enforcement

Factory generates a compose override file at spin-up time that enforces the correct Traefik labels (`websecure` entrypoint + `tls=true`) and Factory metadata labels on the first service in your compose file. This means:

- Even if your compose file uses the wrong entrypoint (e.g., `web` instead of `websecure`), Factory will override it
- Even if your compose file is missing `tls=true`, Factory will add it
- The `factory-preview` network is added automatically

You should still include the correct labels in your compose file as best practice, but Factory will fix common mistakes automatically.

### Cleanup Behaviour

- **Test environments** (`env-type=test`): Automatically removed when the agent task completes. Agents should also call `tear_down_test_env()` explicitly.
- **Preview environments** (`env-type=preview`): Persist until the associated PR is merged or closed, then removed by a scheduled cleanup job.

### Reference Template

See `prompts/templates/docker-compose.preview.yml` for a complete reference implementation.

## Clarification System

Agents can pause mid-task to ask the user a question. This is useful when the task description is ambiguous or the agent needs user input before proceeding.

### How It Works

1. Agent outputs a special JSON line to stdout
2. Factory detects the clarification request
3. Task is paused and status changes to `waiting_for_input`
4. Question is posted as a comment on the Plane issue
5. User replies to the Plane comment
6. Factory detects the reply and resumes the agent with the answer

### Agent API

To request clarification, output this JSON (and nothing else) then exit:

```json
{"type": "clarification_needed", "question": "What color scheme would you like for the UI?"}
```

**Important:** After outputting the JSON, the agent should stop immediately. Do not continue working.

### Example Flow

```
Agent: Building the dashboard...
Agent: {"type": "clarification_needed", "question": "Should the dashboard use dark or light mode by default?"}
[Agent exits, task paused]

[User replies on Plane: "Dark mode please"]

[Agent resumes with context]
Agent: User requested dark mode. Implementing...
```

### Clarification Context

When resumed, the agent receives the clarification history in its prompt:

```
## Previous clarifications
Q: Should the dashboard use dark or light mode by default?
A: Dark mode please

Please continue with the task using the information above.
```

### Multi-Round Clarifications

Agents can ask multiple clarification questions during a task. Each exchange is preserved in the history.

### Telegram Notifications

When an agent requests clarification, a notification is sent to Telegram:
```
❓ Agent needs input: Build dashboard
Question: Should the dashboard use dark or light mode?
Reply on Plane to continue.
```

## Inter-Agent Communication

Agents communicate via the message board. Messages are JSON objects written to stdout:

```json
{"type": "message", "to": "reviewer", "content": "Ready for review", "message_type": "handoff"}
```

Message types: `info`, `question`, `handoff`, `status`, `error`.

## System Prompt Customisation

- **Public prompts** live in `prompts/` and are version-controlled
- **Private prompts** live in `prompts/private/` (gitignored) and are appended to public prompts
- Both are loaded by `load_prompt()` in `orchestrator/src/factory/prompts.py`

To add organisation-specific instructions, create a file in `prompts/private/` with the same name as the public prompt (e.g., `prompts/private/coder.md`).
