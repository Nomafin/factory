import asyncio
import logging
import os
from pathlib import Path

from factory.config import Config
from factory.db import Database
from factory.models import TaskStatus
from factory.notifier import TelegramNotifier
from factory.plane import PlaneClient
from factory.runner import AgentRunner
from factory.workspace import RepoManager

logger = logging.getLogger(__name__)

FACTORY_ROOT = Path("/opt/factory")

PROGRESS_INTERVAL = 5  # Post progress to Plane every N output messages


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
        self.notifier: TelegramNotifier | None = None
        if config.telegram.bot_token and config.telegram.chat_id:
            self.notifier = TelegramNotifier(
                bot_token=config.telegram.bot_token,
                chat_id=config.telegram.chat_id,
            )
        self._output_counts: dict[int, int] = {}
        self._output_buffers: dict[int, list[str]] = {}

    async def recover_orphaned_tasks(self):
        """Mark any in_progress tasks as failed on startup (no agent is running for them)."""
        tasks = await self.db.list_tasks(status=TaskStatus.IN_PROGRESS)
        for task in tasks:
            logger.warning("Recovering orphaned task %d: %s", task.id, task.title)
            await self.db.update_task_status(
                task.id, TaskStatus.FAILED,
                error="Agent lost due to orchestrator restart",
            )
            await self._update_plane_state(
                task.plane_issue_id, self.config.plane.states.failed,
                "Agent lost due to orchestrator restart",
            )
            await self._notify(f"\u274c Task failed (restart): {task.title}")
        if tasks:
            logger.info("Recovered %d orphaned tasks", len(tasks))

    async def _run(self, *args: str, cwd: str | Path | None = None) -> str:
        env = os.environ.copy()
        env["GH_TOKEN"] = os.environ.get("GITHUB_TOKEN", "")
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Command {args} failed: {stderr.decode()}")
        return stdout.decode().strip()

    async def _push_and_create_pr(self, task_id: int, wt_path: Path, branch_name: str,
                                   title: str, summary: str = "") -> str:
        """Push branch and create a GitHub PR. Returns the PR URL."""
        token = os.environ.get("GITHUB_TOKEN", "")
        remote_url = await self._run("git", "remote", "get-url", "origin", cwd=wt_path)
        if token and "github.com" in remote_url and "x-access-token" not in remote_url:
            auth_url = remote_url.replace("https://github.com/", f"https://x-access-token:{token}@github.com/")
            await self._run("git", "remote", "set-url", "origin", auth_url, cwd=wt_path)

        await self._run("git", "push", "-u", "origin", branch_name, cwd=wt_path)

        body = f"Automated PR from Factory agent (task #{task_id})\n\n"
        if summary:
            body += f"## Summary\n\n{summary[:3000]}\n"

        pr_url = await self._run(
            "gh", "pr", "create",
            "--title", title,
            "--body", body,
            "--head", branch_name,
            cwd=wt_path,
        )
        return pr_url

    async def _notify(self, message: str):
        if self.notifier:
            try:
                await self.notifier.send(message)
            except Exception as e:
                logger.warning("Telegram notification failed: %s", e)

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

    async def _post_plane_comment(self, plane_issue_id: str, comment: str):
        if not self.plane or not plane_issue_id:
            return
        project_id = self.config.plane.project_id
        try:
            await self.plane.add_comment(project_id, plane_issue_id, f"<p>{comment}</p>")
        except Exception as e:
            logger.warning("Failed to post comment to Plane issue %s: %s", plane_issue_id, e)

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
            await self._notify(f"\u274c Task failed: {task.title}\nUnknown repo: {task.repo}")
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
            await self._notify(f"\u274c Task failed: {task.title}\nWorkspace setup error")
            return False

        prompt = self._build_prompt(task.title, task.description)

        await self.db.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await self._update_plane_state(
            task.plane_issue_id, self.config.plane.states.in_progress,
            f"Agent started on branch <code>{branch_name}</code>"
        )
        await self._notify(f"\U0001f527 Agent started: {task.title}\nBranch: {branch_name}")

        self._output_counts[task_id] = 0
        self._output_buffers[task_id] = []

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
            await self._notify(f"\u274c Task failed: {task.title}\nCould not start agent")
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

            # Buffer output and post to Plane periodically
            self._output_counts[task_id] = self._output_counts.get(task_id, 0) + 1
            buf = self._output_buffers.setdefault(task_id, [])
            buf.append(content[:200])

            if self._output_counts[task_id] % PROGRESS_INTERVAL == 0:
                summary = "\n".join(buf[-PROGRESS_INTERVAL:])
                self._output_buffers[task_id] = []
                loop.create_task(self._post_progress(task_id, summary))
        except RuntimeError:
            pass

    async def _post_progress(self, task_id: int, summary: str):
        task = await self.db.get_task(task_id)
        if not task or not task.plane_issue_id:
            return
        step = self._output_counts.get(task_id, 0)
        comment = f"<b>Progress (step {step})</b><br/><pre>{summary[:1000]}</pre>"
        await self._post_plane_comment(task.plane_issue_id, comment)

    def _on_agent_complete(self, task_id: int, returncode: int, output: str):
        self._output_counts.pop(task_id, None)
        self._output_buffers.pop(task_id, None)
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

        task = await self.db.get_task(task_id)
        if not task:
            return

        # Push branch and create PR
        pr_url = ""
        wt_path = FACTORY_ROOT / "worktrees" / task.branch_name.replace("/", "-")
        try:
            pr_url = await self._push_and_create_pr(task_id, wt_path, task.branch_name, task.title, summary=output)
            await self.db.update_task_fields(task_id, pr_url=pr_url)
            logger.info("Created PR for task %d: %s", task_id, pr_url)
        except Exception as e:
            logger.warning("Failed to create PR for task %d: %s", task_id, e)
            await self.db.add_log(task_id, f"PR creation failed: {e}")

        await self.db.update_task_status(task_id, TaskStatus.IN_REVIEW)

        comment = f"Agent completed. Branch: <code>{task.branch_name}</code>"
        if pr_url:
            comment += f'<br/>PR: <a href="{pr_url}">{pr_url}</a>'
        await self._update_plane_state(
            task.plane_issue_id, self.config.plane.states.in_review, comment
        )

        notify_msg = f"\u2705 Agent completed: {task.title}"
        if pr_url:
            notify_msg += f"\nPR: {pr_url}"
        await self._notify(notify_msg)

    async def _handle_failure(self, task_id: int, output: str):
        await self.db.update_task_status(task_id, TaskStatus.FAILED, error=output[:2000])
        task = await self.db.get_task(task_id)
        if task:
            await self._update_plane_state(
                task.plane_issue_id, self.config.plane.states.failed,
                f"Agent failed: {output[:500]}"
            )
            await self._notify(f"\u274c Agent failed: {task.title}\n{output[:200]}")

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
                await self._notify(f"\U0001f6d1 Task cancelled: {task.title}")
        return cancelled

    async def close(self):
        # Kill all running agents on shutdown
        for task_id in list(self.runner.get_running_agents()):
            await self.runner.cancel_agent(task_id)
        if self.plane:
            await self.plane.close()
        if self.notifier:
            await self.notifier.close()


def _slugify(text: str) -> str:
    return "-".join(text.lower().split()[:5]).replace("/", "-")[:40]
