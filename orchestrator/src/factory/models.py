from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class TaskStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    WAITING_FOR_INPUT = "waiting_for_input"
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
    clarification_context: str = ""
    workflow_id: int | None = None
    workflow_step: int | None = None
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


# ── Workflow models ──────────────────────────────────────────────────────


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowStepDef(BaseModel):
    """Definition of a single step in a workflow template."""
    agent: str
    input: str = ""
    output: str = ""
    condition: str = ""


class WorkflowDef(BaseModel):
    """Definition of a workflow template (from config or API)."""
    name: str
    steps: list[WorkflowStepDef]


class WorkflowCreate(BaseModel):
    """Request body for starting a new workflow."""
    workflow_name: str
    title: str
    description: str = ""
    repo: str = ""
    plane_issue_id: str = ""


class WorkflowStep(BaseModel):
    """Runtime state of a single workflow step."""
    id: int
    workflow_id: int
    step_index: int
    agent_type: str
    task_id: int | None = None
    status: str = "pending"  # pending, running, completed, skipped, failed
    input_key: str = ""
    output_key: str = ""
    condition: str = ""
    output_data: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None


class Workflow(BaseModel):
    """Runtime state of a workflow execution."""
    id: int
    name: str
    title: str
    description: str = ""
    repo: str = ""
    status: WorkflowStatus = WorkflowStatus.PENDING
    current_step: int = 0
    plane_issue_id: str = ""
    error: str = ""
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    steps: list[WorkflowStep] = []
