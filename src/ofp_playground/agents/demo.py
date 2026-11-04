"""Explicit synthetic agents for provider-free runtime/Canvas exploration."""
from __future__ import annotations

import asyncio
from typing import Any

from ofp_playground.agents.llm.base import BaseLLMAgent


class DemoAgent(BaseLLMAgent):
    """Scripted work uses the real bus/floor/session/status path and no model API."""

    def __init__(self, *, mode: str = "normal", **kwargs: Any):
        if mode not in {"normal", "rate-limit", "input-wait", "silent", "stalled"}:
            raise ValueError("Unknown synthetic demo mode")
        super().__init__(model="scripted-local", relevance_filter=False, **kwargs)
        self.mode = mode
        self._work_activity = "showcase"
        self._status_heartbeat_interval = 1
        self._status_stall_threshold = 4
        self._status_silenced = False
        self._resume = asyncio.Event()
        self._max_retries = 1
        self._retry_delay = 1
        self._steps = 0
        self._attempts = 0

    async def _generate_response(self, participants: list[str]) -> str:
        self._attempts += 1
        if self.mode == "rate-limit" and self._attempts == 1:
            raise RuntimeError("429 synthetic rate limit")
        if self._attempts == 1 and self.mode in {"input-wait", "silent", "stalled"}:
            publisher = self._status
            if self.mode == "input-wait" and publisher and publisher.current_task:
                publisher.transition("task", "input-required", task_id=publisher.current_task,
                                     reason="input-needed")
                publisher.activity = "awaiting-input"
            elif self.mode == "silent":
                # Deliberate loss of telemetry: the independent monitor, rather
                # than this agent, must infer unreachable after 2.5 intervals.
                self._status_silenced = True
            await self._resume.wait()
            if publisher and publisher.current_task:
                publisher.progress += 1
                publisher.transition("task", "working", task_id=publisher.current_task,
                                     reason="work-started")
                publisher.activity = "showcase"
        await asyncio.sleep(0.8 if self._steps < 6 else 3)
        self._steps += 1
        return (f"Synthetic demo · {self.name} completed step {self._steps}. "
                "The runtime emitted work status and will yield the floor. No provider call was made.")

    async def _quick_check(self, prompt: str) -> str:
        return "YES"

    async def _handle_grant_floor(self) -> None:
        if self._steps >= 6:
            # Keep the instrumented participant observable after the bounded
            # demonstration, without an endless synthetic conversation.
            self._has_floor = False
            await self.yield_floor()
            return
        await super()._handle_grant_floor()

    def resume_input(self) -> bool:
        """A real operator input releases only the synthetic input wait."""
        if self.mode != "input-wait":
            return False
        self._resume.set()
        return True
