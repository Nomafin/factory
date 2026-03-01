import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from factory.config import Config
from factory.db import Database
from factory.deps import get_config, get_db, get_orchestrator
from factory.docker_toolkit import PREVIEW_DOMAIN
from factory.models import (
    AgentHandoff, AgentInfo, CodeReviewCreate, HandoffCreate,
    Message, MessageCreate, MessageType,
    Task, TaskCreate, TaskStatus, Workflow, WorkflowCreate, WorkflowStatus,
)
from factory.orchestrator import Orchestrator
from factory.plane import parse_webhook_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


@router.get("/settings")
async def get_settings(config: Config = Depends(get_config)):
    """Return public configuration for the dashboard frontend."""
    plane = config.plane
    return {
        "plane_base_url": plane.base_url.rstrip("/") if plane.base_url else "",
        "plane_workspace_slug": plane.workspace_slug,
        "plane_project_id": plane.project_id,
    }


@router.post("/tasks", response_model=Task, status_code=201)
async def create_task(
    body: TaskCreate,
    auto_run: bool = Query(False),
    db: Database = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
):
    if not body.repo:
        body.repo = orch.config.plane.default_repo

    # Create a corresponding Plane issue if no plane_issue_id provided
    if not body.plane_issue_id and orch.plane:
        try:
            issue_id = await orch.plane.create_issue(
                project_id=orch.config.plane.project_id,
                title=body.title,
                description=body.description or "",
                state_id=orch.config.plane.states.queued,
            )
            body.plane_issue_id = issue_id
        except Exception as e:
            logger.warning("Failed to create Plane issue: %s", e)

    task = await db.create_task(body)
    if auto_run:
        try:
            await orch.process_task(task.id)
        except Exception:
            logger.exception("Failed to auto-run task %d", task.id)
        task = await db.get_task(task.id) or task
    return task


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


class ResumeInput(BaseModel):
    response: str


@router.post("/tasks/{task_id}/resume", response_model=Task)
async def resume_task(
    task_id: int,
    body: ResumeInput,
    db: Database = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != TaskStatus.WAITING_FOR_INPUT:
        raise HTTPException(status_code=400, detail=f"Task is {task.status}, must be waiting_for_input")
    success = await orch.resume_task(task_id, body.response)
    if not success:
        raise HTTPException(status_code=503, detail="No agent slots available or resume failed")
    return await db.get_task(task_id)


# ── Workflow endpoints ────────────────────────────────────────────────────


@router.post("/workflows", response_model=Workflow, status_code=201)
async def create_workflow(
    body: WorkflowCreate,
    db: Database = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
):
    if not body.repo:
        body.repo = orch.config.plane.default_repo

    wf_config = orch.config.workflows.get(body.workflow_name)
    if not wf_config:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown workflow: '{body.workflow_name}'. "
                   f"Available: {list(orch.config.workflows.keys())}",
        )

    workflow = await orch.start_workflow(
        workflow_name=body.workflow_name,
        title=body.title,
        description=body.description,
        repo=body.repo,
        plane_issue_id=body.plane_issue_id,
    )
    if not workflow:
        raise HTTPException(status_code=503, detail="Failed to start workflow")
    return workflow


@router.get("/workflows", response_model=list[Workflow])
async def list_workflows(
    status: WorkflowStatus | None = None,
    db: Database = Depends(get_db),
):
    return await db.list_workflows(status=status)


@router.get("/workflows/{workflow_id}", response_model=Workflow)
async def get_workflow(workflow_id: int, db: Database = Depends(get_db)):
    workflow = await db.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


@router.post("/workflows/{workflow_id}/cancel", response_model=Workflow)
async def cancel_workflow(
    workflow_id: int,
    db: Database = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
):
    workflow = await db.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if workflow.status != WorkflowStatus.RUNNING:
        raise HTTPException(
            status_code=400,
            detail=f"Workflow is {workflow.status.value}, must be running",
        )
    success = await orch.cancel_workflow(workflow_id)
    if not success:
        raise HTTPException(status_code=503, detail="Failed to cancel workflow")
    return await db.get_workflow(workflow_id)


