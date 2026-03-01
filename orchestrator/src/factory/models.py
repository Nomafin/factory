from datetime import datetime
from enum import Enum
from typing import Literal

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
    plane_sequence_id: int | None = None  # Human-readable Plane issue number


class Task(BaseModel):
    id: int
    title: str
    description: str = ""
    repo: str = ""
    agent_type: str = "coder"
    status: TaskStatus = TaskStatus.QUEUED
    plane_issue_id: str = ""
    plane_sequence_id: int | None = None  # Human-readable Plane issue number (e.g., 42 for "FACT-42")
    branch_name: str = ""
    pr_url: str = ""
    preview_url: str = ""
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


# ── Review models ────────────────────────────────────────────────────────


class IssueSeverity(str, Enum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"


class ReviewIssue(BaseModel):
    """A single issue found during code review."""
    severity: IssueSeverity
    description: str
    file: str = ""
    line: int | None = None
    suggestion: str = ""


class ReviewResult(BaseModel):
    """Structured output from a code review."""
    approved: bool = False
    summary: str = ""
    issues: list[ReviewIssue] = []
    suggestions: list[str] = []

    @property
    def has_blockers(self) -> bool:
        return any(i.severity == IssueSeverity.BLOCKER for i in self.issues)

    @property
    def has_blockers_or_majors(self) -> bool:
        return any(
            i.severity in (IssueSeverity.BLOCKER, IssueSeverity.MAJOR)
            for i in self.issues
        )


class CodeReviewCreate(BaseModel):
    """Request body for starting a code_review workflow."""
    title: str
    description: str = ""
    repo: str = ""
    plane_issue_id: str = ""


# ── Workflow models ──────────────────────────────────────────────────────


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReviewIssue(BaseModel):
    """A single issue found during code review."""
    severity: Literal["blocker", "major", "minor", "nit"]
    description: str
    file: str = ""
    line: int | None = None
    suggestion: str = ""


class ReviewResult(BaseModel):
    """Structured output from a code review."""
    approved: bool = False
    summary: str = ""
    issues: list[ReviewIssue] = []
    suggestions: list[str] = []

    @property
    def has_blockers_or_majors(self) -> bool:
        return any(i.severity in ("blocker", "major") for i in self.issues)

    @property
    def blocker_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "blocker")

    @property
    def major_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "major")

    @property
    def minor_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "minor")

    @property
    def nit_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "nit")


class WorkflowStepDef(BaseModel):
    """Definition of a single step in a workflow template."""
    agent: str
    input: str = ""
    output: str = ""
    condition: str = ""
    loop_to: str = ""
    prompt_template: str = ""


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


class CodeReviewCreate(BaseModel):
    """Request body for starting a code_review workflow."""
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
    loop_to: str = ""  # Step name to loop back to
    prompt_template: str = ""
    output_data: str = ""
    iteration: int = 0
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
    iteration: int = 0
    max_iterations: int = 3
    plane_issue_id: str = ""
    error: str = ""
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    steps: list[WorkflowStep] = []


# ── Agent handoff models ─────────────────────────────────────────────────

# Recognised output types for handoff content
HANDOFF_OUTPUT_TYPES = {
    "code_diff", "review_comments", "research_notes",
    "test_results", "general", "error_report",
}


class AgentHandoff(BaseModel):
    """A structured context record passed from one agent to the next."""
    id: int
    from_task_id: int
    to_task_id: int | None = None
    workflow_id: int | None = None
    output_type: str = "general"
    content: str = ""
    summary: str = ""
    created_at: datetime


class HandoffCreate(BaseModel):
    """Request body for creating a handoff record."""
    from_task_id: int
    to_task_id: int | None = None
    workflow_id: int | None = None
    output_type: str = "general"
    content: str = ""
    summary: str = ""


# ── Message board models ─────────────────────────────────────────────────


class MessageType(str, Enum):
    INFO = "info"
    QUESTION = "question"
    HANDOFF = "handoff"
    STATUS = "status"
    ERROR = "error"


class MessageCreate(BaseModel):
    sender: str
    recipient: str | None = None  # null = broadcast
    task_id: int | None = None
    workflow_id: int | None = None
    message: str
    message_type: MessageType = MessageType.INFO
    reply_to: int | None = None  # threading support


class Message(BaseModel):
    id: int
    sender: str
    recipient: str | None = None
    task_id: int | None = None
    workflow_id: int | None = None
    message: str
    message_type: MessageType = MessageType.INFO
    reply_to: int | None = None
    created_at: datetime
