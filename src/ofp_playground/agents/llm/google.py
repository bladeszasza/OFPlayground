"""Google Gemini LLM agent."""
from __future__ import annotations

import logging
from typing import Optional

from ofp_playground.agents.llm.base import BaseLLMAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.1-flash-lite-preview"


class GoogleAgent(BaseLLMAgent):
    """LLM agent powered by Google Gemini."""

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

    async def _quick_check(self, prompt: str) -> str:
        import asyncio
        from google import genai
        loop = asyncio.get_event_loop()

        def _call():
            client = genai.Client(api_key=self._api_key)
            response = client.models.generate_content(
                model=self._model,
                contents=prompt,
            )
            return response.text or "NO"

        return await loop.run_in_executor(None, _call)

    async def _generate_response(self, participants: list[str]) -> Optional[str]:
        import asyncio
        from google import genai
        from google.genai import types
        loop = asyncio.get_event_loop()
        system = self._build_system_prompt(participants)

        history = list(self._conversation_history[-20:])
        if not history:
            history = [{"role": "user", "content": "Please introduce yourself briefly."}]

        contents = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in history
        )
        full_prompt = f"{system}\n\n{contents}"

        def _call():
            from google.genai import types as gtypes
            client = genai.Client(api_key=self._api_key)
            response = client.models.generate_content(
                model=self._model,
                contents=full_prompt,
                config=gtypes.GenerateContentConfig(max_output_tokens=self._max_tokens),
            )
            return response.text

        return await loop.run_in_executor(None, _call)
