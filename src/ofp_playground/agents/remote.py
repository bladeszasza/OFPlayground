"""Remote OFP agent: HTTP proxy for external OFP agents."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from openfloor import Envelope

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

REMOTE_URI_TEMPLATE = "tag:ofp-playground.local,2025:remote-{name}"


class RemoteOFPAgent(BasePlaygroundAgent):
    """Proxy for an external OFP agent accessible via HTTP POST.

    Participates in floor protocol: listens for utterances, requests the floor,
    POSTs to the remote in standard OFP dict format, and publishes the response.
    """

    def __init__(
        self,
        service_url: str,
        name: str,
        bus: MessageBus,
        conversation_id: str,
        timeout: float = 30.0,
    ):
        speaker_uri = REMOTE_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        super().__init__(
            speaker_uri=speaker_uri,
            name=name,
            service_url=service_url,
            bus=bus,
            conversation_id=conversation_id,
        )
        self._remote_url = service_url
        self._timeout = timeout
        self._has_floor = False
        self._pending_text: Optional[str] = None
        self._pending_sender_uri: Optional[str] = None

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    async def _post_to_remote(self, text: str, sender_uri: str) -> Optional[str]:
        """POST an utterance to the remote OFP endpoint, return response text."""
        payload = {
            "openFloor": {
                "schema": {"version": "1.0.0"},
                "conversation": {"id": self._conversation_id},
                "sender": {
                    "speakerUri": sender_uri,
                    "serviceUrl": self._service_url,
                },
                "events": [{
                    "eventType": "utterance",
                    "parameters": {
                        "dialogEvent": {
                            "id": f"de:{uuid.uuid4()}",
                            "speakerUri": sender_uri,
                            "span": {"startTime": self._now_iso()},
                            "features": {
                                "text": {
                                    "mimeType": "text/plain",
                                    "tokens": [{"value": text}],
                                }
                            },
                        }
                    },
                }],
            }
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self._remote_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                body = resp.json()

            of_data = body.get("openFloor", {})
            for event in of_data.get("events", []):
                if event.get("eventType") != "utterance":
                    continue
                # Support both formats: parameters.dialogEvent and direct dialogEvent
                de = event.get("parameters", {}).get("dialogEvent") or event.get("dialogEvent", {})
                tokens = de.get("features", {}).get("text", {}).get("tokens", [])
                response_text = " ".join(t.get("value", "") for t in tokens).strip()
                if response_text:
                    return response_text

        except httpx.TimeoutException:
            logger.warning("[%s] Remote timed out after %ss", self._name, self._timeout)
        except Exception as e:
            logger.error("[%s] Remote error: %s", self._name, e)

        return None

    async def _handle_utterance(self, envelope: Envelope) -> None:
        if self._get_sender_uri(envelope) == self.speaker_uri:
            return
        text = self._extract_text_from_envelope(envelope)
        if not text:
            return
        self._pending_text = text
        self._pending_sender_uri = self._get_sender_uri(envelope)
        if not self._has_floor:
            await self.request_floor("responding to conversation")

    async def _handle_grant_floor(self) -> None:
        self._has_floor = True
        try:
            if self._pending_text:
                response_text = await self._post_to_remote(
                    self._pending_text,
                    self._pending_sender_uri or "unknown",
                )
                if response_text:
                    await self.send_envelope(self._make_utterance_envelope(response_text))
        except Exception as e:
            logger.error("[%s] Floor grant error: %s", self._name, e)
        finally:
            self._has_floor = False
            self._pending_text = None
            self._pending_sender_uri = None
            await self.yield_floor()

    async def _dispatch(self, envelope: Envelope) -> None:
        for event in (envelope.events or []):
            event_type = getattr(event, "eventType", type(event).__name__)
            if event_type == "utterance":
                await self._handle_utterance(envelope)
            elif event_type == "grantFloor":
                await self._handle_grant_floor()
            elif event_type == "revokeFloor":
                self._has_floor = False

    async def run(self) -> None:
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)
        await self.request_floor("Ready to participate")

        try:
            while self._running:
                try:
                    envelope = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    await self._dispatch(envelope)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error("[%s] error: %s", self._name, e, exc_info=True)
        finally:
            self._running = False
