import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class RunningAgent:
    task_id: int
    process: asyncio.subprocess.Process
    workdir: Path
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AgentRunner:
    def __init__(self, max_concurrent: int = 3, claude_path: str = "claude"):
        self.max_concurrent = max_concurrent
        self.claude_path = claude_path
        self._running: dict[int, RunningAgent] = {}

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
    ) -> list[str]:
        cmd = [
            self.claude_path,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
        ]
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
        return cmd

    async def start_agent(
        self,
        task_id: int,
        prompt: str,
        workdir: Path,
        allowed_tools: list[str],
        on_output: Callable[[int, str], None] | None = None,
        on_complete: Callable[[int, int, str], None] | None = None,
    ) -> bool:
        if not self.can_accept_task:
            return False

        cmd = self._build_command(prompt, workdir, allowed_tools)
        logger.info("Starting agent for task %d", task_id)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        agent = RunningAgent(task_id=task_id, process=process, workdir=workdir)
        self._running[task_id] = agent

        asyncio.create_task(self._monitor_agent(agent, on_output, on_complete))
        return True

    async def _monitor_agent(
        self,
        agent: RunningAgent,
        on_output: Callable[[int, str], None] | None,
        on_complete: Callable[[int, int, str], None] | None,
    ):
        output_lines = []
        try:
            async for line in agent.process.stdout:
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

            result_text = ""
            for line in reversed(output_lines):
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "result":
                        result_text = msg.get("result", "")
                        break
                except json.JSONDecodeError:
                    pass

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
