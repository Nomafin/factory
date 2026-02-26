from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class TaskStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    repo: str = ""
    agent_type: str = "coder"
    plane_issue_id: str = ""


class Task(BaseModel):
    id: int
    title: str
    description: str = ""
    repo: str = ""
    agent_type: str = "coder"
    status: TaskStatus = TaskStatus.QUEUED
    plane_issue_id: str = ""
    branch_name: str = ""
    pr_url: str = ""
    error: str = ""
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class AgentInfo(BaseModel):
    task_id: int
    task_title: str
    agent_type: str
    repo: str
    status: str
    started_at: datetime | None = None
    pid: int | None = None
