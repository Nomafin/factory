from fastapi import APIRouter, Depends, HTTPException

from factory.db import Database
from factory.deps import get_db
from factory.models import AgentInfo, Task, TaskCreate, TaskStatus

router = APIRouter(prefix="/api")

# In-memory agent tracking (will be managed by the agent runner later)
_running_agents: dict[int, AgentInfo] = {}


@router.post("/tasks", response_model=Task, status_code=201)
async def create_task(body: TaskCreate, db: Database = Depends(get_db)):
    return await db.create_task(body)


@router.get("/tasks", response_model=list[Task])
async def list_tasks(status: TaskStatus | None = None, db: Database = Depends(get_db)):
    return await db.list_tasks(status=status)


@router.get("/tasks/{task_id}", response_model=Task)
async def get_task(task_id: int, db: Database = Depends(get_db)):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/tasks/{task_id}/cancel", response_model=Task)
async def cancel_task(task_id: int, db: Database = Depends(get_db)):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return await db.update_task_status(task_id, TaskStatus.CANCELLED)


@router.get("/agents", response_model=list[AgentInfo])
async def list_agents():
    return list(_running_agents.values())