@router.post("/workflows/code_review", response_model=Workflow, status_code=201)
async def create_code_review_workflow(
    body: CodeReviewCreate,
    db: Database = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
):
    """Start a code_review workflow with coder-reviewer collaboration.

    This is a convenience endpoint that starts the built-in code_review workflow.
    A single API call kicks off the coder, auto-triggers review, auto-triggers
    revision if needed, and produces a final PR.
    """
    if not body.repo:
        body.repo = orch.config.plane.default_repo

    wf_config = orch.config.workflows.get("code_review")
    if not wf_config:
        raise HTTPException(
            status_code=400,
            detail="code_review workflow is not configured. "
                   "Add it to the workflows section of config.yml.",
        )

    workflow = await orch.start_workflow(
        workflow_name="code_review",
        title=body.title,
        description=body.description,
        repo=body.repo,
        plane_issue_id=body.plane_issue_id,
    )
    if not workflow:
        raise HTTPException(status_code=503, detail="Failed to start code_review workflow")
    return workflow


# ── Handoff endpoints ──────────────────────────────────────────────────


@router.get("/tasks/{task_id}/handoffs", response_model=list[AgentHandoff])
async def get_task_handoffs(
    task_id: int,
    direction: str = Query("to", description="'to' for inputs, 'from' for outputs"),
    db: Database = Depends(get_db),
):
    """Get handoffs linked to a task.

    - direction=to  → handoffs that feed *into* this task (inputs)
    - direction=from → handoffs produced *by* this task (outputs)
    """
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if direction == "from":
        return await db.get_handoffs_from_task(task_id)
    return await db.get_handoffs_for_task(task_id)


@router.get("/workflows/{workflow_id}/handoffs", response_model=list[AgentHandoff])
async def get_workflow_handoffs(
    workflow_id: int,
    db: Database = Depends(get_db),
):
    """Get all handoffs within a workflow."""
    workflow = await db.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return await db.get_handoffs_for_workflow(workflow_id)


