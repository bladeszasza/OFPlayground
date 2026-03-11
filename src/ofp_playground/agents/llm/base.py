"""Base LLM agent with common logic for all LLM providers."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from openfloor import Envelope

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus, FLOOR_MANAGER_URI

logger = logging.getLogger(__name__)

LLM_URI_TEMPLATE = "tag:ofp-playground.local,2025:llm-{name}"

SYSTEM_PROMPT_TEMPLATE = """You are {name}, an AI agent participating in an Open Floor Protocol conversation.

Your role: {synopsis}

You are in a multi-party conversation. You can see all messages on the floor.

RULES:
- Only speak when you have something relevant and valuable to contribute.
- Keep responses concise and on-topic.
- If addressed directly, always respond.
- Be collaborative — build on what others say.
- Do not repeat what has already been said.
- You may reference other participants by name.

Current participants: {participants}"""


class BaseLLMAgent(BasePlaygroundAgent):
    """Common logic for LLM-powered agents."""

    def __init__(
        self,
        name: str,
        synopsis: str,
        bus: MessageBus,
        conversation_id: str,
        model: Optional[str] = None,
        relevance_filter: bool = True,
        api_key: Optional[str] = None,
    ):
        speaker_uri = LLM_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        super().__init__(
            speaker_uri=speaker_uri,
            name=name,
            service_url=f"local://llm-{name.lower()}",
            bus=bus,
            conversation_id=conversation_id,
        )
        self._synopsis = synopsis
        self._model = model
        self._relevance_filter = relevance_filter
        self._api_key = api_key
        self._has_floor = False
        self._pending_context: list[dict] = []  # buffered messages since last turn
        self._consecutive_errors: int = 0

    def _build_system_prompt(self, participants: list[str]) -> str:
        return SYSTEM_PROMPT_TEMPLATE.format(
            name=self._name,
            synopsis=self._synopsis,
            participants=", ".join(participants) if participants else "You and the user",
        )

    def _append_to_context(self, speaker_name: str, text: str, is_self: bool) -> None:
        self._conversation_history.append({
            "role": "assistant" if is_self else "user",
            "content": f"[{speaker_name}]: {text}" if not is_self else text,
        })
        self._pending_context.append({
            "role": "assistant" if is_self else "user",
            "content": f"[{speaker_name}]: {text}" if not is_self else text,
        })

    async def _generate_response(self, participants: list[str]) -> Optional[str]:
        """Generate a response using the LLM. Override in subclasses."""
        raise NotImplementedError

    async def _check_relevance(self, latest_message: str) -> bool:
        """Ask the LLM if it should respond. Returns True if relevant."""
        if not self._relevance_filter:
            return True
        try:
            prompt = (
                f'Given this latest message in the conversation: "{latest_message}"\n\n'
                f"Should you ({self._name}, whose role is: {self._synopsis}) "
                f"contribute a response? Only say YES if you have something genuinely "
                f"relevant to add or if directly addressed. Respond with only YES or NO."
            )
            response = await self._quick_check(prompt)
            return response.strip().upper().startswith("YES")
        except Exception as e:
            logger.warning("Relevance check failed: %s", e)
            return True  # Default to speaking if check fails

    async def _quick_check(self, prompt: str) -> str:
        """Fast LLM call for relevance check. Override in subclasses."""
        raise NotImplementedError

    async def _handle_utterance(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return  # Don't process own messages

        text = self._extract_text_from_envelope(envelope)
        if not text:
            return

        # Resolve sender name from URI
        parts = sender_uri.split(":")
        sender_name = parts[-1] if parts else sender_uri

        self._append_to_context(sender_name, text, is_self=False)

        # Request floor to respond (if not already holding or waiting)
        if not self._has_floor and self._consecutive_errors < 3:
            await self.request_floor("responding to conversation")

    async def _handle_grant_floor(self) -> None:
        """Floor was granted — generate and send a response."""
        self._has_floor = True
        try:
            # Get participants from conversation history
            participants = []  # Could be populated from registry

            response_text = await self._generate_response(participants)
            if response_text:
                self._append_to_context(self._name, response_text, is_self=True)
                envelope = self._make_utterance_envelope(response_text)
                await self.send_envelope(envelope)
                self._consecutive_errors = 0
        except Exception as e:
            self._consecutive_errors += 1
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower() or "rate" in err_str.lower():
                short = err_str.split("\n")[0][:120]
                logger.warning("[%s] quota/rate-limit (attempt %d): %s",
                               self._name, self._consecutive_errors, short)
            elif "400" in err_str or "Bad request" in err_str or "model_not_supported" in err_str or "not supported" in err_str.lower():
                short = err_str.split("\n")[0][:120]
                logger.error("[%s] model error (giving up): %s", self._name, short)
                self._consecutive_errors = 99  # permanently retire this agent
            else:
                logger.error("[%s] LLM error: %s", self._name, e, exc_info=True)
        finally:
            self._has_floor = False
            await self.yield_floor()

    async def run(self) -> None:
        """Main LLM agent loop."""
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)

        # LLM agents request floor when they have something to say
        # For now, request floor to participate
        await self.request_floor("Ready to participate")

        try:
            while self._running:
                try:
                    envelope = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    await self._dispatch(envelope)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error("LLM agent error: %s", e, exc_info=True)
        finally:
            self._running = False

    async def _dispatch(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        for event in (envelope.events or []):
            event_type = getattr(event, "eventType", type(event).__name__)
            if event_type == "utterance":
                await self._handle_utterance(envelope)
            elif event_type == "grantFloor":
                await self._handle_grant_floor()
            elif event_type == "revokeFloor":
                self._has_floor = False
