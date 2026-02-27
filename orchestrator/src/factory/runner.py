import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# 10MB buffer limit for subprocess stdout (Claude can output large JSON lines)
STREAM_BUFFER_LIMIT = 10 * 1024 * 1024

# Default timeouts
DEFAULT_TIMEOUT_MINUTES = 60
DEFAULT_ACTIVITY_TIMEOUT_MINUTES = 15


@dataclass
class RunningAgent:
    task_id: int
    process: asyncio.subprocess.Process
    workdir: Path
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES
    activity_timeout_minutes: int = DEFAULT_ACTIVITY_TIMEOUT_MINUTES


class AgentRunner:
    def __init__(
        self,
        max_concurrent: int = 3,
        claude_path: str = "claude",
        timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
        activity_timeout_minutes: int = DEFAULT_ACTIVITY_TIMEOUT_MINUTES,
    ):
        self.max_concurrent = max_concurrent
        self.claude_path = claude_path
        self.timeout_minutes = timeout_minutes
        self.activity_timeout_minutes = activity_timeout_minutes
        self._running: dict[int, RunningAgent] = {}
        self._watchdog_task: asyncio.Task | None = None

    @property
    def available_slots(self) -> int:
        return self.max_concurrent - len(self._running)

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def can_accept_task(self) -> bool:
        return self.available_slots > 0

    def get_running_agents(self) -> dict[int, RunningAgent]:
        return dict(self._running)

    def _build_command(
        self,
        prompt: str,
        workdir: Path,
        allowed_tools: list[str],
        system_prompt: str = "",
    ) -> list[str]:
        cmd = [
            self.claude_path,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
        ]
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
        return cmd

    async def start_agent(
        self,
        task_id: int,
        prompt: str,
        workdir: Path,
        allowed_tools: list[str],
        system_prompt: str = "",
        on_output: Callable[[int, str], None] | None = None,
        on_complete: Callable[[int, int, str], None] | None = None,
        timeout_minutes: int | None = None,
        activity_timeout_minutes: int | None = None,
    ) -> bool:
        if not self.can_accept_task:
            return False

        cmd = self._build_command(prompt, workdir, allowed_tools, system_prompt)
        logger.info("Starting agent for task %d (timeout: %d min, activity timeout: %d min)",
                    task_id,
                    timeout_minutes or self.timeout_minutes,
                    activity_timeout_minutes or self.activity_timeout_minutes)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_BUFFER_LIMIT,
        )

        agent = RunningAgent(
            task_id=task_id,
            process=process,
            workdir=workdir,
            timeout_minutes=timeout_minutes or self.timeout_minutes,
            activity_timeout_minutes=activity_timeout_minutes or self.activity_timeout_minutes,
        )
        self._running[task_id] = agent

        # Start watchdog if not running
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._timeout_watchdog())

        asyncio.create_task(self._monitor_agent(agent, on_output, on_complete))
        return True

    async def _timeout_watchdog(self):
        """Periodically check for timed-out agents and kill them."""
        while self._running:
            await asyncio.sleep(30)  # Check every 30 seconds

            now = datetime.now(timezone.utc)
            timed_out = []

            for task_id, agent in list(self._running.items()):
                runtime = (now - agent.started_at).total_seconds() / 60
                idle_time = (now - agent.last_activity).total_seconds() / 60

                if runtime > agent.timeout_minutes:
                    logger.warning(
                        "Task %d exceeded total timeout (%.1f min > %d min), killing agent",
                        task_id, runtime, agent.timeout_minutes
                    )
                    timed_out.append((task_id, f"Total runtime timeout ({agent.timeout_minutes} min)"))
                elif idle_time > agent.activity_timeout_minutes:
                    logger.warning(
                        "Task %d exceeded activity timeout (%.1f min idle > %d min), killing agent",
                        task_id, idle_time, agent.activity_timeout_minutes
                    )
                    timed_out.append((task_id, f"No activity for {agent.activity_timeout_minutes} min"))

            for task_id, reason in timed_out:
                await self._kill_agent(task_id, reason)

        logger.debug("Watchdog exiting - no running agents")

    async def _kill_agent(self, task_id: int, reason: str):
        """Kill an agent and trigger completion callback with timeout error."""
        agent = self._running.get(task_id)
        if not agent:
            return

        logger.info("Killing agent for task %d: %s", task_id, reason)

        # Terminate gracefully first
        try:
            agent.process.terminate()
            await asyncio.wait_for(agent.process.wait(), timeout=5)
        except asyncio.TimeoutError:
            # Force kill if terminate didn't work
            agent.process.kill()
            try:
                await asyncio.wait_for(agent.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.error("Failed to kill agent process for task %d", task_id)

    async def _monitor_agent(
        self,
        agent: RunningAgent,
        on_output: Callable[[int, str], None] | None,
        on_complete: Callable[[int, int, str], None] | None,
    ):
        output_lines = []
        timed_out = False
        try:
            async for line in agent.process.stdout:
                # Update activity timestamp on any output
                agent.last_activity = datetime.now(timezone.utc)

                decoded = line.decode().strip()
                if not decoded:
                    continue
                output_lines.append(decoded)
                try:
                    msg = json.loads(decoded)
                    if msg.get("type") == "assistant" and on_output:
                        content = msg.get("message", {}).get("content", "")
                        if isinstance(content, list):
                            text_parts = [
                                p.get("text", "")
                                for p in content
                                if p.get("type") == "text"
                            ]
                            content = "\n".join(text_parts)
                        if content:
                            on_output(agent.task_id, content)
                except json.JSONDecodeError:
                    pass

            await agent.process.wait()
            returncode = agent.process.returncode

            # Check if this was a timeout kill (negative return code from signal)
            if returncode is not None and returncode < 0:
                timed_out = True

            result_text = ""
            for line in reversed(output_lines):
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "result":
                        result_text = msg.get("result", "")
                        break
                except json.JSONDecodeError:
                    pass

            if timed_out:
                logger.warning("Agent for task %d was killed due to timeout", agent.task_id)
                result_text = f"Agent killed: timeout exceeded\n\nLast output:\n{chr(10).join(output_lines[-10:])}"
                returncode = -1
            else:
                logger.info(
                    "Agent for task %d exited with code %d",
                    agent.task_id,
                    returncode,
                )

            if on_complete:
                on_complete(
                    agent.task_id,
                    returncode,
                    result_text or "\n".join(output_lines[-20:]),
                )

        except Exception as e:
            logger.exception("Error monitoring agent for task %d", agent.task_id)
            if on_complete:
                on_complete(agent.task_id, -1, str(e))
        finally:
            self._running.pop(agent.task_id, None)

    async def cancel_agent(self, task_id: int) -> bool:
        agent = self._running.get(task_id)
        if not agent:
            return False
        agent.process.terminate()
        try:
            await asyncio.wait_for(agent.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            agent.process.kill()
        self._running.pop(task_id, None)
        return True
