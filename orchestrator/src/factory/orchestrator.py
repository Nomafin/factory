import asyncio
import logging
from pathlib import Path

from factory.config import Config
from factory.db import Database
from factory.models import TaskStatus
from factory.runner import AgentRunner
from factory.workspace import RepoManager

logger = logging.getLogger(__name__)

FACTORY_ROOT = Path("/opt/factory")


class Orchestrator:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.repo_manager = RepoManager(
            repos_dir=FACTORY_ROOT / "repos",
            worktrees_dir=FACTORY_ROOT / "worktrees",
        )
        self.runner = AgentRunner(max_concurrent=config.max_concurrent_agents)

    async def process_task(self, task_id: int) -> bool:
        if not self.runner.can_accept_task:
            logger.warning("Cannot accept task %d: no available slots", task_id)
            return False

        task = await self.db.get_task(task_id)
        if not task:
            logger.error("Task %d not found", task_id)
            return False

        repo_config = self.config.repos.get(task.repo)
        if not repo_config:
            await self.db.update_task_status(task_id, TaskStatus.FAILED, error=f"Unknown repo: {task.repo}")
            return False

        template = self.config.agent_templates.get(task.agent_type)
        if not template:
            await self.db.update_task_status(task_id, TaskStatus.FAILED, error=f"Unknown agent type: {task.agent_type}")
            return False

        try:
            await self.repo_manager.ensure_repo(task.repo, repo_config.url)
            branch_name = f"agent/task-{task.id}-{_slugify(task.title)}"
            wt_path = await self.repo_manager.create_worktree(task.repo, branch_name)
            await self.db.update_task_fields(task_id, branch_name=branch_name)
        except Exception as e:
            logger.exception("Failed to set up workspace for task %d", task_id)
            await self.db.update_task_status(task_id, TaskStatus.FAILED, error=str(e))
            return False

        prompt = self._build_prompt(task.title, task.description)

        await self.db.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        started = await self.runner.start_agent(
            task_id=task_id,
            prompt=prompt,
            workdir=wt_path,
            allowed_tools=template.allowed_tools,
            on_output=self._on_agent_output,
            on_complete=self._on_agent_complete,
        )

        if not started:
            await self.db.update_task_status(task_id, TaskStatus.FAILED, error="Failed to start agent")
            return False

        return True

    def _build_prompt(self, title: str, description: str) -> str:
        parts = [f"Task: {title}"]
        if description:
            parts.append(f"\n{description}")
        parts.append("\nWhen done, commit your changes with a descriptive message.")
        return "\n".join(parts)

    def _on_agent_output(self, task_id: int, content: str):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.db.add_log(task_id, content[:1000]))
        except RuntimeError:
            pass

    def _on_agent_complete(self, task_id: int, returncode: int, output: str):
        try:
            loop = asyncio.get_running_loop()
            if returncode == 0:
                loop.create_task(self._handle_success(task_id, output))
            else:
                loop.create_task(
                    self.db.update_task_status(task_id, TaskStatus.FAILED, error=output[:2000])
                )
        except RuntimeError:
            pass

    async def _handle_success(self, task_id: int, output: str):
        await self.db.add_log(task_id, f"Agent completed successfully:\n{output[:2000]}")
        await self.db.update_task_status(task_id, TaskStatus.IN_REVIEW)

    async def cancel_task(self, task_id: int) -> bool:
        cancelled = await self.runner.cancel_agent(task_id)
        if cancelled:
            await self.db.update_task_status(task_id, TaskStatus.CANCELLED)
        return cancelled


def _slugify(text: str) -> str:
    return "-".join(text.lower().split()[:5]).replace("/", "-")[:40]
