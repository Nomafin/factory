import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from factory.config import Config
from factory.db import Database
from factory.memory import AgentMemory
from factory.models import TaskCreate, TaskStatus, Workflow, WorkflowStatus
from factory.notifier import TelegramNotifier
from factory.plane import PlaneClient
from factory.prompts import load_prompt
from factory.runner import AgentRunner
from factory.workspace import RepoManager

logger = logging.getLogger(__name__)

FACTORY_ROOT = Path("/opt/factory")

PROGRESS_INTERVAL = 5  # Post progress to Plane every N output messages
POLL_INTERVAL = 30  # Seconds between polling for responses on waiting tasks


class Orchestrator:
    def __init__(self, db: Database, config: Config, memory: AgentMemory | None = None, base_dir: Path = FACTORY_ROOT):
        self.db = db
        self.config = config
        self.memory = memory
        self.base_dir = base_dir
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
        self._polling_task: asyncio.Task | None = None

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

        memories = []
        if self.memory:
            try:
                memories = await self.memory.recall(
                    repo=task.repo, query=f"{task.title} {task.description}"
                )
            except Exception as e:
                logger.warning("Memory recall failed for task %d: %s", task_id, e)

        try:
            prompt = self._build_prompt(task.title, task.description, memories=memories)
            system_prompt = load_prompt(template.system_prompt_file, self.base_dir)

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
                system_prompt=system_prompt,
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
        except Exception as e:
            logger.exception("Failed to launch agent for task %d", task_id)
            try:
                await self.db.update_task_status(
                    task_id, TaskStatus.FAILED, error=f"Agent launch error: {e}"
                )
                await self._update_plane_state(
                    task.plane_issue_id, self.config.plane.states.failed,
                    f"Agent launch error: {e}"
                )
                await self._notify(f"\u274c Task failed: {task.title}\nAgent launch error")
            except Exception:
                logger.exception("Failed to update status after agent launch error for task %d", task_id)
            return False

    def _build_prompt(
        self,
        title: str,
        description: str,
        memories: list[dict] | None = None,
        clarification_history: list[dict] | None = None,
    ) -> str:
        parts = [f"Task: {title}"]
        if description:
            parts.append(f"\n{description}")

        if memories:
            lines = ["\n## Relevant past experience"]
            for m in memories[:5]:
                outcome = m.get("outcome", "?")
                m_title = m.get("title", "")
                summary = m.get("summary", "")[:150]
                lines.append(f"- [{outcome}] Task \"{m_title}\": {summary}")
            parts.append("\n".join(lines))

        if clarification_history:
            lines = ["\n## Previous clarifications"]
            for exchange in clarification_history:
                lines.append(f"Q: {exchange.get('question', '')}")
                lines.append(f"A: {exchange.get('response', '')}")
            parts.append("\n".join(lines))
            parts.append("\nPlease continue with the task using the information above.")

        parts.append("""
If you need clarification from the user before proceeding, output ONLY this JSON (no other text):
{"type": "clarification_needed", "question": "Your question here"}
This will pause the task and post your question for the user to answer.

When done, commit your changes with a descriptive message.

After committing, end your response with a summary in exactly this format:

## Summary
What was done and why.

## Changes
- Bullet points of key changes made.

This summary will be used as the PR description, so write it for a human reviewer.""")
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

    @staticmethod
    def _extract_clarification(output: str) -> str | None:
        """Extract a clarification question from agent output, if present."""
        try:
            pattern = r'\{\s*"type"\s*:\s*"clarification_needed"\s*,\s*"question"\s*:\s*"([^"]*)"\s*\}'
            match = re.search(pattern, output)
            if match:
                return match.group(1)
        except Exception:
            pass
        return None

    async def _handle_clarification(self, task_id: int, question: str):
        """Handle an agent requesting clarification from the user."""
        task = await self.db.get_task(task_id)
        if not task:
            return

        asked_at = datetime.now(timezone.utc).isoformat()

        # Build clarification context (preserve history for multi-round)
        context: dict = {}
        if task.clarification_context:
            try:
                context = json.loads(task.clarification_context)
            except json.JSONDecodeError:
                pass
        history = context.get("history", [])
        history.append({"question": question, "asked_at": asked_at})
        context["history"] = history
        context["pending_question"] = question
        context["asked_at"] = asked_at

        await self.db.update_task_fields(task_id, clarification_context=json.dumps(context))
        await self.db.update_task_status(task_id, TaskStatus.WAITING_FOR_INPUT)

        # Post question to Plane
        comment_html = (
            f"<p><b>\u2753 Agent needs clarification:</b></p>"
            f"<p>{question}</p>"
            f"<p><i>Reply to this issue to provide your answer.</i></p>"
        )
        await self._post_plane_comment(task.plane_issue_id, comment_html)

        # Update Plane issue state if configured
        if self.config.plane.states.waiting_for_input:
            await self._update_plane_state(
                task.plane_issue_id, self.config.plane.states.waiting_for_input,
            )

        await self._notify(
            f"\u2753 Agent needs input: {task.title}\n"
            f"Question: {question}\n"
            f"Reply on Plane to continue."
        )
        logger.info("Task %d waiting for clarification: %s", task_id, question)

    async def _handle_success(self, task_id: int, output: str):
        # Check if the agent is requesting clarification instead of completing
        question = self._extract_clarification(output)
        if question:
            await self.db.add_log(task_id, f"Agent requesting clarification: {question}")
            await self._handle_clarification(task_id, question)
            return

        await self.db.add_log(task_id, f"Agent completed successfully:\n{output[:2000]}")

        task = await self.db.get_task(task_id)
        if not task:
            return

        if self.memory:
            try:
                await self.memory.store(
                    task_id=task_id, repo=task.repo, agent_type=task.agent_type,
                    title=task.title, description=task.description,
                    outcome="success", summary=output[:2000],
                )
            except Exception as e:
                logger.warning("Failed to store success memory for task %d: %s", task_id, e)

        # If this task is part of a workflow, advance to next step
        if task.workflow_id is not None and task.workflow_step is not None:
            await self._advance_workflow(task.workflow_id, task.workflow_step, output)
            return

        # Standalone task: push branch and create PR
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
            if self.memory:
                try:
                    await self.memory.store(
                        task_id=task_id, repo=task.repo, agent_type=task.agent_type,
                        title=task.title, description=task.description,
                        outcome="failed", summary=output[:2000],
                        error=output[:500],
                    )
                except Exception as e:
                    logger.warning("Failed to store failure memory for task %d: %s", task_id, e)

            # If this task is part of a workflow, fail the workflow
            if task.workflow_id is not None and task.workflow_step is not None:
                await self._fail_workflow(
                    task.workflow_id, task.workflow_step,
                    f"Step {task.workflow_step} failed: {output[:500]}",
                )
                return

            await self._update_plane_state(
                task.plane_issue_id, self.config.plane.states.failed,
                f"Agent failed: {output[:500]}"
            )
            await self._notify(f"\u274c Agent failed: {task.title}\n{output[:200]}")

    async def resume_task(self, task_id: int, user_response: str) -> bool:
        """Resume a task that was waiting for user input."""
        task = await self.db.get_task(task_id)
        if not task or task.status != TaskStatus.WAITING_FOR_INPUT:
            return False

        if not self.runner.can_accept_task:
            logger.warning("Cannot resume task %d: no available slots", task_id)
            return False

        # Update clarification context with the response
        context: dict = {}
        if task.clarification_context:
            try:
                context = json.loads(task.clarification_context)
            except json.JSONDecodeError:
                pass
        history = context.get("history", [])
        if history:
            history[-1]["response"] = user_response
        context.pop("pending_question", None)
        context.pop("asked_at", None)
        context["history"] = history

        await self.db.update_task_fields(task_id, clarification_context=json.dumps(context))

        # Rebuild prompt with clarification history
        memories = []
        if self.memory:
            try:
                memories = await self.memory.recall(
                    repo=task.repo, query=f"{task.title} {task.description}"
                )
            except Exception as e:
                logger.warning("Memory recall failed for task %d: %s", task_id, e)

        prompt = self._build_prompt(
            task.title, task.description,
            memories=memories,
            clarification_history=history,
        )

        template = self.config.agent_templates.get(task.agent_type)
        if not template:
            await self.db.update_task_status(task_id, TaskStatus.FAILED, error="Unknown agent type on resume")
            return False

        wt_path = FACTORY_ROOT / "worktrees" / task.branch_name.replace("/", "-")

        await self.db.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        await self._update_plane_state(
            task.plane_issue_id, self.config.plane.states.in_progress,
            f"Agent resumed with user input"
        )
        await self._notify(f"\u25b6\ufe0f Agent resumed: {task.title}")

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
            await self.db.update_task_status(task_id, TaskStatus.FAILED, error="Failed to restart agent")
            return False

        return True

    async def poll_waiting_tasks(self):
        """Check for user responses on tasks waiting for input."""
        if not self.plane:
            return

        tasks = await self.db.list_tasks(status=TaskStatus.WAITING_FOR_INPUT)
        project_id = self.config.plane.project_id

        for task in tasks:
            if not task.plane_issue_id or not task.clarification_context:
                continue

            try:
                context = json.loads(task.clarification_context)
            except json.JSONDecodeError:
                continue

            asked_at = context.get("asked_at")
            if not asked_at:
                continue

            try:
                comments = await self.plane.get_comments(project_id, task.plane_issue_id)
            except Exception as e:
                logger.warning("Failed to fetch comments for task %d: %s", task.id, e)
                continue

            # Find the first comment posted after we asked the question
            response_text = None
            for comment in comments:
                created = comment.get("created_at", "")
                if created > asked_at:
                    html = comment.get("comment_html", "")
                    # Strip HTML tags to get plain text
                    response_text = re.sub(r"<[^>]+>", "", html).strip()
                    if response_text:
                        break

            if response_text:
                logger.info("Found response for task %d: %s", task.id, response_text[:100])
                await self.db.add_log(task.id, f"User responded: {response_text[:1000]}")
                await self.resume_task(task.id, response_text)

    def start_polling(self):
        """Start the background polling loop for waiting tasks."""
        if self._polling_task is None or self._polling_task.done():
            self._polling_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self):
        """Background loop that periodically checks for responses on waiting tasks."""
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL)
                await self.poll_waiting_tasks()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error in polling loop: %s", e)

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

    # ── Workflow orchestration ─────────────────────────────────────────────

    async def start_workflow(self, workflow_name: str, title: str,
                              description: str = "", repo: str = "",
                              plane_issue_id: str = "") -> Workflow | None:
        """Start a multi-step workflow by name."""
        wf_config = self.config.workflows.get(workflow_name)
        if not wf_config:
            logger.error("Unknown workflow: %s", workflow_name)
            return None

        if not wf_config.steps:
            logger.error("Workflow %s has no steps", workflow_name)
            return None

        # Create workflow record
        workflow = await self.db.create_workflow(
            name=workflow_name, title=title, description=description,
            repo=repo, plane_issue_id=plane_issue_id,
        )

        # Create step records
        for i, step_cfg in enumerate(wf_config.steps):
            await self.db.create_workflow_step(
                workflow_id=workflow.id,
                step_index=i,
                agent_type=step_cfg.agent,
                input_key=step_cfg.input,
                output_key=step_cfg.output,
                condition=step_cfg.condition,
            )

        # Mark workflow as running
        workflow = await self.db.update_workflow_status(workflow.id, WorkflowStatus.RUNNING)
        await self._notify(f"\U0001f680 Workflow started: {workflow_name} - {title}")
        logger.info("Started workflow %d (%s): %s", workflow.id, workflow_name, title)

        # Start first step
        await self._run_workflow_step(workflow.id, step_index=0)

        return await self.db.get_workflow(workflow.id)

    async def _run_workflow_step(self, workflow_id: int, step_index: int):
        """Execute a specific step in a workflow."""
        workflow = await self.db.get_workflow(workflow_id)
        if not workflow:
            return

        steps = workflow.steps
        if step_index >= len(steps):
            # All steps completed
            await self._complete_workflow(workflow_id)
            return

        step = steps[step_index]

        # Evaluate condition (if any)
        if step.condition:
            should_run = await self._evaluate_condition(workflow_id, step.condition)
            if not should_run:
                logger.info(
                    "Skipping workflow %d step %d (condition '%s' not met)",
                    workflow_id, step_index, step.condition,
                )
                await self.db.update_workflow_step_status(step.id, "skipped")
                await self.db.update_workflow_fields(workflow_id, current_step=step_index + 1)
                # Move to the next step
                await self._run_workflow_step(workflow_id, step_index + 1)
                return

        # Gather input from previous step output
        step_input = ""
        if step.input_key:
            step_input = await self.db.get_step_output(workflow_id, step.input_key)

        # Build task description with workflow context
        step_description = workflow.description or ""
        if step_input:
            step_description += f"\n\n## Input from previous step ({step.input_key})\n{step_input}"

        # Create a task for this step
        task = await self.db.create_task(TaskCreate(
            title=f"[{workflow.name}] Step {step_index}: {workflow.title}",
            description=step_description,
            repo=workflow.repo,
            agent_type=step.agent_type,
            plane_issue_id=workflow.plane_issue_id,
        ))

        # Link task to workflow
        await self.db.update_task_fields(
            task.id, workflow_id=workflow_id, workflow_step=step_index,
        )
        await self.db.update_workflow_step_status(step.id, "running", task_id=task.id)
        await self.db.update_workflow_fields(workflow_id, current_step=step_index)

        # Start the task
        success = await self.process_task(task.id)
        if not success:
            await self._fail_workflow(
                workflow_id, step_index,
                f"Failed to start step {step_index} (agent: {step.agent_type})",
            )

    async def _advance_workflow(self, workflow_id: int, completed_step_index: int, output: str):
        """Called when a workflow step's task completes successfully."""
        workflow = await self.db.get_workflow(workflow_id)
        if not workflow or workflow.status != WorkflowStatus.RUNNING:
            return

        # Find the step and mark it as completed with output
        steps = workflow.steps
        for step in steps:
            if step.step_index == completed_step_index:
                await self.db.update_workflow_step_status(
                    step.id, "completed", output_data=output[:10000],
                )
                # Also mark the task as done
                if step.task_id:
                    await self.db.update_task_status(step.task_id, TaskStatus.DONE)
                break

        logger.info(
            "Workflow %d step %d completed, advancing to step %d",
            workflow_id, completed_step_index, completed_step_index + 1,
        )

        # Advance to next step
        next_step = completed_step_index + 1
        await self._run_workflow_step(workflow_id, next_step)

    async def _complete_workflow(self, workflow_id: int):
        """Mark a workflow as completed and handle final outputs."""
        workflow = await self.db.update_workflow_status(workflow_id, WorkflowStatus.COMPLETED)
        if not workflow:
            return

        logger.info("Workflow %d (%s) completed successfully", workflow_id, workflow.name)

        # Find the last task with a branch to create a PR from
        last_task_id = None
        for step in reversed(workflow.steps):
            if step.task_id and step.status == "completed":
                last_task_id = step.task_id
                break

        if last_task_id:
            task = await self.db.get_task(last_task_id)
            if task and task.branch_name:
                pr_url = ""
                wt_path = FACTORY_ROOT / "worktrees" / task.branch_name.replace("/", "-")
                try:
                    summary = f"Workflow: {workflow.name}\n\n"
                    for step in workflow.steps:
                        status_icon = "\u2705" if step.status == "completed" else "\u23ed\ufe0f"
                        summary += f"{status_icon} Step {step.step_index}: {step.agent_type} ({step.status})\n"
                    pr_url = await self._push_and_create_pr(
                        last_task_id, wt_path, task.branch_name,
                        f"[Workflow] {workflow.title}", summary=summary,
                    )
                    await self.db.update_task_fields(last_task_id, pr_url=pr_url)
                except Exception as e:
                    logger.warning("Failed to create PR for workflow %d: %s", workflow_id, e)

        await self._notify(
            f"\u2705 Workflow completed: {workflow.name} - {workflow.title}"
        )

    async def _fail_workflow(self, workflow_id: int, failed_step_index: int, error: str):
        """Mark a workflow as failed."""
        workflow = await self.db.update_workflow_status(
            workflow_id, WorkflowStatus.FAILED, error=error,
        )
        if not workflow:
            return

        # Mark the failed step
        for step in workflow.steps:
            if step.step_index == failed_step_index and step.status == "running":
                await self.db.update_workflow_step_status(step.id, "failed")

        logger.error(
            "Workflow %d (%s) failed at step %d: %s",
            workflow_id, workflow.name, failed_step_index, error,
        )

        await self._update_plane_state(
            workflow.plane_issue_id, self.config.plane.states.failed,
            f"Workflow failed at step {failed_step_index}: {error[:500]}",
        )
        await self._notify(
            f"\u274c Workflow failed: {workflow.name} - {workflow.title}\n"
            f"Step {failed_step_index}: {error[:200]}"
        )

    async def cancel_workflow(self, workflow_id: int) -> bool:
        """Cancel a running workflow and its current task."""
        workflow = await self.db.get_workflow(workflow_id)
        if not workflow or workflow.status != WorkflowStatus.RUNNING:
            return False

        # Cancel the currently running task
        for step in workflow.steps:
            if step.status == "running" and step.task_id:
                await self.cancel_task(step.task_id)
                await self.db.update_workflow_step_status(step.id, "failed")

        await self.db.update_workflow_status(workflow_id, WorkflowStatus.CANCELLED)
        await self._notify(f"\U0001f6d1 Workflow cancelled: {workflow.name} - {workflow.title}")
        return True

    async def _evaluate_condition(self, workflow_id: int, condition: str) -> bool:
        """Evaluate a simple condition for conditional branching.

        Supported conditions:
        - "has_issues": Check if previous step output mentions issues/problems
        - "no_issues": Inverse of has_issues
        - Any other string is treated as a key to check in previous step outputs
        """
        # Get all completed step outputs for this workflow
        steps = await self.db.get_workflow_steps(workflow_id)
        last_output = ""
        for step in reversed(steps):
            if step.status == "completed" and step.output_data:
                last_output = step.output_data
                break

        if not last_output:
            return True  # No previous output, run the step by default

        lower_output = last_output.lower()

        if condition == "has_issues":
            issue_indicators = [
                "issue", "bug", "error", "problem", "fix needed",
                "needs revision", "change requested", "reject",
                "improvement needed", "concern",
            ]
            return any(indicator in lower_output for indicator in issue_indicators)

        if condition == "no_issues":
            issue_indicators = [
                "issue", "bug", "error", "problem", "fix needed",
                "needs revision", "change requested", "reject",
                "improvement needed", "concern",
            ]
            return not any(indicator in lower_output for indicator in issue_indicators)

        # Check for a specific key in output (e.g., check if a named output exists)
        output_data = await self.db.get_step_output(workflow_id, condition)
        return bool(output_data)

    async def recover_orphaned_workflows(self):
        """Mark any running workflows as failed on startup."""
        workflows = await self.db.list_workflows(status=WorkflowStatus.RUNNING)
        for wf in workflows:
            logger.warning("Recovering orphaned workflow %d: %s", wf.id, wf.title)
            await self.db.update_workflow_status(
                wf.id, WorkflowStatus.FAILED,
                error="Workflow lost due to orchestrator restart",
            )
            await self._notify(f"\u274c Workflow failed (restart): {wf.name} - {wf.title}")
        if workflows:
            logger.info("Recovered %d orphaned workflows", len(workflows))

    async def close(self):
        # Stop polling loop
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        # Kill all running agents on shutdown
        for task_id in list(self.runner.get_running_agents()):
            await self.runner.cancel_agent(task_id)
        if self.plane:
            await self.plane.close()
        if self.notifier:
            await self.notifier.close()


def _slugify(text: str) -> str:
    return "-".join(text.lower().split()[:5]).replace("/", "-")[:40]
