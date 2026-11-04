"""Remote OFP agent: HTTP proxy for external OFP agents."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import httpx
from openfloor import Envelope

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import FLOOR_MANAGER_URI, MessageBus

logger = logging.getLogger(__name__)

REMOTE_URI_TEMPLATE = "tag:ofp-playground.local,2025:remote-{name}"

# Known public OFP agents from https://openfloor.dev/agent-registry
# slug: (display_name, service_url, synopsis)
KNOWN_REMOTE_AGENTS: dict[str, tuple[str, str, str]] = {
    "polly": (
        "Polly the Parrot",
        "https://parrot-agent.openfloor.dev/",
        "Echoes back any text message with a parrot emoji",
    ),
    "arxiv": (
        "ArXiv Research Specialist",
        "https://krsnzn5xm3.us-east-1.awsapprunner.com/",
        "Find and analyze scientific papers on arXiv",
    ),
    "github": (
        "GitHub Technology Analyst",
        "https://p23fimjxfm.us-east-1.awsapprunner.com/",
        "Analyze GitHub repositories for technology adoption trends",
    ),
    "sec": (
        "SEC Financial Analyst",
        "https://wzy3kbgcpr.us-east-1.awsapprunner.com/",
        "Research SEC filings and financial data for public companies",
    ),
    "web-search": (
        "Web Search Specialist",
        "https://pszvphapmr.us-east-1.awsapprunner.com/",
        "Search the web for current information, news, and guides",
    ),
    "wikipedia": (
        "Wikipedia Research Specialist",
        "https://yahandhjjf.us-east-1.awsapprunner.com/",
        "Encyclopedic research and authoritative factual information",
    ),
    "wiki": (
        "Wikipedia Research Specialist",
        "https://yahandhjjf.us-east-1.awsapprunner.com/",
        "Encyclopedic research and authoritative factual information",
    ),
    "stella": (
        "Stella",
        "https://openvoice-stella.vercel.app",
        "Shows astronomical images from NASA's image libraries",
    ),
    "verity": (
        "Verity",
        "https://secondassistant.pythonanywhere.com/verity/",
        "Detects and mitigates hallucinations, fact-checking specialist",
    ),
    "profanity": (
        "Content Moderator Sentinel",
        "https://bladeszasza-ofpbadword.hf.space/ofp",
        "Automated content moderation and profanity detection",
    ),
}


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
        self._pending_text: str | None = None
        self._pending_sender_uri: str | None = None

    def _now_iso(self) -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def _make_base_payload(self, sender_uri: str, sender_service_url: str = "local://agent") -> dict:
        return {
            "openFloor": {
                "schema": {"version": "1.0.0"},
                "conversation": {"id": self._conversation_id},
                "sender": {
                    "speakerUri": sender_uri,
                    "serviceUrl": sender_service_url,
                },
                "events": [],
            }
        }

    async def _fetch_and_publish_manifest(self) -> None:
        """POST inviteEvent to the remote; if it responds with a manifest, relay it to the local bus."""
        payload = self._make_base_payload(FLOOR_MANAGER_URI, "local://floor-manager")
        payload["openFloor"]["events"] = [{
            "eventType": "invite",
            "parameters": {
                "to": {
                    "speakerUri": self.speaker_uri,
                    "serviceUrl": self._remote_url,
                }
            },
        }]
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                resp = await client.post(
                    self._remote_url, json=payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                body = resp.json()

            of_data = body.get("openFloor", {})
            for event in of_data.get("events", []):
                if event.get("eventType") != "publishManifests":
                    continue
                servicing = event.get("parameters", {}).get("servicingManifests", [])
                if not servicing:
                    continue
                from openfloor import Conversation, Parameters, PublishManifestsEvent, Sender
                relay = Envelope(
                    sender=Sender(speakerUri=self.speaker_uri, serviceUrl=self._remote_url),
                    conversation=Conversation(id=self._conversation_id),
                    events=[PublishManifestsEvent(
                        parameters=Parameters({
                            "servicingManifests": servicing,
                            "discoveryManifests": [],
                        })
                    )],
                )
                await self.send_envelope(relay)
                logger.debug("[%s] Manifest relayed from remote", self._name)
                return

            logger.debug("[%s] Remote returned no manifest in invite response", self._name)
        except Exception:
            logger.warning('[%s] Could not fetch manifest from remote: see traceback', self._name, exc_info=True)

    async def _handle_invite(self) -> None:
        """OFP invite: acknowledge and fetch remote manifest.

        Floor is NOT requested here — the remote agent only speaks when it has
        an utterance to forward, so request_floor is deferred to _handle_utterance.
        """
        from openfloor import Conversation, Event, Sender, To
        accept_env = Envelope(
            sender=Sender(speakerUri=self.speaker_uri, serviceUrl=self._remote_url),
            conversation=Conversation(id=self._conversation_id),
            events=[Event(
                eventType="acceptInvite",
                to=To(speakerUri=FLOOR_MANAGER_URI),
                reason="Ready to participate",
            )],
        )
        await self.send_envelope(accept_env)
        await self._fetch_and_publish_manifest()

    async def _post_to_remote(self, text: str, sender_uri: str) -> str | None:
        """POST an utterance to the remote OFP endpoint, return response text."""
        payload = self._make_base_payload(sender_uri)
        payload["openFloor"]["events"] = [{
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
        }]

        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
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
        except Exception:
            logger.exception('[%s] Remote error: see traceback', self._name)

        return None

    def _should_respond(self, sender_uri: str, text: str) -> tuple[bool, str]:
        """Decide whether to respond to this utterance and return the task text to send.

        Rules:
        - Other remote agents → never (prevents cascade loops like Polly↔Wikipedia)
        - Floor manager → only for [DIRECTIVE for <our name>] (showrunner_driven tasks);
          director notes and round summaries are ignored
        - Humans and local LLM agents → always, pass text through unchanged
        """
        import re

        if "remote-" in sender_uri:
            return False, ""

        if sender_uri == FLOOR_MANAGER_URI:
            m = re.match(r"\[DIRECTIVE\s+for\s+([^\]]+)\]\s*:\s*(.+)", text, re.DOTALL | re.IGNORECASE)
            if m and m.group(1).strip().lower() == self._name.lower():
                return True, m.group(2).strip()
            return False, ""

        return True, text

    async def _handle_utterance(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return
        raw_text = self._extract_text_from_envelope(envelope)
        if not raw_text:
            return
        respond, task_text = self._should_respond(sender_uri, raw_text)
        if not respond:
            return
        self._pending_text = task_text
        self._pending_sender_uri = sender_uri
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
        except Exception:
            logger.exception('[%s] Floor grant error: see traceback', self._name)
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
            elif event_type == "invite":
                await self._handle_invite()
            elif event_type == "uninvite":
                logger.info("[%s] received uninvite — stopping", self._name)
                self._running = False

    async def run(self) -> None:
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)
        # Floor request is deferred until the floor manager sends an invite.

        try:
            while self._running:
                try:
                    envelope = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    await self._dispatch(envelope)
                except TimeoutError:
                    continue
                except Exception:
                    logger.exception('[%s] error: see traceback', self._name)
        finally:
            self._running = False
