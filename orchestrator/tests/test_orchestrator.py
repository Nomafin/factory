from pathlib import Path
from unittest.mock import AsyncMock, patch

from factory.config import Config, RepoConfig, AgentTemplateConfig
from factory.db import Database
from factory.models import TaskCreate, TaskStatus
from factory.orchestrator import Orchestrator


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_orchestrator_process_task(MockRunner, MockRepoMgr):
    db = Database(":memory:")
    await db.initialize()

    config = Config(
        repos={"myapp": RepoConfig(url="git@github.com:user/myapp.git")},
        agent_templates={"coder": AgentTemplateConfig(
            system_prompt_file="prompts/coder.md",
            allowed_tools=["Read", "Edit", "Bash"],
        )},
    )

    mock_repo_mgr = MockRepoMgr.return_value
    mock_repo_mgr.ensure_repo = AsyncMock(return_value=Path("/tmp/repos/myapp"))
    mock_repo_mgr.create_worktree = AsyncMock(return_value=Path("/tmp/worktrees/test"))

    mock_runner = MockRunner.return_value
    mock_runner.can_accept_task = True
    mock_runner.start_agent = AsyncMock(return_value=True)

    orch = Orchestrator(db=db, config=config)
    orch.repo_manager = mock_repo_mgr
    orch.runner = mock_runner

    task = await db.create_task(TaskCreate(
        title="Fix bug",
        repo="myapp",
        agent_type="coder",
    ))

    await orch.process_task(task.id)

    mock_repo_mgr.ensure_repo.assert_called_once()
    mock_repo_mgr.create_worktree.assert_called_once()
    mock_runner.start_agent.assert_called_once()

    updated = await db.get_task(task.id)
    assert updated.status == TaskStatus.IN_PROGRESS

    await db.close()


@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_orchestrator_rejects_when_full(MockRunner, MockRepoMgr):
    db = Database(":memory:")
    await db.initialize()

    config = Config()

    mock_runner = MockRunner.return_value
    mock_runner.can_accept_task = False

    orch = Orchestrator(db=db, config=config)
    orch.runner = mock_runner

    task = await db.create_task(TaskCreate(title="Task", repo="myapp", agent_type="coder"))

    result = await orch.process_task(task.id)
    assert result is False

    await db.close()
