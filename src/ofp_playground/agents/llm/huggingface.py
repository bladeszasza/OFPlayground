"""HuggingFace Inference API LLM agent."""
from __future__ import annotations

import logging
import re
from typing import Optional

from ofp_playground.agents.llm.base import BaseLLMAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "MiniMaxAI/MiniMax-M2.5"

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class HuggingFaceAgent(BaseLLMAgent):
    """LLM agent powered by HuggingFace Inference API (serverless)."""

    def __init__(
        self,
        name: str,
        synopsis: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str = DEFAULT_MODEL,
        relevance_filter: bool = True,
        max_tokens: Optional[int] = None,
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
        if max_tokens is not None:
            self._max_tokens = max_tokens

    def _get_client(self):
        from huggingface_hub import InferenceClient
        return InferenceClient(token=self._api_key)

    def _clean(self, text: str) -> str:
        """Strip <think>...</think> blocks produced by reasoning models like Qwen3."""
        return _THINK_RE.sub("", text).strip()

    async def _quick_check(self, prompt: str) -> str:
        import asyncio
        loop = asyncio.get_event_loop()

        def _call():
            client = self._get_client()
            response = client.chat.completions.create(
                model=self._model,
                max_tokens=16,
                messages=[{"role": "user", "content": "/no_think\n" + prompt}],
            )
            return self._clean(response.choices[0].message.content or "NO")

        return await loop.run_in_executor(None, _call)

    async def _generate_response(self, participants: list[str]) -> Optional[str]:
        import asyncio
        loop = asyncio.get_event_loop()
        system = self._build_system_prompt(participants)
        messages = [{"role": "system", "content": system}] + list(self._conversation_history[-20:])

        if len(messages) == 1:
            messages.append({"role": "user", "content": "Please introduce yourself briefly."})

        # Prepend /no_think to the last user message to suppress chain-of-thought
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "user":
                messages[i] = {**messages[i], "content": "/no_think\n" + messages[i]["content"]}
                break

        def _call():
            client = self._get_client()
            response = client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=messages,
            )
            return self._clean(response.choices[0].message.content or "") or None

        return await loop.run_in_executor(None, _call)
