"""Base agent class for all OFP Playground agents."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from openfloor import (
    Conversation,
    DialogEvent,
    Envelope,
    RequestFloorEvent,
    Sender,
    TextFeature,
    Token,
    UtteranceEvent,
)

from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)


class BasePlaygroundAgent:
    """Base class for all playground agents.

    Provides async message bus integration, conversation history tracking,
    and floor request/yield helpers.
    """

    def __init__(
        self,
        speaker_uri: str,
        name: str,
        service_url: str,
        bus: MessageBus,
        conversation_id: str,
    ):
        self._speaker_uri = speaker_uri
        self._name = name
        self._service_url = service_url
        self._bus = bus
        self._conversation_id = conversation_id
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._conversation_history: list[dict] = []

    @property
    def speaker_uri(self) -> str:
        return self._speaker_uri

    @property
    def name(self) -> str:
        return self._name

    @property
    def queue(self) -> asyncio.Queue:
        return self._queue

    def _make_sender(self) -> Sender:
        return Sender(speakerUri=self._speaker_uri, serviceUrl=self._service_url)

    def _make_conversation(self) -> Conversation:
        return Conversation(id=self._conversation_id)

    async def send_envelope(self, envelope: Envelope) -> None:
        await self._bus.send(envelope)

    async def request_floor(self, reason: str = "") -> None:
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[RequestFloorEvent(reason=reason)],
        )
        await self.send_envelope(envelope)

    async def yield_floor(self, reason: str = "@complete") -> None:
        from openfloor import Event
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[Event(eventType="yieldFloor", reason=reason)],
        )
        await self.send_envelope(envelope)

    def _make_utterance_envelope(self, text: str) -> Envelope:
        dialog_event = DialogEvent(
            id=str(uuid.uuid4()),
            speakerUri=self._speaker_uri,
            features={
                "text": TextFeature(
                    mimeType="text/plain",
                    tokens=[Token(value=text)],
                )
            },
        )
        return Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[UtteranceEvent(dialogEvent=dialog_event)],
        )

    def _extract_text_from_envelope(self, envelope: Envelope) -> Optional[str]:
        for event in (envelope.events or []):
            # UtteranceEvent stores dialogEvent directly
            de = getattr(event, "dialogEvent", None)
            if de and hasattr(de, "features") and de.features:
                text_feat = de.features.get("text")
                if text_feat and hasattr(text_feat, "tokens") and text_feat.tokens:
                    return " ".join(
                        t.value for t in text_feat.tokens if hasattr(t, "value") and t.value
                    )
        return None

    def _get_sender_uri(self, envelope: Envelope) -> str:
        if envelope.sender:
            return envelope.sender.speakerUri or "unknown"
        return "unknown"

    async def run(self) -> None:
        """Main agent loop. Override in subclasses."""
        raise NotImplementedError

    def stop(self) -> None:
        self._running = False
