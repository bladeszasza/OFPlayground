"""Anthropic Claude LLM agent."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic

from ofp_playground.agents.llm.anthropic_compat import (
    effort_kwargs,
    prepare_messages,
    response_text,
)
from ofp_playground.agents.llm.base import BaseLLMAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-5-5"


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
        self._client: anthropic.Anthropic | None = None

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
            # Current models think by default: leave room for it, and use low
            # effort (where the model accepts effort) to keep the check quick.
            response = client.messages.create(
                model=self._model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
                **effort_kwargs(self._model, "low"),
            )
            return response_text(response) or "NO"

        return await loop.run_in_executor(None, _call)

    async def _generate_response(self, participants: list[str]) -> str | None:
        import asyncio
        loop = asyncio.get_event_loop()
        client = self._get_client()
        system = self._build_system_prompt(participants)
        # Last 20 messages, starting and ending on a user turn: current models
        # reject a request that ends on the assistant's own turn (prefill).
        messages = prepare_messages(self._conversation_history[-20:],
                                    opening="Please introduce yourself briefly.")

        def _call():
            response = client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=messages,
            )
            return response_text(response) or None

        return await loop.run_in_executor(None, _call)
