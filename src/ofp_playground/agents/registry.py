"""Agent registry for tracking all active agents."""
from __future__ import annotations

from typing import Optional
from ofp_playground.agents.base import BasePlaygroundAgent


class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, BasePlaygroundAgent] = {}

    def register(self, agent: BasePlaygroundAgent) -> None:
        self._agents[agent.speaker_uri] = agent

    def unregister(self, speaker_uri: str) -> None:
        self._agents.pop(speaker_uri, None)

    def get(self, speaker_uri: str) -> Optional[BasePlaygroundAgent]:
        return self._agents.get(speaker_uri)

    def all(self) -> list[BasePlaygroundAgent]:
        return list(self._agents.values())

    def by_name(self, name: str) -> Optional[BasePlaygroundAgent]:
        for agent in self._agents.values():
            if agent.name.lower() == name.lower():
                return agent
        return None

    def __len__(self) -> int:
        return len(self._agents)
