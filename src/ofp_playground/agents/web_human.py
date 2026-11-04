"""WebHumanAgent: human participant driven by an asyncio Queue (for Gradio UI)."""
from __future__ import annotations

import asyncio
import logging

from openfloor import Envelope

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

HUMAN_URI_TEMPLATE = "tag:ofp-playground.local,2025:human-{name}"


class WebHumanAgent(BasePlaygroundAgent):
    """Human agent that receives messages via an asyncio Queue from the web UI.

    The Gradio UI posts user input into `input_queue`; this agent reads it
    and sends utterances into the OFP bus.  Incoming envelopes are forwarded
    to `output_queue` so the UI can render them.
    """

    def __init__(
        self,
        name: str,
        bus: MessageBus,
        conversation_id: str,
    ):
        speaker_uri = HUMAN_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        super().__init__(
            speaker_uri=speaker_uri,
            name=name,
            service_url=f"local://human-{name.lower()}",
            bus=bus,
            conversation_id=conversation_id,
        )
        # UI → agent (user text input)
        self.input_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
        # agent → UI (incoming envelopes for display)
        self.output_queue: asyncio.Queue[Envelope] = asyncio.Queue()

    async def send_text(self, text: str) -> None:
        """Send a text utterance on behalf of the human."""
        envelope = self._make_utterance_envelope(text)
        await self.send_envelope(envelope)

    async def run(self) -> None:
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)

        async def _read_bus():
            """Forward incoming bus envelopes to the output queue."""
            while self._running:
                try:
                    envelope = await asyncio.wait_for(self._queue.get(), timeout=1.0)
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
