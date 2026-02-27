from pathlib import Path

import yaml
from pydantic import BaseModel


class TelegramConfig(BaseModel):
    bot_token: str = ""
    chat_id: str = ""


class PlaneStatesConfig(BaseModel):
    queued: str = ""
    in_progress: str = ""
    waiting_for_input: str = ""
    in_review: str = ""
    done: str = ""
    failed: str = ""
    cancelled: str = ""


class PlaneConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    workspace_slug: str = "factory"
    project_id: str = ""
    default_repo: str = ""
    states: PlaneStatesConfig = PlaneStatesConfig()


class OrchestratorConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8100
    auth_token: str = ""


class RepoConfig(BaseModel):
    url: str
    default_agent: str = "coder"


class AgentTemplateConfig(BaseModel):
    system_prompt_file: str = ""
    allowed_tools: list[str] = []
    timeout_minutes: int = 30


class SurrealDBConfig(BaseModel):
    url: str = ""
    user: str = ""
    password: str = ""


class WorkflowStepConfig(BaseModel):
    agent: str
    input: str = ""
    output: str = ""
    condition: str = ""
    loop_to: str = ""  # Name of step to loop back to (for iterative workflows)


class WorkflowConfig(BaseModel):
    steps: list[WorkflowStepConfig]
    max_iterations: int = 3  # Maximum review-revision iterations


class Config(BaseModel):
    max_concurrent_agents: int = 3
    agent_timeout_minutes: int = 30
    plane: PlaneConfig = PlaneConfig()
    orchestrator: OrchestratorConfig = OrchestratorConfig()
    repos: dict[str, RepoConfig] = {}
    telegram: TelegramConfig = TelegramConfig()
    agent_templates: dict[str, AgentTemplateConfig] = {}
    surrealdb: SurrealDBConfig = SurrealDBConfig()
    workflows: dict[str, WorkflowConfig] = {}


def load_config(path: Path) -> Config:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return Config(**data)
