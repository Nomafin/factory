import hashlib
import hmac
import logging
import os
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from factory.db import Database
from factory.deps import get_db, get_orchestrator
from factory.models import AgentInfo, Task, TaskCreate, TaskStatus
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
