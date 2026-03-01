import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from factory.config import Config
from factory.db import Database
from factory.docker_toolkit import PREVIEW_DOMAIN, cleanup_test_environments
from factory.memory import AgentMemory
from factory.models import (
    HANDOFF_OUTPUT_TYPES, HandoffCreate, MessageCreate, MessageType,
    ReviewIssue, ReviewResult, TaskCreate, TaskStatus, Workflow, WorkflowStatus,
)
from factory.notifier import TelegramNotifier
from factory.plane import PlaneClient
from factory.prompts import load_prompt
from factory.revision_context import RevisionContext, build_revision_context
from factory.runner import AgentRunner
from factory.workspace import RepoManager, cleanup_task_worktree

logger = logging.getLogger(__name__)

FACTORY_ROOT = Path("/opt/factory")

PROGRESS_INTERVAL = 5  # Post progress to Plane every N output messages
POLL_INTERVAL = 30  # Seconds between polling for responses on waiting tasks
HANDOFF_MAX_CONTENT = 50000  # Max chars stored in handoff content
HANDOFF_SUMMARY_LIMIT = 2000  # Max chars for handoff summary
HANDOFF_INJECT_LIMIT = 8000  # Max chars injected into a prompt per handoff


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
        self.runner = AgentRunner(
            max_concurrent=config.max_concurrent_agents,
            timeout_minutes=config.agent_timeout_minutes,
            activity_timeout_minutes=config.agent_activity_timeout_minutes,
        )
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
        self._notified_preview_urls: dict[int, set[str]] = {}
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

    async def _push_to_existing_branch(self, wt_path: Path, branch_name: str):
        """Push updates to an existing branch (for revision tasks)."""
        token = os.environ.get("GITHUB_TOKEN", "")
        remote_url = await self._run("git", "remote", "get-url", "origin", cwd=wt_path)
        if token and "github.com" in remote_url and "x-access-token" not in remote_url:
            auth_url = remote_url.replace("https://github.com/", f"https://x-access-token:{token}@github.com/")
            await self._run("git", "remote", "set-url", "origin", auth_url, cwd=wt_path)

        await self._run("git", "push", "origin", branch_name, cwd=wt_path)

    async def _notify(self, message: str):
        if self.notifier:
            try:
                await self.notifier.send(message)
            except Exception as e:
                logger.warning("Telegram notification failed: %s", e)

    async def post_message(
        self,
        sender: str,
        message: str,
        message_type: MessageType = MessageType.INFO,
        recipient: str | None = None,
        task_id: int | None = None,
        workflow_id: int | None = None,
        reply_to: int | None = None,
    ):
        """Post a message to the agent message board."""
        if not self.config.message_board.enabled:
            return None
        try:
            msg = await self.db.create_message(MessageCreate(
                sender=sender,
                recipient=recipient,
                task_id=task_id,
                workflow_id=workflow_id,
                message=message,
                message_type=message_type,
                reply_to=reply_to,
            ))
            # Broadcast to SSE subscribers
            from factory.api import _message_subscribers
            msg_data = msg.model_dump(mode="json")
            msg_data["created_at"] = msg.created_at.isoformat()
            for queue in _message_subscribers:
                try:
                    queue.put_nowait(msg_data)
                except asyncio.QueueFull:
                    pass

            await self.forward_message_to_telegram(msg)
            return msg
        except Exception as e:
            logger.warning("Failed to post message: %s", e)
            return None

    async def forward_message_to_telegram(self, msg):
        """Forward a message board message to Telegram if configured."""
        if not self.notifier:
            return
        mb_config = self.config.message_board
        if not mb_config.telegram_forward:
            return
        if mb_config.forward_types and msg.message_type.value not in mb_config.forward_types:
            return

        type_icons = {
            "info": "\u2139\ufe0f",
            "question": "\u2753",
            "handoff": "\U0001f91d",
            "status": "\U0001f4cb",
            "error": "\U0001f6a8",
        }
        icon = type_icons.get(msg.message_type.value, "\U0001f4ac")
        text = f"{icon} <b>[{msg.message_type.value.upper()}]</b> from <b>{msg.sender}</b>"
        if msg.recipient:
            text += f" \u2192 <b>{msg.recipient}</b>"
        if msg.task_id:
            text += f" (task #{msg.task_id})"
        text += f"\n{msg.message[:500]}"

        try:
            # Use separate chat_id for message board if configured
            chat_id = mb_config.telegram_chat_id or self.notifier.chat_id
            original_chat_id = self.notifier.chat_id
            self.notifier.chat_id = chat_id
            await self.notifier.send(text)
            self.notifier.chat_id = original_chat_id
        except Exception as e:
            logger.warning("Failed to forward message to Telegram: %s", e)

    def _parse_agent_messages(self, task_id: int, content: str):
        """Check if agent output contains a message board post."""
        try:
            data = json.loads(content)
            if isinstance(data, dict) and data.get("type") == "message":
                loop = asyncio.get_running_loop()
                loop.create_task(self.post_message(
                    sender=f"task-{task_id}",
                    message=data.get("content", ""),
                    recipient=data.get("to"),
                    task_id=task_id,
                    message_type=MessageType(data.get("message_type", "info")),
                ))
                return True
        except (json.JSONDecodeError, ValueError):
            pass
        return False

    # Regex to detect Factory preview/test environment URLs in agent output
    _PREVIEW_URL_RE = re.compile(
        r"https://(?:task-\d+|pr-\d+)\." + re.escape(PREVIEW_DOMAIN)
    )

    def _detect_preview_url(self, task_id: int, content: str):
        """Check if agent output contains a preview environment URL.

        When detected, stores the URL in the database and sends a Telegram
        notification. Each URL is only notified once per task to avoid
        duplicate messages.
        """
        match = self._PREVIEW_URL_RE.search(content)
        if not match:
            return
        preview_url = match.group(0)

        # Deduplicate: only notify once per URL per task
        seen = self._notified_preview_urls.setdefault(task_id, set())
        if preview_url in seen:
            return
        seen.add(preview_url)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._handle_preview_deployed(task_id, preview_url))
        except RuntimeError:
            pass

    async def _handle_preview_deployed(self, task_id: int, preview_url: str):
        """Handle a successfully deployed preview environment.

        Stores the preview URL in the database, sends a Telegram notification,
        and posts a comment to the Plane issue.
        """
        task = await self.db.get_task(task_id)
        if not task:
            return

        # Persist the preview URL
        await self.db.update_task_fields(task_id, preview_url=preview_url)
        await self.db.add_log(task_id, f"Preview environment deployed: {preview_url}")

        logger.info(
            "Preview environment deployed for task %d: %s", task_id, preview_url,
        )

        # Send Telegram notification
        await self._notify(
            f"\U0001f680 Preview deployed: {task.title}\nURL: {preview_url}"
        )

        # Post to Plane
        await self._post_plane_comment(
            task.plane_issue_id,
            f'<p>\U0001f680 Preview deployed: <a href="{preview_url}">{preview_url}</a></p>',
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

    async def _post_plane_comment(self, plane_issue_id: str, comment: str):
        if not self.plane or not plane_issue_id:
            return
        project_id = self.config.plane.project_id
        try:
            await self.plane.add_comment(project_id, plane_issue_id, f"<p>{comment}</p>")
        except Exception as e:
            logger.warning("Failed to post comment to Plane issue %s: %s", plane_issue_id, e)

    async def _detect_revision_task(self, task) -> tuple[bool, str, str]:
        """Check if a task is a revision of a previous task that already has a PR.

        Returns:
            Tuple of (is_revision, existing_branch_name, existing_pr_url).
        """
        if not task.plane_issue_id:
            return False, "", ""

        try:
            previous = await self.db.find_previous_task_with_pr(task.plane_issue_id)
            if previous and previous.pr_url and previous.branch_name:
                logger.info(
                    "Task %d is a revision of task %d (PR: %s, branch: %s)",
                    task.id, previous.id, previous.pr_url, previous.branch_name,
                )
                return True, previous.branch_name, previous.pr_url
        except Exception as e:
            logger.warning("Revision detection failed for task %d: %s", task.id, e)

        return False, "", ""

    async def _fetch_revision_context(
        self, pr_url: str, branch_name: str, plane_issue_id: str, repo_dir: str | None = None,
    ) -> RevisionContext:
        """Fetch review feedback for a revision task."""
        try:
            return await build_revision_context(
                pr_url=pr_url,
                branch_name=branch_name,
                plane_client=self.plane,
                plane_project_id=self.config.plane.project_id,
                plane_issue_id=plane_issue_id,
                repo_dir=repo_dir,
            )
        except Exception as e:
            logger.warning("Failed to build revision context: %s", e)
            return RevisionContext(pr_url=pr_url, branch_name=branch_name)

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

        # Detect if this is a revision of a previous task with an existing PR
        is_revision, existing_branch, existing_pr_url = await self._detect_revision_task(task)

        try:
            await self.repo_manager.ensure_repo(task.repo, repo_config.url)

            if is_revision and existing_branch:
                # Revision task: check out the existing branch
                branch_name = existing_branch
                wt_path = await self.repo_manager.checkout_existing_branch(task.repo, branch_name)
                await self.db.update_task_fields(
                    task_id, branch_name=branch_name, pr_url=existing_pr_url,
                )
                logger.info(
                    "Task %d: revision mode, checked out existing branch %s",
                    task_id, branch_name,
                )
            else:
                # Fresh task: create a new branch
                # Use Plane sequence ID if available, otherwise fall back to internal ID
                task_ref = task.plane_sequence_id or task.id
                branch_name = f"agent/task-{task_ref}-{_slugify(task.title)}"
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

        # Fetch handoff context for this task (from previous agents)
        handoff_context = ""
        try:
            handoffs = await self.db.get_handoffs_for_task(task_id)
            if handoffs:
                handoff_context = self._format_handoff_context(handoffs)
        except Exception as e:
            logger.warning("Handoff context fetch failed for task %d: %s", task_id, e)

        # Fetch revision context (PR comments, task comments) if this is a revision
        revision_context = ""
        if is_revision:
            repo_path = self.repo_manager.repos_dir / task.repo
            rev_ctx = await self._fetch_revision_context(
                pr_url=existing_pr_url,
                branch_name=existing_branch,
                plane_issue_id=task.plane_issue_id,
                repo_dir=str(repo_path),
            )
            revision_context = rev_ctx.format_prompt_section()

        try:
            prompt = self._build_prompt(
                task.title, task.description,
                memories=memories, handoff_context=handoff_context,
                revision_context=revision_context,
            )

            # Use coder_revision prompt if available and this is a revision
            system_prompt_file = template.system_prompt_file
            if is_revision:
                revision_template = self.config.agent_templates.get("coder_revision")
                if revision_template and revision_template.system_prompt_file:
                    system_prompt_file = revision_template.system_prompt_file
            system_prompt = load_prompt(system_prompt_file, self.base_dir)

            await self.db.update_task_status(task_id, TaskStatus.IN_PROGRESS)
            mode_label = "revision" if is_revision else "fresh"
            await self._update_plane_state(
                task.plane_issue_id, self.config.plane.states.in_progress,
                f"Agent started ({mode_label}) on branch <code>{branch_name}</code>"
            )
            await self._notify(f"\U0001f527 Agent started ({mode_label}): {task.title}\nBranch: {branch_name}")

            # Auto-post status message on task start
            await self.post_message(
                sender=f"orchestrator",
                message=f"Task started ({mode_label}): {task.title} (agent: {task.agent_type}, branch: {branch_name})",
                message_type=MessageType.STATUS,
                task_id=task_id,
                workflow_id=task.workflow_id,
            )

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

    # ── Handoff helpers ─────────────────────────────────────────────────

    @staticmethod
    def _detect_output_type(output: str) -> str:
        """Classify agent output into a recognised handoff output type."""
        lower = output.lower()
        if any(tok in lower for tok in ("diff --git", "@@", "+++ b/", "--- a/")):
            return "code_diff"
        if any(tok in lower for tok in ("review comment", "nit:", "suggestion:", "change requested", "needs revision")):
            return "review_comments"
        if any(tok in lower for tok in ("research", "findings", "analysis", "investigation")):
            return "research_notes"
        if any(tok in lower for tok in ("test result", "tests pass", "tests fail", "pytest", "test suite")):
            return "test_results"
        if any(tok in lower for tok in ("error:", "traceback", "exception", "stack trace", "failed:")):
            return "error_report"
        return "general"

    @staticmethod
    def _extract_structured_output(output: str) -> dict:
        """Try to extract structured JSON handoff data from agent output.

        Agents may output a JSON block with explicit handoff metadata:
        ```json
        {"type": "handoff_output", "output_type": "...", "content": "...", "summary": "..."}
        ```
        """
        try:
            pattern = r'\{[^{}]*"type"\s*:\s*"handoff_output"[^{}]*\}'
            match = re.search(pattern, output)
            if match:
                data = json.loads(match.group(0))
                if data.get("type") == "handoff_output":
                    return {
                        "output_type": data.get("output_type", "general"),
                        "content": data.get("content", ""),
                        "summary": data.get("summary", ""),
                    }
        except (json.JSONDecodeError, AttributeError):
            pass
        return {}

    @staticmethod
    def _summarize_output(output: str, limit: int = HANDOFF_SUMMARY_LIMIT) -> str:
        """Create a summary of agent output for large content.

        Looks for ## Summary / ## Changes sections first; falls back to
        truncation with an ellipsis marker.
        """
        if len(output) <= limit:
            return output

        # Try to find explicit summary sections in the output
        summary_match = re.search(
            r"## Summary\s*\n(.*?)(?=\n## |\Z)", output, re.DOTALL,
        )
        changes_match = re.search(
            r"## Changes\s*\n(.*?)(?=\n## |\Z)", output, re.DOTALL,
        )

        parts: list[str] = []
        if summary_match:
            parts.append(summary_match.group(1).strip())
        if changes_match:
            parts.append("Changes:\n" + changes_match.group(1).strip())

        if parts:
            combined = "\n\n".join(parts)
            if len(combined) <= limit:
                return combined
            return combined[:limit - 3] + "..."

        # Fallback: truncate
        return output[:limit - 3] + "..."

    async def _create_handoff_from_output(
        self, task_id: int, output: str, workflow_id: int | None = None,
    ) -> int | None:
        """Parse agent output and persist a handoff record. Returns handoff id."""
        # First try explicit structured output
        structured = self._extract_structured_output(output)

        if structured and structured.get("content"):
            output_type = structured["output_type"]
            content = structured["content"][:HANDOFF_MAX_CONTENT]
            summary = structured.get("summary") or self._summarize_output(content)
        else:
            output_type = self._detect_output_type(output)
            content = output[:HANDOFF_MAX_CONTENT]
            summary = self._summarize_output(output)

        if output_type not in HANDOFF_OUTPUT_TYPES:
            output_type = "general"

        handoff = await self.db.create_handoff(HandoffCreate(
            from_task_id=task_id,
            workflow_id=workflow_id,
            output_type=output_type,
            content=content,
            summary=summary[:HANDOFF_SUMMARY_LIMIT],
        ))
        logger.info(
            "Created handoff %d from task %d (type=%s, %d chars)",
            handoff.id, task_id, output_type, len(content),
        )
        return handoff.id

    @staticmethod
    def _format_handoff_context(handoffs: list, inject_limit: int = HANDOFF_INJECT_LIMIT) -> str:
        """Format handoff records into a prompt section for injection."""
        if not handoffs:
            return ""

        lines: list[str] = ["\n## Context from previous agent steps"]
        remaining = inject_limit

        for h in handoffs:
            header = f"\n### {h.output_type} (from task #{h.from_task_id})"
            # Use summary for large content, full content if small enough
            body = h.summary if len(h.content) > inject_limit // len(handoffs) else h.content
            section = f"{header}\n{body}"

            if len(section) > remaining:
                section = section[:remaining - 3] + "..."
                lines.append(section)
                break
            lines.append(section)
            remaining -= len(section)

        return "\n".join(lines)

    def _build_prompt(
        self,
        title: str,
        description: str,
        memories: list[dict] | None = None,
        clarification_history: list[dict] | None = None,
        handoff_context: str = "",
        revision_context: str = "",
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

        if handoff_context:
            parts.append(handoff_context)

        if revision_context:
            parts.append(revision_context)

        if clarification_history:
            lines = ["\n## Previous clarifications"]
            for exchange in clarification_history:
                lines.append(f"Q: {exchange.get('question', '')}")
                lines.append(f"A: {exchange.get('response', '')}")
            parts.append("\n".join(lines))
            parts.append("\nPlease continue with the task using the information above.")

        parts.append("""
If you need clarification from the user before proceeding, output ONLY this JSON and then STOP (exit immediately):
{"type": "clarification_needed", "question": "Your question here"}
Do not output anything else — no explanation, no code, no thinking. Just the JSON line, then exit.
This will pause the task and post your question as a Plane comment for the user to answer.

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

            # Check for agent message board posts
            self._parse_agent_messages(task_id, content)

            # Check for preview environment URLs
            self._detect_preview_url(task_id, content)

            # Check for mid-stream clarification requests
            question = self._extract_clarification(content)
            if question:
                loop.create_task(self._handle_clarification_and_stop(task_id, question))
                return  # Don't buffer this output

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
        self._notified_preview_urls.pop(task_id, None)

        # Best-effort cleanup of any Docker test containers spawned by this task.
        # Runs synchronously before async completion handlers to ensure containers
        # are cleaned up even if the event loop is shutting down.
        # Preview environments are intentionally left running.
        try:
            cleanup_test_environments(task_id)
        except Exception as exc:
            logger.warning(
                "Docker cleanup failed for task %d: %s", task_id, exc,
            )

        try:
            loop = asyncio.get_running_loop()
            if returncode == 0:
                loop.create_task(self._handle_success(task_id, output))
            else:
                loop.create_task(self._handle_failure(task_id, output))
        except RuntimeError:
            pass

    async def _cleanup_task_worktree(self, task):
        """Best-effort cleanup of worktree and branches for a failed/cancelled task.

        Removes the worktree directory and local branch. Remote branches are
        only deleted when no PR was created (otherwise the branch is still
        useful for debugging or retries).
        """
        if not task.branch_name or not task.repo:
            return

        try:
            result = await cleanup_task_worktree(
                repos_dir=self.repo_manager.repos_dir,
                worktrees_dir=self.repo_manager.worktrees_dir,
                repo_name=task.repo,
                branch_name=task.branch_name,
                delete_remote_branch=not task.pr_url,
            )
            logger.info(
                "Worktree cleanup for task %d (branch %s): %s",
                task.id, task.branch_name, result,
            )
        except Exception as exc:
            logger.warning(
                "Worktree cleanup failed for task %d: %s", task.id, exc,
            )

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

    async def _handle_clarification_and_stop(self, task_id: int, question: str):
        """Handle mid-stream clarification: stop the agent and pause for input."""
        logger.info("Mid-stream clarification detected for task %d: %s", task_id, question)
        # Set status to waiting FIRST so _on_agent_complete skips failure handling
        await self.db.update_task_status(task_id, TaskStatus.WAITING_FOR_INPUT)
        # Handle the clarification (post to Plane, notify, update context)
        await self._handle_clarification(task_id, question)
        # Now stop the agent (completion handler will see waiting_for_input status)
        await self.runner.cancel_agent(task_id)

    async def _handle_success(self, task_id: int, output: str):
        # Check if task is already waiting for input (mid-stream clarification
        # was already handled by _handle_clarification_and_stop via _on_agent_output).
        # Without this guard, the same clarification would be posted to Plane twice:
        # once mid-stream and once here when the cancelled agent completes.
        task = await self.db.get_task(task_id)
        if task and task.status == TaskStatus.WAITING_FOR_INPUT:
            logger.info(
                "Skipping success handling for task %d (already waiting for input)",
                task_id,
            )
            return

        # Check if the agent is requesting clarification instead of completing
        question = self._extract_clarification(output)
        if question:
            await self.db.add_log(task_id, f"Agent requesting clarification: {question}")
            await self._handle_clarification(task_id, question)
            return

        await self.db.add_log(task_id, f"Agent completed successfully:\n{output[:2000]}")

        # Re-fetch task in case it was modified (task was fetched above for the
        # WAITING_FOR_INPUT guard, but may be stale after the log write).
        task = await self.db.get_task(task_id)
        if not task:
            return

        # Create a handoff record capturing the agent's output
        try:
            await self._create_handoff_from_output(
                task_id, output, workflow_id=task.workflow_id,
            )
        except Exception as e:
            logger.warning("Failed to create handoff for task %d: %s", task_id, e)

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

        # Standalone task: push branch and create PR (or push to existing PR)
        pr_url = task.pr_url  # May already be set for revision tasks
        wt_path = FACTORY_ROOT / "worktrees" / task.branch_name.replace("/", "-")

        if pr_url:
            # Revision task: push to existing branch (PR already exists)
            try:
                await self._push_to_existing_branch(wt_path, task.branch_name)
                logger.info("Pushed revision for task %d to existing PR: %s", task_id, pr_url)
                await self.db.add_log(task_id, f"Pushed revision to existing PR: {pr_url}")
            except Exception as e:
                logger.warning("Failed to push revision for task %d: %s", task_id, e)
                await self.db.add_log(task_id, f"Push to existing branch failed: {e}")
        else:
            # Fresh task: create a new PR
            try:
                pr_url = await self._push_and_create_pr(task_id, wt_path, task.branch_name, task.title, summary=output)
                await self.db.update_task_fields(task_id, pr_url=pr_url)
                logger.info("Created PR for task %d: %s", task_id, pr_url)
            except Exception as e:
                logger.warning("Failed to create PR for task %d: %s", task_id, e)
                await self.db.add_log(task_id, f"PR creation failed: {e}")

        await self.db.update_task_status(task_id, TaskStatus.IN_REVIEW)

        # Re-fetch to get preview_url that may have been set during the run
        task = await self.db.get_task(task_id)
        preview_url = task.preview_url if task else ""

        is_revision = bool(task.pr_url) if task else False
        mode_label = "Revision completed" if is_revision else "Agent completed"
        comment = f"{mode_label}. Branch: <code>{task.branch_name}</code>" if task else mode_label
        if pr_url:
            comment += f'<br/>PR: <a href="{pr_url}">{pr_url}</a>'
        if preview_url:
            comment += f'<br/>Preview: <a href="{preview_url}">{preview_url}</a>'
        await self._update_plane_state(
            task.plane_issue_id if task else "", self.config.plane.states.in_review, comment
        )

        notify_msg = f"\u2705 {mode_label}: {task.title}" if task else f"\u2705 {mode_label}"
        if pr_url:
            notify_msg += f"\nPR: {pr_url}"
        if preview_url:
            notify_msg += f"\nPreview: {preview_url}"
        await self._notify(notify_msg)

        # Auto-post status message on task completion
        completion_msg = f"{mode_label}: {task.title}" if task else mode_label
        if pr_url:
            completion_msg += f" (PR: {pr_url})"
        if preview_url:
            completion_msg += f" (Preview: {preview_url})"
        await self.post_message(
            sender="orchestrator",
            message=completion_msg,
            message_type=MessageType.STATUS,
            task_id=task_id,
            workflow_id=task.workflow_id if task else None,
        )

    async def _handle_failure(self, task_id: int, output: str):
        # Check if task is already waiting for input (mid-stream clarification)
        task = await self.db.get_task(task_id)
        if task and task.status == TaskStatus.WAITING_FOR_INPUT:
            logger.info("Skipping failure handling for task %d (waiting for input)", task_id)
            return
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

            # Clean up worktree and branches for failed task (best-effort).
            # Only delete remote branch if no PR was created (otherwise the
            # branch is still useful for debugging or retries).
            await self._cleanup_task_worktree(task)

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

            # Auto-post error message on task failure
            await self.post_message(
                sender="orchestrator",
                message=f"Task failed: {task.title}\n{output[:500]}",
                message_type=MessageType.ERROR,
                task_id=task_id,
                workflow_id=task.workflow_id,
            )

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

        handoff_context = ""
        try:
            handoffs = await self.db.get_handoffs_for_task(task_id)
            if handoffs:
                handoff_context = self._format_handoff_context(handoffs)
        except Exception as e:
            logger.warning("Handoff context fetch failed for task %d: %s", task_id, e)

        prompt = self._build_prompt(
            task.title, task.description,
            memories=memories,
            clarification_history=history,
            handoff_context=handoff_context,
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
            # Skip comments that look like our own system messages
            system_markers = ["Agent needs clarification", "Progress (step", "Agent failed:", "Agent resumed"]
            response_text = None
            for comment in comments:
                created = comment.get("created_at", "")
                if created > asked_at:
                    html = comment.get("comment_html", "")
                    # Skip our own clarification/system comments
                    if any(marker in html for marker in system_markers):
                        continue
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
                # Clean up worktree and branches for cancelled task
                await self._cleanup_task_worktree(task)

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
            max_iterations=wf_config.max_iterations,
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
                loop_to=step_cfg.loop_to,
                prompt_template=step_cfg.prompt_template,
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

        # Link any unlinked handoffs from this workflow to this task
        try:
            wf_handoffs = await self.db.get_handoffs_for_workflow(workflow_id)
            for h in wf_handoffs:
                if h.to_task_id is None:
                    await self.db.link_handoff_to_task(h.id, task.id)
        except Exception as e:
            logger.warning("Failed to link handoffs for workflow %d step %d: %s", workflow_id, step_index, e)

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
        completed_step = None
        steps = workflow.steps
        for step in steps:
            if step.step_index == completed_step_index:
                completed_step = step
                await self.db.update_workflow_step_status(
                    step.id, "completed", output_data=output[:10000],
                )
                # Also mark the task as done
                if step.task_id:
                    await self.db.update_task_status(step.task_id, TaskStatus.DONE)
                break

        # Check if this step has a loop_to directive
        if completed_step and completed_step.loop_to:
            should_loop = await self._should_loop(workflow, output)
            if should_loop:
                # Find the step index to loop back to
                loop_target = self._find_step_by_name(workflow, completed_step.loop_to)
                if loop_target is not None:
                    new_iteration = workflow.iteration + 1
                    await self.db.update_workflow_fields(
                        workflow_id, iteration=new_iteration,
                    )
                    logger.info(
                        "Workflow %d looping from step %d back to step %d (iteration %d/%d)",
                        workflow_id, completed_step_index, loop_target,
                        new_iteration, workflow.max_iterations,
                    )
                    await self._notify(
                        f"🔄 Workflow iteration {new_iteration}/{workflow.max_iterations}: "
                        f"{workflow.name} - {workflow.title}"
                    )
                    # Reset the target and subsequent steps for re-execution
                    await self._reset_steps_for_loop(workflow_id, loop_target)
                    await self._run_workflow_step(workflow_id, loop_target)
                    return

        logger.info(
            "Workflow %d step %d completed, advancing to step %d",
            workflow_id, completed_step_index, completed_step_index + 1,
        )

        # Advance to next step
        next_step = completed_step_index + 1
        await self._run_workflow_step(workflow_id, next_step)

    async def _should_loop(self, workflow: Workflow, step_output: str) -> bool:
        """Determine if a workflow step should loop back for another iteration.

        Called when a step with loop_to completes. The step is typically the
        coder's revision step. We loop back to review unless:
        - Max iterations have been reached
        - The coder reported inability to address feedback

        The review quality check (whether issues remain) happens via the
        condition evaluation when the review step's output triggers the
        next revision step.

        Returns True if we should loop back.
        """
        if workflow.iteration + 1 >= workflow.max_iterations:
            logger.info(
                "Workflow %d reached max iterations (%d), not looping",
                workflow.id, workflow.max_iterations,
            )
            return False

        # Check if coder reported inability to fix
        if _output_indicates_unable(step_output):
            logger.info("Workflow %d: coder reported unable to address feedback", workflow.id)
            return False

        return True

    @staticmethod
    def _find_step_by_name(workflow: Workflow, step_name: str) -> int | None:
        """Find a step's index by matching step name from config.

        We map step names based on the workflow config naming convention:
        - 'review' maps to the reviewer step
        - 'initial_code' maps to step 0
        - 'revision' maps to the conditional coder step

        For now, we use the step name to find matching output_key or step index.
        """
        # First try to find by output_key matching the name
        for step in workflow.steps:
            if step.output_key == step_name:
                return step.step_index

        # Try to match by agent type if the name contains the agent
        agent_map = {"review": "reviewer", "code": "coder", "revision": "coder"}
        for step in workflow.steps:
            if step_name in agent_map and step.agent_type == agent_map[step_name]:
                # If looking for 'review', find the reviewer step
                if step_name == "review" and step.agent_type == "reviewer":
                    return step.step_index

        # Last resort: try numeric interpretation
        try:
            idx = int(step_name)
            if 0 <= idx < len(workflow.steps):
                return idx
        except ValueError:
            pass

        return None

    async def _reset_steps_for_loop(self, workflow_id: int, from_step_index: int):
        """Reset workflow steps from a given index onwards for re-execution.

        Creates new step records for the loop iteration while preserving
        step configuration.
        """
        workflow = await self.db.get_workflow(workflow_id)
        if not workflow:
            return

        for step in workflow.steps:
            if step.step_index >= from_step_index:
                # Reset the step status so it can run again
                await self.db.update_workflow_step_status(
                    step.id, "pending",
                )
                # Clear previous timestamps and task linkage for re-run
                await self._db_clear_step_for_rerun(step.id)

    async def _db_clear_step_for_rerun(self, step_id: int):
        """Clear step's task_id, output_data, and timestamps for a new iteration."""
        await self.db._db.execute(
            "UPDATE workflow_steps SET task_id = NULL, output_data = '', "
            "started_at = NULL, completed_at = NULL WHERE id = ?",
            (step_id,),
        )
        await self.db._db.commit()

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
        - "has_issues": Check if previous step output indicates issues (prefers structured review)
        - "no_issues": Inverse of has_issues
        - "review_approved": Check if structured review was approved
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

        # Try structured review parsing first
        review = parse_review_output(last_output)

        if condition == "has_issues":
            if review is not None:
                return not review.approved and review.has_blockers_or_majors
            return _has_review_issues(last_output)

        if condition == "no_issues":
            if review is not None:
                return review.approved or not review.has_blockers_or_majors
            return not _has_review_issues(last_output)

        if condition == "review_approved":
            if review is not None:
                return review.approved
            return not _has_review_issues(last_output)

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
    """Convert text to a git-safe branch name slug.
    
    Git branch names cannot contain: space, ~, ^, :, ?, *, [, \\, or ..
    """
    import re
    # Remove or replace invalid git branch characters
    slug = text.lower()
    slug = re.sub(r'[~^:?*\[\]\\]', '', slug)  # Remove invalid chars
    slug = re.sub(r'\.\.+', '.', slug)  # Collapse multiple dots
    slug = "-".join(slug.split()[:5])  # Take first 5 words
    slug = slug.replace("/", "-")
    slug = re.sub(r'-+', '-', slug)  # Collapse multiple dashes
    slug = slug.strip('-.')  # Remove leading/trailing dashes and dots
    return slug[:40]


def parse_review_output(output: str) -> ReviewResult | None:
    """Parse structured review JSON from agent output.

    Expects output containing a JSON block with the review result:
    ```json
    {"approved": false, "summary": "...", "issues": [...], "suggestions": [...]}
    ```

    Returns None if no structured review JSON is found.
    """

    def _build_review(data: dict) -> ReviewResult:
        issues = []
        for issue_data in data.get("issues", []):
            severity = issue_data.get("severity", "minor")
            if severity not in ("blocker", "major", "minor", "nit"):
                severity = "minor"
            issues.append(ReviewIssue(
                severity=severity,
                description=issue_data.get("description", ""),
                file=issue_data.get("file", ""),
                line=issue_data.get("line"),
                suggestion=issue_data.get("suggestion", ""),
            ))
        return ReviewResult(
            approved=data.get("approved", False),
            summary=data.get("summary", ""),
            issues=issues,
            suggestions=data.get("suggestions", []),
        )

    # Try to find a fenced code block first
    patterns = [
        r'```json\s*\n(.*?)\n\s*```',
        r'```\s*\n(.*?)\n\s*```',
    ]

    for pattern in patterns:
        match = re.search(pattern, output, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                if "approved" in data:
                    return _build_review(data)
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

    # Fallback: find raw JSON by brace-counting for nested objects
    start_idx = output.find('{')
    while start_idx != -1:
        depth = 0
        for i in range(start_idx, len(output)):
            if output[i] == '{':
                depth += 1
            elif output[i] == '}':
                depth -= 1
                if depth == 0:
                    candidate = output[start_idx:i + 1]
                    try:
                        data = json.loads(candidate)
                        if "approved" in data:
                            return _build_review(data)
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass
                    break
        start_idx = output.find('{', start_idx + 1)

    return None


def _has_review_issues(output: str) -> bool:
    """Fallback check for review issues in unstructured output."""
    lower_output = output.lower()
    issue_indicators = [
        "issue", "bug", "error", "problem", "fix needed",
        "needs revision", "change requested", "reject",
        "improvement needed", "concern",
    ]
    return any(indicator in lower_output for indicator in issue_indicators)


def _output_indicates_unable(output: str) -> bool:
    """Check if the coder's output indicates inability to address feedback."""
    lower = output.lower()
    unable_indicators = [
        "unable to address",
        "cannot fix",
        "unable to resolve",
        "cannot address",
        "not possible to fix",
        "beyond scope",
        "cannot be resolved",
        "unable to implement",
    ]
    return any(indicator in lower for indicator in unable_indicators)
