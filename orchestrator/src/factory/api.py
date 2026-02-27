import hashlib
import hmac
import logging
import os
import re
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from factory.db import Database
from factory.deps import get_db, get_orchestrator
from factory.models import AgentInfo, CodeReviewCreate, Task, TaskCreate, TaskStatus, Workflow, WorkflowCreate, WorkflowStatus
from factory.orchestrator import Orchestrator
from factory.plane import parse_webhook_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


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
        repo = event.repo or orch.config.plane.default_repo
        task = await db.create_task(TaskCreate(
            title=event.issue_title,
            description=event.description,
            repo=repo,
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
