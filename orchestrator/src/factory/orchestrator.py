import asyncio
import logging
from pathlib import Path

from factory.config import Config
from factory.db import Database
from factory.models import TaskStatus
from factory.plane import PlaneClient
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
        self.plane: PlaneClient | None = None
        if config.plane.api_key and config.plane.base_url:
            self.plane = PlaneClient(
                base_url=config.plane.base_url,
                api_key=config.plane.api_key,
                workspace_slug=config.plane.workspace_slug,
            )

    async def _update_plane_state(self, plane_issue_id: str, state_id: str, comment: str = ""):
        if not self.plane or not plane_issue_id or not state_id:
            return
        project_id = self.config.plane.project_id
        try:
            await self.plane.update_issue_state(project_id, plane_issue_id, state_id)
            if comment:
                await self.plane.add_comment(project_id, plane_issue_id, f"<p>{comment}</p>")
        except Exception as e:
            logger.warning("Failed to update Plane issue %s: %s", plane_issue_id, e)

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
            await self._update_plane_state(
                task.plane_issue_id, self.config.plane.states.failed,
                f"Failed: Unknown repo '{task.repo}'"
            )
            return False

        template = self.config.agent_templates.get(task.agent_type)
        if not template:
            await self.db.update_task_status(task_id, TaskStatus.FAILED, error=f"Unknown agent type: {task.agent_type}")
            await self._update_plane_state(
                task.plane_issue_id, self.config.plane.states.failed,
                f"Failed: Unknown agent type '{task.agent_type}'"
            )
            return False

        try:
            await self.repo_manager.ensure_repo(task.repo, repo_config.url)
            branch_name = f"agent/task-{task.id}-{_slugify(task.title)}"
            wt_path = await self.repo_manager.create_worktree(task.repo, branch_name)
            await self.db.update_task_fields(task_id, branch_name=branch_name)
        except Exception as e:
            logger.exception("Failed to set up workspace for task %d", task_id)
            await self.db.update_task_status(task_id, TaskStatus.FAILED, error=str(e))
            await self._update_plane_state(
                task.plane_issue_id, self.config.plane.states.failed,
                f"Failed to set up workspace: {e}"
            )
            return False

        prompt = self._build_prompt(task.title, task.description)

        await self.db.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await self._update_plane_state(
            task.plane_issue_id, self.config.plane.states.in_progress,
            f"Agent started on branch <code>{branch_name}</code>"
        )

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
            await self._update_plane_state(
                task.plane_issue_id, self.config.plane.states.failed,
                "Failed to start agent process"
            )
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
                loop.create_task(self._handle_failure(task_id, output))
        except RuntimeError:
            pass

    async def _handle_success(self, task_id: int, output: str):
        await self.db.add_log(task_id, f"Agent completed successfully:\n{output[:2000]}")
        await self.db.update_task_status(task_id, TaskStatus.IN_REVIEW)
        task = await self.db.get_task(task_id)
        if task:
            await self._update_plane_state(
                task.plane_issue_id, self.config.plane.states.in_review,
                f"Agent completed. Branch: <code>{task.branch_name}</code>"
            )

    async def _handle_failure(self, task_id: int, output: str):
        await self.db.update_task_status(task_id, TaskStatus.FAILED, error=output[:2000])
        task = await self.db.get_task(task_id)
        if task:
            await self._update_plane_state(
                task.plane_issue_id, self.config.plane.states.failed,
                f"Agent failed: {output[:500]}"
            )

    async def cancel_task(self, task_id: int) -> bool:
        cancelled = await self.runner.cancel_agent(task_id)
        if cancelled:
            await self.db.update_task_status(task_id, TaskStatus.CANCELLED)
            task = await self.db.get_task(task_id)
            if task:
                await self._update_plane_state(
                    task.plane_issue_id, self.config.plane.states.cancelled,
                    "Task cancelled"
                )
        return cancelled

    async def close(self):
        if self.plane:
            await self.plane.close()


def _slugify(text: str) -> str:
    return "-".join(text.lower().split()[:5]).replace("/", "-")[:40]
