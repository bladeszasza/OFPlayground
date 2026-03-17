"""Floor Manager: the OFP conversation coordinator."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from openfloor import (
    BotAgent,
    Capability,
    Conversation,
    DialogEvent,
    Envelope,
    GrantFloorEvent,
    Identification,
    InviteEvent,
    Manifest,
    PublishManifestsEvent,
    RequestFloorEvent,
    RevokeFloorEvent,
    Sender,
    SupportedLayers,
    TextFeature,
    Token,
    UninviteEvent,
    UtteranceEvent,
)

from ofp_playground.bus.message_bus import MessageBus, FLOOR_MANAGER_URI
from ofp_playground.floor.history import ConversationHistory
from ofp_playground.floor.policy import FloorController, FloorPolicy
from ofp_playground.models.artifact import Utterance
from ofp_playground.renderer.terminal import TerminalRenderer

logger = logging.getLogger(__name__)

FLOOR_MANAGER_NAME = "Floor Manager"
FLOOR_MANAGER_URI_STR = FLOOR_MANAGER_URI


def make_floor_manager_manifest() -> Manifest:
    return Manifest(
        identification=Identification(
            speakerUri=FLOOR_MANAGER_URI_STR,
            serviceUrl="local://floor-manager",
            organization="OFP Playground",
            conversationalName=FLOOR_MANAGER_NAME,
            role="convener",
            synopsis="Manages the conversation floor and coordinates agents",
        ),
        capabilities=[
            Capability(
                keyphrases=["floor management", "conversation coordination"],
                descriptions=["Manages multi-party OFP conversations"],
                supportedLayers=SupportedLayers(input=["text"], output=["text"]),
            )
        ],
    )


class FloorManager:
    """Central coordinator for OFP conversations.

    Manages floor control, routes envelopes, and tracks conversation state.
    """

    def __init__(
        self,
        bus: MessageBus,
        policy: FloorPolicy = FloorPolicy.SEQUENTIAL,
        renderer: Optional[TerminalRenderer] = None,
    ):
        self._bus = bus
        self._manifest = make_floor_manager_manifest()
        self._conversation_id = f"conv:{uuid.uuid4()}"
        self._policy = FloorController(policy)
        self._history = ConversationHistory()
        self._renderer = renderer
        self._queue: asyncio.Queue = asyncio.Queue()
        self._agents: dict[str, str] = {}  # speakerUri -> conversationalName
        self._running = False
        self._stop_event = asyncio.Event()
        self._round_count = 0
        self._agents_spoken_this_round: set[str] = set()
        self._director_uri: Optional[str] = None
        self._showrunner_uri: Optional[str] = None
        self._orchestrator_uri: Optional[str] = None
        self._assigned_uri: Optional[str] = None  # currently assigned agent in showrunner_driven mode
        self._spawn_callback: Optional[callable] = None  # set externally after creation
        self._manuscript: list[str] = []  # accumulated accepted chunks (showrunner_driven)
        self._last_worker_text: str = ""  # most recent non-orchestrator utterance text
        self._last_worker_name: str = ""  # speaker name for the above

    @property
    def speaker_uri(self) -> str:
        return FLOOR_MANAGER_URI_STR

    @property
    def queue(self) -> asyncio.Queue:
        return self._queue

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def history(self) -> ConversationHistory:
        return self._history

    @property
    def active_agents(self) -> dict[str, str]:
        return dict(self._agents)

    @property
    def floor_holder(self) -> Optional[str]:
        return self._policy.current_holder

    def register_director(self, speaker_uri: str) -> None:
        """Mark an agent as the narrative director (gets floor at round boundaries)."""
        self._director_uri = speaker_uri

    def register_showrunner(self, speaker_uri: str) -> None:
        """Mark an agent as the show runner (gets floor at round boundaries, after story agents)."""
        self._showrunner_uri = speaker_uri

    def register_orchestrator(self, speaker_uri: str) -> None:
        """Mark an agent as the SHOWRUNNER_DRIVEN orchestrator.

        The orchestrator gets the floor first and after every non-orchestrator utterance.
        Its utterances are parsed for structured directives ([ASSIGN], [REJECT], etc.).
        """
        self._orchestrator_uri = speaker_uri

    async def grant_to(self, speaker_uri: str) -> None:
        """Directly grant floor to a specific agent, bypassing the request queue."""
        self._policy.grant_to(speaker_uri)
        await self._grant_floor(speaker_uri)

    def register_agent(self, speaker_uri: str, name: str) -> None:
        """Register a local agent with the floor manager."""
        self._agents[speaker_uri] = name
        self._policy.add_to_rotation(speaker_uri)
        if self._renderer:
            self._renderer.add_agent(speaker_uri, name)

    def unregister_agent(self, speaker_uri: str) -> None:
        self._agents.pop(speaker_uri, None)
        self._policy.remove_from_rotation(speaker_uri)

    def _make_sender(self) -> Sender:
        return Sender(
            speakerUri=self.speaker_uri,
            serviceUrl="local://floor-manager",
        )

    def _make_conversation(self) -> Conversation:
        return Conversation(id=self._conversation_id)

    async def _send(self, envelope: Envelope) -> None:
        await self._bus.send(envelope)

    async def _grant_floor(self, speaker_uri: str) -> None:
        """Send grantFloor event to the specified agent."""
        from openfloor import To
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[
                GrantFloorEvent(
                    to=To(speakerUri=speaker_uri),
                    reason="Your turn to speak",
                )
            ],
        )
        await self._send(envelope)
        if self._renderer and self._renderer.show_floor_events:
            agent_name = self._agents.get(speaker_uri, speaker_uri)
            self._renderer.show_system_event(f"Floor granted to {agent_name}")

    async def _revoke_floor(self, speaker_uri: str, reason: str = "@timedOut") -> None:
        """Revoke floor from the specified agent."""
        from openfloor import To
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[
                RevokeFloorEvent(
                    to=To(speakerUri=speaker_uri),
                    reason=reason,
                )
            ],
        )
        await self._send(envelope)
        self._policy.revoke_floor()

    async def _handle_utterance(self, envelope: Envelope, event: UtteranceEvent) -> None:
        """Process an utterance: build typed Utterance, add to history, render."""
        sender_uri = envelope.sender.speakerUri if envelope.sender else "unknown"
        sender_name = self._agents.get(sender_uri, sender_uri.split(":")[-1])

        # Extract text and optional media features from the dialogEvent
        text = ""
        media_key: str | None = None
        media_path: str | None = None

        de = getattr(event, "dialogEvent", None)
        if de and de.features:
            text_feat = de.features.get("text")
            if text_feat and text_feat.tokens:
                text = " ".join(t.value for t in text_feat.tokens if t.value)

            for key in ("image", "video", "audio", "3d"):
                feat = de.features.get(key)
                if feat and feat.tokens and feat.tokens[0].value:
                    media_key = key
                    media_path = feat.tokens[0].value
                    break

        # Build typed Utterance and store in history
        if media_key == "image" and media_path:
            utterance = Utterance.from_image(sender_uri, sender_name, text, media_path)
        elif media_key == "video" and media_path:
            utterance = Utterance.from_video(sender_uri, sender_name, text, media_path)
        else:
            utterance = Utterance.from_text(sender_uri, sender_name, text)

        self._history.add(utterance)

        # ----------------------------------------------------------------
        # SHOWRUNNER_DRIVEN: orchestrator utterance → parse its directives
        # ----------------------------------------------------------------
        if self._orchestrator_uri and sender_uri == self._orchestrator_uri:
            if self._renderer:
                self._renderer.show_utterance(
                    sender_uri, sender_name, text,
                    media_key=media_key, media_path=media_path,
                )
            await self._handle_orchestrator_directives(text)
            return

        # Track round completion / floor routing for non-orchestrator paths
        if sender_uri != self.speaker_uri:
            self._agents_spoken_this_round.add(sender_uri)

            if self._orchestrator_uri:
                # SHOWRUNNER_DRIVEN: every worker utterance returns floor to orchestrator
                is_media = any(k in sender_uri for k in ("image", "video", "audio"))
                if not is_media:
                    # Record last worker output for manuscript accumulation on [ACCEPT]
                    self._last_worker_text = text
                    self._last_worker_name = sender_name
                    self._assigned_uri = None  # clear assignment — orchestrator decides next
                    await self.grant_to(self._orchestrator_uri)
                    if self._renderer:
                        self._renderer.show_system_event(
                            f"Floor returned to {self._agents.get(self._orchestrator_uri, 'Orchestrator')}"
                        )
            else:
                # Existing round-boundary logic (showrunner / director / summary)
                text_agents = {
                    uri for uri in self._agents
                    if "image" not in uri and "video" not in uri and "audio" not in uri
                    and uri != self._showrunner_uri
                }
                if text_agents and text_agents.issubset(self._agents_spoken_this_round):
                    self._round_count += 1
                    self._agents_spoken_this_round.clear()
                    if self._showrunner_uri:
                        await self.grant_to(self._showrunner_uri)
                        if self._renderer:
                            self._renderer.show_system_event(
                                f"Round {self._round_count} complete — ShowRunner has the floor"
                            )
                    elif self._director_uri:
                        await self.grant_to(self._director_uri)
                        if self._renderer:
                            self._renderer.show_system_event(
                                f"Round {self._round_count} complete — Director has the floor"
                            )
                    else:
                        await self._inject_round_summary()

        # Display
        if self._renderer:
            self._renderer.show_utterance(
                sender_uri, sender_name, text,
                media_key=media_key, media_path=media_path,
            )

    def _resolve_agent_uri_by_name(self, name: str) -> Optional[str]:
        """Look up a speakerUri by conversational name (case-insensitive)."""
        name_lower = name.lower()
        for uri, agent_name in self._agents.items():
            if agent_name.lower() == name_lower:
                return uri
        return None

    async def _send_directed_utterance(self, text: str, target_uri: Optional[str] = None) -> None:
        """Send a directive utterance, privately if target_uri given (OFP private message)."""
        de = DialogEvent(
            speakerUri=self.speaker_uri,
            id=str(uuid.uuid4()),
            features={"text": TextFeature(tokens=[Token(value=text)])},
        )
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[UtteranceEvent(dialogEvent=de)],
        )
        if target_uri:
            await self._bus.send_private(envelope, target_uri)
        else:
            await self._send(envelope)

    async def _handle_orchestrator_directives(self, text: str) -> None:
        """Parse and execute structured directives emitted by the OrchestratorAgent.

        Directives (one per line):
            [ASSIGN AgentName]: task
            [ACCEPT]
            [REJECT AgentName]: reason
            [KICK AgentName]
            [SPAWN -provider hf -name X -system Y -model Z]
            [TASK_COMPLETE]
        """
        import re

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue

            # [ASSIGN AgentName]: task  — set assigned agent, clear stale queue, grant floor
            m = re.match(r"\[ASSIGN\s+(.+?)\]\s*:\s*(.+)", line, re.IGNORECASE)
            if m:
                target_name = m.group(1).strip()
                task = m.group(2).strip()
                target_uri = self._resolve_agent_uri_by_name(target_name)
                if target_uri:
                    # Track who is assigned so _handle_request_floor can filter others
                    self._assigned_uri = target_uri
                    # Flush any stale queue entries from agents that saw the orchestrator utterance
                    self._policy._request_queue.clear()
                    # Build directive, injecting manuscript context if any has been accepted
                    directive = f"[DIRECTIVE for {target_name}]: {task}"
                    if self._manuscript:
                        manuscript_text = "\n\n".join(self._manuscript)
                        directive += (
                            f"\n\n--- STORY SO FAR ({sum(len(c.split()) for c in self._manuscript)} words) ---\n"
                            f"{manuscript_text}\n"
                            f"--- END OF STORY SO FAR ---\n"
                            f"Continue directly from where the story left off."
                        )
                    # Grant before sending directive; use private routing (OFP private message)
                    await self.grant_to(target_uri)
                    await self._send_directed_utterance(directive, target_uri=target_uri)
                    if self._renderer:
                        self._renderer.show_system_event(
                            f"[Orchestrator → {target_name}]: {task[:60]}"
                        )
                else:
                    logger.warning("Orchestrator [ASSIGN]: agent '%s' not found", target_name)
                continue

            # [ACCEPT]  — append last worker output to shared manuscript
            if re.match(r"\[ACCEPT\]", line, re.IGNORECASE):
                if self._last_worker_text:
                    self._manuscript.append(self._last_worker_text)
                    self._last_worker_text = ""
                if self._renderer:
                    word_count = sum(len(chunk.split()) for chunk in self._manuscript)
                    self._renderer.show_system_event(
                        f"[Orchestrator] Accepted — manuscript: {word_count} words"
                    )
                continue

            # [REJECT AgentName]: reason
            m = re.match(r"\[REJECT\s+(.+?)\]\s*:\s*(.+)", line, re.IGNORECASE)
            if m:
                target_name = m.group(1).strip()
                reason = m.group(2).strip()
                target_uri = self._resolve_agent_uri_by_name(target_name)
                if target_uri:
                    self._assigned_uri = target_uri
                    self._policy._request_queue.clear()
                    await self.grant_to(target_uri)
                    await self._send_directed_utterance(
                        f"[DIRECTIVE for {target_name}]: Revision requested — {reason}",
                        target_uri=target_uri,
                    )
                    if self._renderer:
                        self._renderer.show_system_event(
                            f"[Orchestrator] Rejected {target_name}: {reason[:60]}"
                        )
                else:
                    logger.warning("Orchestrator [REJECT]: agent '%s' not found", target_name)
                continue

            # [KICK AgentName]
            m = re.match(r"\[KICK\s+(.+?)\]", line, re.IGNORECASE)
            if m:
                target_name = m.group(1).strip()
                target_uri = self._resolve_agent_uri_by_name(target_name)
                if target_uri:
                    self.unregister_agent(target_uri)
                    await self._bus.unregister(target_uri)
                    if self._renderer:
                        self._renderer.show_system_event(
                            f"[Orchestrator] Kicked {target_name}"
                        )
                else:
                    logger.warning("Orchestrator [KICK]: agent '%s' not found", target_name)
                continue

            # [SPAWN spec]
            m = re.match(r"\[SPAWN\s+(.+?)\]", line, re.IGNORECASE)
            if m:
                spec_str = m.group(1).strip()
                if self._spawn_callback:
                    try:
                        await self._spawn_callback(spec_str)
                        if self._renderer:
                            self._renderer.show_system_event(
                                f"[Orchestrator] Spawned: {spec_str[:60]}"
                            )
                    except Exception as e:
                        logger.error("Orchestrator [SPAWN] failed: %s", e, exc_info=True)
                        if self._renderer:
                            self._renderer.show_system_event(f"[Orchestrator] Spawn failed: {e}")
                else:
                    logger.warning("Orchestrator [SPAWN]: no spawn_callback registered")
                continue

            # [TASK_COMPLETE]
            if re.match(r"\[TASK_COMPLETE\]", line, re.IGNORECASE):
                if self._renderer:
                    self._renderer.show_system_event("[Orchestrator] Task complete — stopping")
                self.stop()
                return

    async def _inject_round_summary(self) -> None:
        """Inject a director note between rounds to keep agents aligned."""
        recent = self._history.recent(len(self._agents) + 2)

        recap_parts = []
        for entry in recent:
            if entry.speaker_uri == self.speaker_uri:
                continue
            if "image" in entry.speaker_uri or "video" in entry.speaker_uri:
                continue
            snippet = entry.text[:80].replace("\n", " ").strip()
            if snippet:
                recap_parts.append(f"  {entry.speaker_name}: {snippet}...")

        if not recap_parts:
            return

        director_text = (
            f"[DIRECTOR — Round {self._round_count} complete]\n"
            f"What just happened:\n" + "\n".join(recap_parts) + "\n\n"
            f"IMPORTANT FOR ALL AGENTS: Build on what was said above. "
            f"Do NOT contradict or ignore other agents' contributions. "
            f"Do NOT invent new characters or plot threads that conflict with what was just established. "
            f"React to and extend the existing narrative."
        )

        de = DialogEvent(
            speakerUri=self.speaker_uri,
            id=str(uuid.uuid4()),
            features={"text": TextFeature(tokens=[Token(value=director_text)])},
        )
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[UtteranceEvent(dialogEvent=de)],
        )
        await self._send(envelope)

        if self._renderer:
            self._renderer.show_system_event(
                f"[Director] Round {self._round_count} summary injected"
            )

    async def _handle_request_floor(self, envelope: Envelope, event: RequestFloorEvent) -> None:
        sender_uri = envelope.sender.speakerUri if envelope.sender else "unknown"

        # In showrunner_driven mode only the assigned agent may request floor —
        # all other requests are silently dropped to prevent queue pile-up.
        if self._orchestrator_uri:
            if sender_uri not in (self._orchestrator_uri, self._assigned_uri):
                return

        reason = getattr(event, "reason", "") or ""
        granted = self._policy.request_floor(sender_uri, reason)

        if granted:
            await self._grant_floor(sender_uri)
        else:
            if self._renderer and self._renderer.show_floor_events:
                agent_name = self._agents.get(sender_uri, sender_uri)
                self._renderer.show_system_event(f"{agent_name} is waiting for the floor")

    async def _handle_yield_floor(self, envelope: Envelope) -> None:
        sender_uri = envelope.sender.speakerUri if envelope.sender else "unknown"
        next_holder = self._policy.yield_floor(sender_uri)
        if next_holder:
            await self._grant_floor(next_holder)

    async def process_envelope(self, envelope: Envelope) -> None:
        """Process an incoming envelope from the bus."""
        sender_uri = envelope.sender.speakerUri if envelope.sender else "unknown"

        for event in (envelope.events or []):
            event_type = event.eventType if hasattr(event, "eventType") else str(type(event).__name__)

            if event_type == "utterance":
                await self._handle_utterance(envelope, event)
            elif event_type == "requestFloor":
                await self._handle_request_floor(envelope, event)
            elif event_type == "yieldFloor":
                await self._handle_yield_floor(envelope)
            elif event_type == "bye":
                self.unregister_agent(sender_uri)
                await self._bus.unregister(sender_uri)
                if self._renderer:
                    agent_name = self._agents.get(sender_uri, sender_uri)
                    self._renderer.show_system_event(f"{agent_name} left the conversation")
            else:
                logger.debug("Floor manager ignoring event type: %s", event_type)

    async def run(self) -> None:
        """Main floor manager loop."""
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)

        if self._renderer:
            self._renderer.show_system_event(
                f"Conversation started (ID: {self._conversation_id[:8]}...)"
            )

        try:
            while self._running:
                try:
                    envelope = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    await self.process_envelope(envelope)
                except asyncio.TimeoutError:
                    if self._stop_event.is_set():
                        break
                except Exception as e:
                    logger.error("Floor manager error: %s", e, exc_info=True)
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
