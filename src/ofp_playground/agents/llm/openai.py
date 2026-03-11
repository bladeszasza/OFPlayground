"""OpenAI GPT LLM agent."""
from __future__ import annotations

import logging
from typing import Optional

from ofp_playground.agents.llm.base import BaseLLMAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o"


class OpenAIAgent(BaseLLMAgent):
    """LLM agent powered by OpenAI GPT."""

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
            from openai import OpenAI
            self._client = OpenAI(api_key=self._api_key)
        return self._client

    async def _quick_check(self, prompt: str) -> str:
        import asyncio
        loop = asyncio.get_event_loop()
        client = self._get_client()

        def _call():
            response = client.chat.completions.create(
                model=self._model,
                max_tokens=10,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content or "NO"

        return await loop.run_in_executor(None, _call)

    async def _generate_response(self, participants: list[str]) -> Optional[str]:
        import asyncio
        loop = asyncio.get_event_loop()
        client = self._get_client()
        system = self._build_system_prompt(participants)
        messages = [{"role": "system", "content": system}] + list(self._conversation_history[-20:])

        if len(messages) == 1:
            messages.append({"role": "user", "content": "Please introduce yourself briefly."})

        def _call():
            response = client.chat.completions.create(
                model=self._model,
                max_tokens=500,
                messages=messages,
            )
            return response.choices[0].message.content

        return await loop.run_in_executor(None, _call)
