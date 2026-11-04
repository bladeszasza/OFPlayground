"""Agent registry for tracking all active agents."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ofp_playground.agents.base import BasePlaygroundAgent

if TYPE_CHECKING:
    from ofp_playground.status.runtime import SessionStatusRuntime


class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, BasePlaygroundAgent] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.status_runtime: SessionStatusRuntime | None = None

    @property
    def tasks(self) -> tuple[asyncio.Task[None], ...]:
        return tuple(self._tasks.values())

    def start(self, agent: BasePlaygroundAgent) -> asyncio.Task[None]:
        """Own the run task until removal or session shutdown, including failures."""
        old = self._tasks.get(agent.speaker_uri)
        if old is not None:
            raise ValueError(f"Agent {agent.name!r} already has a run task")
        if self.status_runtime:
            self.status_runtime.track(agent)
        run = self.status_runtime.run_agent(agent) if self.status_runtime else agent.run()
        task = asyncio.create_task(run, name=f"agent:{agent.instance_id}")
        self._tasks[agent.speaker_uri] = task
        return task

    async def stop_agent(self, speaker_uri: str) -> None:
        agent = self.get(speaker_uri)
        if agent is not None:
            agent.stop()
        task = self._tasks.pop(speaker_uri, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.unregister(speaker_uri)

    async def close(self) -> None:
        """Cancel all children before awaiting any one child."""
        for agent in self.all():
            agent.stop()
        tasks = self.tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._agents.clear()

    def register(self, agent: BasePlaygroundAgent) -> None:
        self._agents[agent.speaker_uri] = agent

    def unregister(self, speaker_uri: str) -> None:
        self._agents.pop(speaker_uri, None)

    def get(self, speaker_uri: str) -> BasePlaygroundAgent | None:
        return self._agents.get(speaker_uri)

    def all(self) -> list[BasePlaygroundAgent]:
        return list(self._agents.values())

    def by_name(self, name: str) -> BasePlaygroundAgent | None:
        for agent in self._agents.values():
            if agent.name.lower() == name.lower():
                return agent
        return None

    def __len__(self) -> int:
        return len(self._agents)
