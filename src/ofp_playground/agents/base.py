"""Base agent class for all OFP Playground agents."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from openfloor import (
    Capability,
    Conversation,
    DialogEvent,
    Envelope,
    Identification,
    Manifest,
    PublishManifestsEvent,
    RequestFloorEvent,
    Sender,
    SupportedLayers,
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
        self._pending_floor_request: bool = False  # prevent duplicate floor requests
        # Retry / timeout settings — overridden per-agent via CLI flags
        self._timeout: Optional[float] = None  # seconds per API call; None = no timeout
        self._max_retries: int = 0             # extra attempts after first failure
        self._retry_delay: float = 2.0         # base back-off seconds (doubles each attempt)

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

    async def send_private_utterance(self, text: str, target_uri: str) -> None:
        """Send a private utterance visible only to target_uri and the floor manager."""
        envelope = self._make_utterance_envelope(text)
        await self._bus.send_private(envelope, target_uri)

    async def request_floor(self, reason: str = "") -> None:
        if self._pending_floor_request:
            return
        self._pending_floor_request = True
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[RequestFloorEvent(reason=reason)],
        )
        await self.send_envelope(envelope)

    async def yield_floor(self, reason: str = "@complete") -> None:
        self._pending_floor_request = False
        from openfloor import Event
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[Event(eventType="yieldFloor", reason=reason)],
        )
        await self.send_envelope(envelope)

    def _make_media_utterance_envelope(
        self,
        text: str,
        media_key: str,
        mime_type: str,
        file_path: str,
    ) -> Envelope:
        """Build an utterance envelope with both a text feature and a media feature.

        The text feature carries a verbalizable description (required by OFP).
        The media feature carries the file path via Token.value.
        """
        dialog_event = DialogEvent(
            id=str(uuid.uuid4()),
            speakerUri=self._speaker_uri,
            features={
                "text": TextFeature(
                    mimeType="text/plain",
                    tokens=[Token(value=text)],
                ),
                media_key: TextFeature(
                    mimeType=mime_type,
                    tokens=[Token(value=file_path)],
                ),
            },
        )
        return Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[UtteranceEvent(dialogEvent=dialog_event)],
        )

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

    def _build_manifest(self) -> Manifest:
        """Build an OFP manifest for this agent. Override in subclasses for richer info."""
        return Manifest(
            identification=Identification(
                speakerUri=self._speaker_uri,
                serviceUrl=self._service_url,
                conversationalName=self._name,
                role="AI agent",
            ),
            capabilities=[
                Capability(
                    keyphrases=["text-generation"],
                    descriptions=[f"AI agent: {self._name}"],
                    supportedLayers=SupportedLayers(input=["text"], output=["text"]),
                )
            ],
        )

    async def _publish_manifest(self) -> None:
        """Send a publishManifests event so the floor manager and others can see capabilities."""
        from openfloor import Parameters
        manifest = self._build_manifest()
        event = PublishManifestsEvent(
            parameters=Parameters({
                "servicingManifests": [dict(manifest)],
                "discoveryManifests": [],
            })
        )
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[event],
        )
        await self.send_envelope(envelope)

    @staticmethod
    def _is_retryable_error(err_str: str) -> bool:
        """Return True for transient errors worth retrying (rate-limits, overload, 5xx)."""
        low = err_str.lower()
        return any(k in low for k in (
            "429", "rate", "quota", "resource_exhausted",
            "503", "502", "504", "overload", "timeout",
        ))

    async def _call_with_retry(self, coro_fn):
        """Call coro_fn() (a zero-arg callable returning a coroutine) with timeout + retry.

        Uses self._timeout (seconds, None = unlimited) and self._max_retries (0 = single attempt).
        Retryable errors get exponential back-off: delay * 2**attempt, capped at 30 s.
        Non-retryable exceptions are re-raised immediately.
        When all attempts are exhausted the last exception is re-raised.
        """
        last_exc: BaseException = RuntimeError("no attempts made")
        total_attempts = self._max_retries + 1
        for attempt in range(total_attempts):
            try:
                if self._timeout is not None:
                    return await asyncio.wait_for(coro_fn(), timeout=self._timeout)
                return await coro_fn()
            except asyncio.TimeoutError as exc:
                last_exc = exc
                logger.warning(
                    "[%s] call timed out after %.1fs (attempt %d/%d)",
                    self._name, self._timeout, attempt + 1, total_attempts,
                )
            except Exception as exc:
                last_exc = exc
                if not self._is_retryable_error(str(exc)):
                    raise
                logger.warning(
                    "[%s] retryable error (attempt %d/%d): %s",
                    self._name, attempt + 1, total_attempts,
                    str(exc).split("\n")[0][:120],
                )
            if attempt + 1 < total_attempts:
                delay = min(self._retry_delay * (2 ** attempt), 30.0)
                logger.info("[%s] retrying in %.1fs...", self._name, delay)
                await asyncio.sleep(delay)
        raise last_exc

    async def run(self) -> None:
        """Main agent loop. Override in subclasses."""
        raise NotImplementedError

    def stop(self) -> None:
        self._running = False
