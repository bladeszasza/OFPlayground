"""Base LLM agent with common logic for all LLM providers."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from openfloor import Capability, Envelope, Identification, Manifest, SupportedLayers

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus, FLOOR_MANAGER_URI

logger = logging.getLogger(__name__)

LLM_URI_TEMPLATE = "tag:ofp-playground.local,2025:llm-{name}"

SYSTEM_PROMPT_TEMPLATE = """You are {name}, an AI agent participating in a collaborative story.

Your role: {synopsis}

RULES:
- Write ONLY your creative content. No preamble, no meta-commentary.
- Do NOT start your response with [{name}]:, [Director]:, or any name in brackets.
- Do NOT repeat instructions or reference who gave them.
- Keep responses concise. Build on what others said.

Current participants: {participants}"""


def _uri_to_name(uri: str) -> str:
    """Derive a readable display name from a speaker URI."""
    raw = uri.split(":")[-1]
    for prefix in ("llm-", "human-", "image-", "video-", "audio-", "director-"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.replace("-", " ").title()


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
        self._max_tokens: int = 12000
        self._has_floor = False
        self._pending_context: list[dict] = []  # buffered messages since last turn
        self._consecutive_errors: int = 0
        self._name_registry: dict[str, str] = {}
        self._current_director_instruction: str = ""  # injected into system prompt at generation time
        self._memory_store = None  # set via set_memory_store() when in showrunner_driven mode

    @property
    def task_type(self) -> str:
        """OFP task type keyphrase for this agent. Override in subclasses."""
        return "text-generation"

    def _build_manifest(self) -> Manifest:
        return Manifest(
            identification=Identification(
                speakerUri=self._speaker_uri,
                serviceUrl=self._service_url,
                conversationalName=self._name,
                role=self._synopsis,
            ),
            capabilities=[
                Capability(
                    keyphrases=[self.task_type],
                    descriptions=[self._synopsis],
                    supportedLayers=SupportedLayers(input=["text"], output=["text"]),
                )
            ],
        )

    def set_name_registry(self, registry: dict[str, str]) -> None:
        """Attach the shared URI→name registry (floor._agents reference)."""
        self._name_registry = registry

    def set_memory_store(self, store) -> None:
        """Attach the shared session MemoryStore (set by FloorManager on agent registration)."""
        self._memory_store = store

    def _build_system_prompt(self, participants: list[str]) -> str:
        base = SYSTEM_PROMPT_TEMPLATE.format(
            name=self._name,
            synopsis=self._synopsis,
            participants=", ".join(participants) if participants else "You and the user",
        )
        if self._current_director_instruction:
            base += f"\n\nYOUR TASK THIS ROUND: {self._current_director_instruction}\nWrite your response now. Do not repeat this instruction."
        if self._memory_store and not self._memory_store.is_empty():
            memory_summary = self._memory_store.get_summary(max_chars=600)
            base += f"\n\n--- SESSION MEMORY ---\n{memory_summary}\n---"
        return base

    def _append_to_context(self, speaker_name: str, text: str, is_self: bool) -> None:
        # Use "Name: content" (no brackets) to reduce LLM echo of the [name]: format
        content = text if is_self else f"{speaker_name}: {text}"
        entry = {"role": "assistant" if is_self else "user", "content": content}
        self._conversation_history.append(entry)
        self._pending_context.append(entry)

    def _parse_director_message(self, text: str) -> bool:
        """Parse a [SCENE]/[AgentName] Director message.

        Stores this agent's assignment into _current_director_instruction (injected
        into the system prompt at generation time — NOT into conversation history).
        Returns True if this agent was assigned (should request floor).
        """
        scene_line = ""
        my_assignment = ""
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("[SCENE]"):
                scene_line = line[7:].strip()
            m = re.match(rf"^\[{re.escape(self._name)}\]\s*(.*)", line, re.IGNORECASE)
            if m:
                my_assignment = m.group(1).strip()

        if my_assignment:
            # Inject as system prompt addendum — never into conversation history
            self._current_director_instruction = (
                f"{my_assignment} (Scene context: {scene_line})"
                if scene_line
                else my_assignment
            )
            return True

        # Not assigned this round — clear any stale instruction
        self._current_director_instruction = ""
        return False

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

    def _parse_showrunner_message(self, text: str) -> bool:
        """Parse [DIRECTIVE for AgentName]: instruction from ShowRunner output.

        Stores this agent's assignment into _current_director_instruction (injected
        into the system prompt at generation time — NOT into conversation history).
        Returns True if this agent was assigned (should request floor).
        """
        m = re.search(
            rf"\[DIRECTIVE for {re.escape(self._name)}\]:\s*(.+)",
            text,
            re.IGNORECASE,
        )
        if m:
            self._current_director_instruction = m.group(1).strip()
            return True
        self._current_director_instruction = ""
        return False

    async def _handle_utterance(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return  # Don't process own messages

        text = self._extract_text_from_envelope(envelope)
        if not text:
            return

        # Detect Director-format message ([SCENE] prefix) and handle specially
        if text.strip().startswith("[SCENE]"):
            assigned = self._parse_director_message(text)
            if assigned and not self._has_floor and self._consecutive_errors < 3:
                # Bypass relevance filter — Director already decided we should speak
                await self.request_floor("responding to Director assignment")
            # Either way, don't fall through to generic floor-request logic
            return

        # Detect ShowRunner-format message ([DIRECTIVE for Name]: lines) and handle specially
        if "[DIRECTIVE for" in text:
            assigned = self._parse_showrunner_message(text)
            if assigned and not self._has_floor and self._consecutive_errors < 3:
                # Orchestrator directives arrive as private messages FROM the floor manager
                # and always come paired with an explicit grantFloor — no need to request.
                # ShowRunner directives come from a peer agent; those need request_floor().
                if sender_uri != FLOOR_MANAGER_URI:
                    await self.request_floor("responding to ShowRunner directive")
            # Either way, don't fall through to generic floor-request logic
            return

        # Skip media agent utterances — generated image/video descriptions are noise
        if any(k in sender_uri for k in ("image", "video", "audio")):
            return

        # Regular utterance: resolve sender name and add to context
        sender_name = self._name_registry.get(sender_uri) or _uri_to_name(sender_uri)
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

            response_text = await self._call_with_retry(lambda: self._generate_response(participants))
            if response_text:
                self._append_to_context(self._name, response_text, is_self=True)
                envelope = self._make_utterance_envelope(response_text)
                await self.send_envelope(envelope)
                self._consecutive_errors = 0
        except asyncio.TimeoutError:
            self._consecutive_errors += 1
            logger.warning("[%s] response timed out (errors: %d)", self._name, self._consecutive_errors)
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
            self._current_director_instruction = ""  # consumed — clear for next round
            await self.yield_floor()

    async def run(self) -> None:
        """Main LLM agent loop."""
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)
        await self._publish_manifest()

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
            elif event_type == "invite":
                # OFP: respond with acceptInvite directed to the floor manager
                from openfloor import Event, To
                accept_envelope = Envelope(
                    sender=self._make_sender(),
                    conversation=self._make_conversation(),
                    events=[Event(
                        eventType="acceptInvite",
                        to=To(speakerUri=FLOOR_MANAGER_URI),
                        reason="Ready to participate",
                    )],
                )
                await self.send_envelope(accept_envelope)
            elif event_type == "uninvite":
                # OFP: floor manager is removing this agent — stop cleanly
                logger.info("[%s] received uninvite — stopping", self._name)
                self._running = False
