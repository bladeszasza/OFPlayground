"""Async in-process message bus for routing OFP envelopes between agents."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from openfloor import Envelope

if TYPE_CHECKING:
    from ofp_playground.trace.collector import TraceCollector

logger = logging.getLogger(__name__)

FLOOR_MANAGER_URI = "tag:ofp-playground.local,2025:floor-manager"


class MessageBus:
    """Routes OFP envelopes between agents within the playground process.

    Each agent registers with their speakerUri and an asyncio.Queue.
    The floor manager always receives a copy of every message.
    """

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._departed: set[str] = set()
        self._lock = asyncio.Lock()
        self._collector: TraceCollector | None = None
        self._collector_breakout_id: str | None = None
        self._collector_scope_id: str | None = None
        self._collector_scope_kind: str = "main"
        self._collector_parent_conversation_id: str | None = None

    def set_collector(
        self,
        collector: TraceCollector,
        breakout_id: str | None = None,
        *,
        scope_id: str | None = None,
        scope_kind: str = "main",
        parent_conversation_id: str | None = None,
    ) -> None:
        """Attach a trace collector that receives a copy of all routed envelopes."""
        self._collector = collector
        self._collector_breakout_id = breakout_id
        self._collector_scope_id = scope_id or breakout_id
        self._collector_scope_kind = self._resolve_scope_kind(scope_kind, self._collector_scope_id)
        if self._collector_scope_kind == "main":
            self._collector_parent_conversation_id = None
        else:
            self._collector_parent_conversation_id = parent_conversation_id or collector.conversation_id

    @staticmethod
    def _resolve_scope_kind(scope_kind: str, scope_id: str | None) -> str:
        if scope_kind != "main":
            return scope_kind
        if not scope_id:
            return "main"
        if scope_id.startswith("breakout:"):
            return "breakout"
        if scope_id.startswith("coding:"):
            return "coding"
        return "nested"

    async def register(self, speaker_uri: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._queues[speaker_uri] = queue
            self._departed.discard(speaker_uri)
            logger.debug("Registered agent: %s", speaker_uri)

    async def unregister(self, speaker_uri: str) -> None:
        async with self._lock:
            self._queues.pop(speaker_uri, None)
            self._departed.add(speaker_uri)
            logger.debug("Unregistered agent: %s", speaker_uri)

    async def send(self, envelope: Envelope) -> None:
        """Route envelope based on 'to' fields in events.

        If any event has a 'to' speakerUri, only that agent + floor manager receive it.
        Otherwise broadcast to all except sender. Floor manager always gets a copy.
        """
        sender_uri = envelope.sender.speakerUri if envelope.sender else None
        if sender_uri in self._departed:
            return

        # Determine target URIs
        targeted_uris: set[str] = set()
        has_target = False

        for event in (envelope.events or []):
            if hasattr(event, "to") and event.to and event.to.speakerUri:
                targeted_uris.add(event.to.speakerUri)
                has_target = True

        async with self._lock:
            all_uris = set(self._queues.keys())

        if has_target:
            recipients = targeted_uris
        else:
            # Broadcast: all except sender
            recipients = all_uris - ({sender_uri} if sender_uri else set())

        # Floor manager always gets a copy (except if it's the sender)
        if FLOOR_MANAGER_URI != sender_uri:
            recipients.add(FLOOR_MANAGER_URI)

        async with self._lock:
            if sender_uri in self._departed:
                return
            outcomes: dict[str, str] = {}
            for uri in recipients:
                outcomes[uri] = "unregistered"
                if uri in self._queues:
                    try:
                        self._queues[uri].put_nowait(envelope)
                        outcomes[uri] = "queued"
                    except asyncio.QueueFull:
                        outcomes[uri] = "dropped-queue-full"
                        logger.warning("Queue full for agent %s, dropping envelope", uri)

        if self._collector is not None:
            self._collector.record(
                envelope=envelope,
                recipients=recipients,
                is_private=False,
                breakout_id=self._collector_breakout_id,
                scope_id=self._collector_scope_id,
                scope_kind=self._collector_scope_kind,
                parent_conversation_id=self._collector_parent_conversation_id,
                delivery_outcomes=outcomes,
            )

    async def send_private(self, envelope: Envelope, target_uri: str) -> None:
        """Deliver envelope only to target_uri and the floor manager (OFP private message).

        Use this for directed utterances that should not be seen by other agents.
        """
        sender_uri = envelope.sender.speakerUri if envelope.sender else None
        if sender_uri in self._departed:
            return
        async with self._lock:
            if sender_uri in self._departed:
                return
            recipients = {target_uri, FLOOR_MANAGER_URI}
            sender_uri = envelope.sender.speakerUri if envelope.sender else None
            if sender_uri:
                recipients.discard(sender_uri)
            outcomes: dict[str, str] = {}
            for uri in recipients:
                outcomes[uri] = "unregistered"
                if uri in self._queues:
                    try:
                        self._queues[uri].put_nowait(envelope)
                        outcomes[uri] = "queued"
                    except asyncio.QueueFull:
                        outcomes[uri] = "dropped-queue-full"
                        logger.warning("Queue full for agent %s, dropping private envelope", uri)

        if self._collector is not None:
            self._collector.record(
                envelope=envelope,
                recipients=recipients,
                is_private=True,
                breakout_id=self._collector_breakout_id,
                scope_id=self._collector_scope_id,
                scope_kind=self._collector_scope_kind,
                parent_conversation_id=self._collector_parent_conversation_id,
                delivery_outcomes=outcomes,
            )

    @property
    def registered_agents(self) -> list[str]:
        return list(self._queues.keys())
