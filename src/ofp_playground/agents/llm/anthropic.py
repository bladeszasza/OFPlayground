"""Anthropic Claude LLM agent."""
from __future__ import annotations

import logging
from typing import Optional

from ofp_playground.agents.llm.base import BaseLLMAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class AnthropicAgent(BaseLLMAgent):
    """LLM agent powered by Anthropic Claude."""

    def __init__(
        self,
        name: str,
        synopsis: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str = DEFAULT_MODEL,
        relevance_filter: bool = True,
    ):
        super().__init__(
            name=name,
            synopsis=synopsis,
            bus=bus,
            conversation_id=conversation_id,
            model=model or DEFAULT_MODEL,
            relevance_filter=relevance_filter,
            api_key=api_key,
        )
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    async def _quick_check(self, prompt: str) -> str:
        import asyncio
        loop = asyncio.get_event_loop()
        client = self._get_client()

        def _call():
            response = client.messages.create(
                model=self._model,
                max_tokens=10,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text if response.content else "NO"

        return await loop.run_in_executor(None, _call)

    async def _generate_response(self, participants: list[str]) -> Optional[str]:
        import asyncio
        loop = asyncio.get_event_loop()
        client = self._get_client()
        system = self._build_system_prompt(participants)
        messages = list(self._conversation_history[-20:])  # Last 20 messages

        # Ensure messages alternate roles properly for Anthropic
        if not messages:
            messages = [{"role": "user", "content": "Please introduce yourself briefly."}]

        def _call():
            response = client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=messages,
            )
            return response.content[0].text if response.content else None

        return await loop.run_in_executor(None, _call)
