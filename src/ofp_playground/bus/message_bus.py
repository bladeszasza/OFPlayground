"""Async in-process message bus for routing OFP envelopes between agents."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from openfloor import Envelope

logger = logging.getLogger(__name__)

FLOOR_MANAGER_URI = "tag:ofp-playground.local,2025:floor-manager"


class MessageBus:
    """Routes OFP envelopes between agents within the playground process.

    Each agent registers with their speakerUri and an asyncio.Queue.
    The floor manager always receives a copy of every message.
    """

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    async def register(self, speaker_uri: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._queues[speaker_uri] = queue
            logger.debug("Registered agent: %s", speaker_uri)

    async def unregister(self, speaker_uri: str) -> None:
        async with self._lock:
            self._queues.pop(speaker_uri, None)
            logger.debug("Unregistered agent: %s", speaker_uri)

    async def send(self, envelope: Envelope) -> None:
        """Route envelope based on 'to' fields in events.

        If any event has a 'to' speakerUri, only that agent + floor manager receive it.
        Otherwise broadcast to all except sender. Floor manager always gets a copy.
        """
        sender_uri = envelope.sender.speakerUri if envelope.sender else None

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
            for uri in recipients:
                if uri in self._queues:
                    try:
                        self._queues[uri].put_nowait(envelope)
                    except asyncio.QueueFull:
                        logger.warning("Queue full for agent %s, dropping envelope", uri)

    async def send_private(self, envelope: Envelope, target_uri: str) -> None:
        """Deliver envelope only to target_uri and the floor manager (OFP private message).

        Use this for directed utterances that should not be seen by other agents.
        """
        async with self._lock:
            recipients = {target_uri, FLOOR_MANAGER_URI}
            sender_uri = envelope.sender.speakerUri if envelope.sender else None
            if sender_uri:
                recipients.discard(sender_uri)
            for uri in recipients:
                if uri in self._queues:
                    try:
                        self._queues[uri].put_nowait(envelope)
                    except asyncio.QueueFull:
                        logger.warning("Queue full for agent %s, dropping private envelope", uri)

    @property
    def registered_agents(self) -> list[str]:
        return list(self._queues.keys())
