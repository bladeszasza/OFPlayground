"""WebHumanAgent: human participant driven by an asyncio Queue (web UI / canvas)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from openfloor import Envelope

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

HUMAN_URI_TEMPLATE = "tag:ofp-playground.local,2025:human-{name}"

# Policies under which a human takes turns (holds the floor, then yields).
_TURN_TAKING_POLICIES = ("sequential", "round_robin")


class WebHumanAgent(BasePlaygroundAgent):
    """Human agent that receives messages via an asyncio Queue from the web UI.

    The Gradio UI / canvas posts user input into `input_queue`; this agent reads
    it and sends utterances into the OFP bus.  Incoming envelopes are forwarded
    to `output_queue` so the UI can render them.

    Floor behaviour mirrors the terminal ``HumanAgent`` when ``floor_policy`` is
    given, so a session behaves the same in every UI:

    * SEQUENTIAL — request the next turn after others speak; yield after sending.
    * ROUND_ROBIN — take the rotation turn when granted; yield after sending.
    * Any policy — a grant (e.g. ``/grant`` or an orchestrator ``[ASSIGN]``) is
      tracked and released after the human answers or ``/pass``-es.

    Without ``floor_policy`` the agent never requests the floor (interjecting
    observer), but it still yields a floor it was granted, so it can never
    deadlock a round-robin ring.
    """

    def __init__(
        self,
        name: str,
        bus: MessageBus,
        conversation_id: str,
        floor_policy: str | None = None,
    ):
        speaker_uri = HUMAN_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        super().__init__(
            speaker_uri=speaker_uri,
            name=name,
            service_url=f"local://human-{name.lower()}",
            bus=bus,
            conversation_id=conversation_id,
        )
        if floor_policy is not None:
            self.set_floor_policy(floor_policy)
        self._has_floor = self._floor_policy == "free_for_all"
        # UI → agent (user text input)
        self.input_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
        # agent → UI (incoming envelopes for display)
        self.output_queue: asyncio.Queue[Envelope] = asyncio.Queue()
        # Optional UI hook for floor notices ("your turn", "floor revoked").
        self.notice_callback: Callable[[str], None] | None = None

    @property
    def has_floor(self) -> bool:
        return self._has_floor

    def _notice(self, text: str) -> None:
        if self.notice_callback is not None:
            try:
                self.notice_callback(text)
            except Exception:
                logger.debug("[%s] notice callback failed", self._name, exc_info=True)

    async def send_text(self, text: str) -> None:
        """Send a text utterance on behalf of the human, then release a held turn."""
        envelope = self._make_utterance_envelope(text)
        await self.send_envelope(envelope)
        if self._has_floor and self._floor_policy != "free_for_all":
            self._has_floor = False
            await self.yield_floor()

    async def pass_turn(self) -> bool:
        """Give up a held floor without speaking. Returns True if a turn was released."""
        if not self._has_floor or self._floor_policy == "free_for_all":
            return False
        self._has_floor = False
        await self.yield_floor()
        return True

    async def _handle_floor_events(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        for event in (envelope.events or []):
            event_type = getattr(event, "eventType", type(event).__name__)
            if event_type == "grantFloor":
                self._has_floor = True
                self._notice(f"🎤 Your turn, {self._name} — type a message, or /pass to skip.")
            elif event_type == "revokeFloor":
                self._has_floor = False
            elif (
                event_type == "utterance"
                and sender_uri != self.speaker_uri
                and self._floor_policy == "sequential"
                and not self._has_floor
            ):
                # Same as the terminal human: queue for the next turn after others speak.
                await self.request_floor("waiting for next turn")

    async def run(self) -> None:
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)
        if self._floor_policy == "sequential":
            await self.request_floor("Starting conversation")

        async def _read_bus():
            """Forward incoming bus envelopes to the output queue."""
            while self._running:
                try:
                    envelope = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    await self._handle_floor_events(envelope)
                    await self.output_queue.put(envelope)
                except TimeoutError:
                    continue
                except Exception:
                    logger.exception('[%s] bus read error: see traceback', self._name)

        async def _read_input():
            """Forward UI input into the OFP bus."""
            task_id: str | None = None
            while self._running:
                try:
                    if self._status and task_id is None:
                        task_id = self._status.begin_work("awaiting-input")
                        self._status.transition("task", "input-required", task_id=task_id, reason="input-needed")
                        self._status.transition("lifecycle", "idle")
                    text = await asyncio.wait_for(self.input_queue.get(), timeout=1.0)
                    if text.strip():
                        await self.send_text(text.strip())
                        if self._status and task_id:
                            self._status.finish_work(task_id, "completed", "work-completed")
                            task_id = None
                except TimeoutError:
                    continue
                except asyncio.CancelledError:
                    if self._status and task_id:
                        self._status.finish_work(task_id, "canceled", "canceled")
                    raise
                except Exception:
                    logger.exception('[%s] input read error: see traceback', self._name)

        try:
            await asyncio.gather(_read_bus(), _read_input())
        finally:
            self._running = False