@router.post("/handoffs", response_model=AgentHandoff, status_code=201)
async def create_handoff(
    body: HandoffCreate,
    db: Database = Depends(get_db),
):
    """Manually create a handoff record (e.g. for external integrations)."""
    task = await db.get_task(body.from_task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Source task {body.from_task_id} not found")
    if body.to_task_id:
        to_task = await db.get_task(body.to_task_id)
        if not to_task:
            raise HTTPException(status_code=404, detail=f"Target task {body.to_task_id} not found")
    return await db.create_handoff(body)


@router.get("/handoffs/{handoff_id}", response_model=AgentHandoff)
async def get_handoff(handoff_id: int, db: Database = Depends(get_db)):
    handoff = await db.get_handoff(handoff_id)
    if not handoff:
        raise HTTPException(status_code=404, detail="Handoff not found")
    return handoff


# ── Message board endpoints ────────────────────────────────────────────


# Global SSE subscriber list for real-time message streaming
_message_subscribers: list[asyncio.Queue] = []


@router.post("/messages", response_model=Message, status_code=201)
async def create_message(
    body: MessageCreate,
    db: Database = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
):
    message = await db.create_message(body)

    # Broadcast to SSE subscribers
    msg_data = message.model_dump(mode="json")
    msg_data["created_at"] = message.created_at.isoformat()
    for queue in _message_subscribers:
        try:
            queue.put_nowait(msg_data)
        except asyncio.QueueFull:
            pass

    # Forward to Telegram if configured
    await orch.forward_message_to_telegram(message)

    return message


@router.get("/messages", response_model=list[Message])
async def list_messages(
    task_id: int | None = Query(None),
    workflow_id: int | None = Query(None),
    sender: str | None = Query(None),
    message_type: str | None = Query(None),
    since: str | None = Query(None),
    before: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    search: str | None = Query(None),
    db: Database = Depends(get_db),
):
    if search:
        return await db.search_messages(search, limit=limit)
    return await db.list_messages(
        task_id=task_id,
        workflow_id=workflow_id,
        sender=sender,
        message_type=message_type,
        since=since,
        before=before,
        limit=limit,
        offset=offset,
    )


@router.get("/messages/{message_id}", response_model=Message)
async def get_message(message_id: int, db: Database = Depends(get_db)):
    message = await db.get_message(message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    return message


@router.get("/messages/{message_id}/thread", response_model=list[Message])
async def get_message_thread(message_id: int, db: Database = Depends(get_db)):
    messages = await db.get_thread(message_id)
    if not messages:
        raise HTTPException(status_code=404, detail="Message not found")
    return messages


@router.get("/messages/stream/sse")
async def message_stream(request: Request):
    """SSE endpoint for real-time message updates."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _message_subscribers.append(queue)

    async def event_generator():
        try:
            # Send initial keepalive
            yield "event: connected\ndata: {}\n\n"
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break
                try:
                    msg_data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"event: message\ndata: {json.dumps(msg_data)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive ping
                    yield "event: ping\ndata: {}\n\n"
        finally:
            _message_subscribers.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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


# ── Preview environment endpoints ──────────────────────────────────────


def _list_factory_containers() -> list[dict]:
    """Query Docker for containers with factory.task-id label.

    Returns a list of dicts with container info including labels,
    status, ports, and calculated preview URL.
    """
    try:
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", "label=factory.task-id",
                "--format", "{{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Labels}}\t{{.CreatedAt}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("Failed to query Docker containers: %s", exc)
        return []

    if result.returncode != 0:
        logger.warning(
            "docker ps failed (rc=%d): %s",
            result.returncode, result.stderr.strip(),
        )
        return []

    containers = []
    now = time.time()
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        container_id, name, status, ports, labels_str, created_at = parts

        # Parse labels into dict
        labels = {}
        for label in labels_str.split(","):
            label = label.strip()
            if "=" in label:
                k, v = label.split("=", 1)
                labels[k] = v

        task_id = labels.get("factory.task-id", "")
        env_type = labels.get("factory.env-type", "unknown")
        repo = labels.get("factory.repo", "")
        created_ts = labels.get("factory.created", "")

        # Calculate age
        age_seconds = 0
        if created_ts:
            try:
                age_seconds = int(now - int(created_ts))
            except ValueError:
                pass

        # Determine preview URL from hostname label or task/PR info
        hostname = labels.get("factory.hostname", "")
        pr_number = labels.get("factory.pr-number", "")
        if not hostname:
            if pr_number:
                hostname = f"pr-{pr_number}.{PREVIEW_DOMAIN}"
            elif task_id:
                hostname = f"task-{task_id}.{PREVIEW_DOMAIN}"
        preview_url = f"https://{hostname}" if hostname else ""

        # Determine health from status string
        health = "unknown"
        status_lower = status.lower()
        if "up" in status_lower:
            if "(healthy)" in status_lower:
                health = "healthy"
            elif "(health: starting)" in status_lower or "starting" in status_lower:
                health = "starting"
            elif "(unhealthy)" in status_lower:
                health = "unhealthy"
            else:
                health = "running"
        elif "exited" in status_lower or "dead" in status_lower:
            health = "stopped"
        elif "created" in status_lower:
            health = "created"

        containers.append({
            "container_id": container_id,
            "name": name,
            "task_id": task_id,
            "env_type": env_type,
            "repo": repo,
            "url": preview_url,
            "status": status,
            "health": health,
            "ports": ports,
            "created_at": created_at,
            "created_ts": created_ts,
            "age_seconds": age_seconds,
        })

    return containers


@router.get("/preview-environments")
async def list_preview_environments():
    """List all Factory Docker containers (test and preview environments)."""
    loop = asyncio.get_event_loop()
    containers = await loop.run_in_executor(None, _list_factory_containers)
    return containers


def _remove_container(container_id: str) -> dict:
    """Stop and remove a Docker container by ID.

    Returns a dict with status and optional error message.
    """
    try:
        # Stop the container
        stop_result = subprocess.run(
            ["docker", "stop", container_id],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if stop_result.returncode != 0:
            logger.warning(
                "docker stop failed for %s: %s",
                container_id, stop_result.stderr.strip(),
            )

        # Remove the container
        rm_result = subprocess.run(
            ["docker", "rm", container_id],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if rm_result.returncode != 0:
            return {
                "status": "error",
                "error": f"Failed to remove container: {rm_result.stderr.strip()}",
            }

        return {"status": "removed", "container_id": container_id}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Docker command timed out"}
    except FileNotFoundError:
        return {"status": "error", "error": "Docker not available"}


@router.delete("/preview-environments/{container_id}")
async def delete_preview_environment(container_id: str):
    """Stop and remove a Factory Docker container."""
    # Validate the container exists and is a factory container
    loop = asyncio.get_event_loop()
    containers = await loop.run_in_executor(None, _list_factory_containers)

    matching = [c for c in containers if c["container_id"].startswith(container_id)]
    if not matching:
        raise HTTPException(
            status_code=404,
            detail=f"Container {container_id} not found or not a Factory container",
        )

    result = await loop.run_in_executor(None, _remove_container, container_id)
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@router.post("/webhooks/plane")
async def plane_webhook(request: Request, db: Database = Depends(get_db), orch: Orchestrator = Depends(get_orchestrator)):
    payload = await request.json()

    # Handle comment events for tasks waiting for input
    event_type = payload.get("event", "")
    if event_type == "comment":
        return await _handle_plane_comment_webhook(payload, db, orch)

    event = parse_webhook_event(payload)

    if event.event_type != "issue":
        return {"status": "ignored"}

    if event.state_name == "Queued" and event.action in ("create", "update"):
        existing = await db.find_by_plane_issue_id(event.issue_id) if event.issue_id else None
        if existing and existing.status in (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS):
            return {"status": "already_exists", "task_id": existing.id}

        # Check if a previous task for this issue has a PR (revision detection)
        previous_with_pr = await db.find_previous_task_with_pr(event.issue_id) if event.issue_id else None
        is_revision = previous_with_pr is not None

        repo = event.repo or orch.config.plane.default_repo
        task = await db.create_task(TaskCreate(
            title=event.issue_title,
            description=event.description,
            repo=repo,
            agent_type=event.agent_type,
            plane_issue_id=event.issue_id,
        ))
        await orch.process_task(task.id)
        status = "revision_task_created" if is_revision else "task_created"
        return {"status": status, "task_id": task.id}

    if event.state_name == "Cancelled":
        tasks = await db.list_tasks(status=TaskStatus.IN_PROGRESS)
        for task in tasks:
            if task.plane_issue_id == event.issue_id:
                await orch.cancel_task(task.id)
                return {"status": "cancelled", "task_id": task.id}

    return {"status": "ok"}


async def _handle_plane_comment_webhook(
    payload: dict, db: Database, orch: Orchestrator
) -> dict:
    """Handle a Plane comment webhook to resume waiting tasks."""
    data = payload.get("data", {})
    issue_id = data.get("issue", "")
    if not issue_id:
        return {"status": "ignored", "reason": "no issue id"}

    task = await db.find_by_plane_issue_id(issue_id)
    if not task or task.status != TaskStatus.WAITING_FOR_INPUT:
        return {"status": "ignored", "reason": "no waiting task for issue"}

    comment_html = data.get("comment_html", "")
    # Skip our own system comments
    system_markers = ["Agent needs clarification", "Progress (step", "Agent failed:", "Agent resumed"]
    if any(marker in comment_html for marker in system_markers):
        return {"status": "ignored", "reason": "system comment"}
    response_text = re.sub(r"<[^>]+>", "", comment_html).strip()
    if not response_text:
        return {"status": "ignored", "reason": "empty comment"}

    logger.info("Plane comment webhook resuming task %d", task.id)
    await db.add_log(task.id, f"User responded (via webhook): {response_text[:1000]}")
    success = await orch.resume_task(task.id, response_text)
    if success:
        return {"status": "resumed", "task_id": task.id}
    return {"status": "resume_failed", "task_id": task.id}


def _verify_github_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/webhooks/github")
async def github_webhook(request: Request):
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    body = await request.body()
    if not _verify_github_signature(body, signature, secret):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()

    # Only deploy on pushes to main
    ref = payload.get("ref", "")
    if ref != "refs/heads/main":
        return {"status": "ignored", "reason": f"not main branch: {ref}"}

    # Use systemd-run to spawn deploy.sh in its own scope (escapes the
    # factory-orchestrator cgroup so it survives service restart)
    subprocess.Popen(
        ["systemd-run", "--scope", "--unit=factory-deploy", "/opt/factory/deploy.sh"],
        start_new_session=True,
    )
    logger.info("Deploy triggered by push to main (spawned deploy.sh)")

    return {"status": "deploy_started"}
