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
from ofp_playground.config.output import SessionOutputManager
from ofp_playground.floor.history import ConversationHistory
from ofp_playground.floor.policy import FloorController, FloorPolicy
from ofp_playground.memory.store import MemoryStore
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
        self._output = SessionOutputManager(self._conversation_id)
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
        self._orchestrator_idle_grants: int = 0  # consecutive floor grants with no productive action
        self._skip_next_orchestrator_yield: bool = False  # absorb stale yieldFloor after breakout re-grant
        self._breakout_callback: Optional[callable] = None  # set externally for breakout sessions
        self._pending_breakout_file = None  # Path to last breakout artifact — injected into next ASSIGN
        self._manuscript: list[str] = []  # accumulated accepted chunks (showrunner_driven)
        self._last_worker_text: str = ""  # most recent non-orchestrator utterance text
        self._last_worker_name: str = ""  # speaker name for the above
        self._manifests: dict[str, "Manifest"] = {}  # speakerUri → Manifest
        self._memory_store: MemoryStore = MemoryStore()  # ephemeral session memory

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
    def output(self) -> SessionOutputManager:
        return self._output

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

    async def invite_agent(self, speaker_uri: str, service_url: str = "local://agent") -> None:
        """Send an OFP InviteEvent to a newly spawned agent."""
        from openfloor import To
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[
                InviteEvent(
                    to=To(speakerUri=speaker_uri, serviceUrl=service_url),
                )
            ],
        )
        await self._send(envelope)

    async def send_uninvite(self, speaker_uri: str, reason: str = "@complete") -> None:
        """Send an OFP UninviteEvent to remove an agent from the conversation."""
        from openfloor import To
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[
                UninviteEvent(
                    to=To(speakerUri=speaker_uri),
                    reason=reason,
                )
            ],
        )
        await self._send(envelope)

    def register_agent(self, speaker_uri: str, name: str) -> None:
        """Register a local agent with the floor manager."""
        self._agents[speaker_uri] = name
        self._policy.add_to_rotation(speaker_uri)
        if self._renderer:
            self._renderer.add_agent(speaker_uri, name)

    def unregister_agent(self, speaker_uri: str) -> None:
        self._agents.pop(speaker_uri, None)
        self._manifests.pop(speaker_uri, None)
        self._policy.remove_from_rotation(speaker_uri)

    def store_manifest(self, speaker_uri: str, manifest: Manifest) -> None:
        """Store a manifest published by an agent."""
        self._manifests[speaker_uri] = manifest

    def find_agent_by_manifest(self, name: str, task_type: str) -> Optional[tuple[str, Manifest]]:
        """Return (uri, manifest) of an existing agent that matches the spawn request.

        Matching rules (in order):
        1. Exact name match (conversationalName, case-insensitive) — definitive duplicate.
        2. Same task-type keyphrase AND same output layer — capability already covered.

        Returns None if no existing agent covers the requested role.
        """
        name_lower = name.lower()
        task_lower = task_type.lower().replace("_", "-")

        # Determine the output layer(s) implied by the requested task type
        _task_output: dict[str, list[str]] = {
            "text-to-image": ["image"],
            "image-generation": ["image"],
            "text-to-video": ["video"],
            "video-generation": ["video"],
            "text-to-audio": ["audio"],
        }
        requested_outputs = _task_output.get(task_lower, [])

        name_match: Optional[tuple[str, Manifest]] = None
        capability_match: Optional[tuple[str, Manifest]] = None

        for uri, manifest in self._manifests.items():
            ident = manifest.identification
            # Rule 1: name match
            if ident.conversationalName and ident.conversationalName.lower() == name_lower:
                name_match = (uri, manifest)
                break
            # Rule 2: capability overlap (non-text output types only — text agents are intentionally diverse)
            if requested_outputs:
                for cap in (manifest.capabilities or []):
                    layers = cap.supportedLayers
                    if layers and any(o in (layers.output or []) for o in requested_outputs):
                        # Also require the task type keyphrase to be present
                        if any(task_lower in kp.lower() for kp in (cap.keyphrases or [])):
                            capability_match = (uri, manifest)
                            break

        return name_match or capability_match

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

    def _handle_publish_manifests(self, sender_uri: str, event) -> None:
        """Store manifests published by agents joining the conversation."""
        params = getattr(event, "parameters", None)
        servicing = params.get("servicingManifests", []) if params is not None else []
        for m in servicing:
            try:
                manifest = Manifest.from_dict(m) if isinstance(m, dict) else m
                self.store_manifest(sender_uri, manifest)
                name = manifest.identification.conversationalName or sender_uri
                caps = [kp for cap in (manifest.capabilities or []) for kp in (cap.keyphrases or [])]
                logger.debug("Manifest stored for %s: capabilities=%s", name, caps)
            except Exception as e:
                logger.warning("Failed to parse manifest from %s: %s", sender_uri, e)

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
            self._orchestrator_idle_grants = 0  # any response resets the nudge sequence
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
                is_media = media_key is not None  # detected from OFP features, covers "3d" too
                if not is_media:
                    # Parse [REMEMBER] directives from worker output (text-directive memory fallback)
                    cleaned_text = self._parse_worker_memory(text, sender_name)
                    # Record for manuscript accumulation on [ACCEPT]
                    self._last_worker_text = cleaned_text
                    self._last_worker_name = sender_name
                    self._assigned_uri = None  # clear assignment — orchestrator decides next
                    await self.grant_to(self._orchestrator_uri)
                    if self._renderer:
                        self._renderer.show_system_event(
                            f"Floor returned to {self._agents.get(self._orchestrator_uri, 'Orchestrator')}"
                        )
                else:
                    # Auto-accept media output — it cannot be text-verified via [ACCEPT]/[REJECT]
                    entry = (
                        f"[{media_key} by {sender_name}]: {media_path}"
                        if media_path
                        else f"[{media_key} by {sender_name}]: {text}"
                    )
                    self._manuscript.append(entry)
                    self._assigned_uri = None
                    await self.grant_to(self._orchestrator_uri)
                    if self._renderer:
                        self._renderer.show_system_event(
                            f"[Auto-accepted] {sender_name}'s {media_key} output"
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
                    elif self._has_llm_agent(text_agents):
                        # Only inject a round summary when at least one LLM agent
                        # is present — remote agents don't maintain narrative context
                        # and would just echo/search for the summary text.
                        await self._inject_round_summary()

        # Display
        if self._renderer:
            self._renderer.show_utterance(
                sender_uri, sender_name, text,
                media_key=media_key, media_path=media_path,
            )

    def _resolve_agent_uri_by_name(self, name: str) -> Optional[str]:
        """Look up a speakerUri by conversational name.

        Matching is case-insensitive and treats underscores as spaces so that
        ``Wikipedia_Research_Specialist`` finds ``Wikipedia Research Specialist``.
        """
        normalized = name.lower().replace("_", " ")
        for uri, agent_name in self._agents.items():
            if agent_name.lower().replace("_", " ") == normalized:
                return uri
        return None

    def _build_recovery_nudge(self, idle_count: int) -> str:
        """Build a graduated recovery message for the orchestrator.

        Escalates detail level with each consecutive idle grant so the LLM
        has increasing context to determine its next action.
        """
        available = [n for u, n in self._agents.items() if "floor-manager" not in u]
        word_count = sum(len(c.split()) for c in self._manuscript)
        parts = [
            f"[SYSTEM] You yielded without issuing an assignment. "
            f"Manuscript progress: {len(self._manuscript)} accepted entries ({word_count} words). "
            f"Available agents: {available}. "
            f"Issue your next [ASSIGN AgentName]: task directive, or [TASK_COMPLETE] if done.",
        ]

        # Escalate: inject memory summary on 3rd+ idle grant
        if idle_count >= 3 and self._memory_store and not self._memory_store.is_empty():
            summary = self._memory_store.get_summary(max_chars=800)
            parts.append(f"\n--- SESSION MEMORY ---\n{summary}\n---")

        # Escalate: inject manuscript content on 5th+ idle grant
        if idle_count >= 5 and self._manuscript:
            manuscript_preview = "\n".join(
                f"  {i+1}. {chunk[:120]}..." if len(chunk) > 120 else f"  {i+1}. {chunk}"
                for i, chunk in enumerate(self._manuscript)
            )
            parts.append(
                f"\n--- COMPLETED STEPS ({len(self._manuscript)}) ---\n"
                f"{manuscript_preview}\n---"
            )

        return "\n".join(parts)

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
            [BREAKOUT policy=<p> max_rounds=<n> topic=<t>]
            [BREAKOUT_AGENT -provider <p> -name <n> -system <s> [-model <m>]]
            [TASK_COMPLETE]
        """
        import re

        assigned_in_batch = False  # guard: only one [ASSIGN] per directive batch
        breakout_header: Optional[dict] = None  # parsed from [BREAKOUT ...]
        breakout_agent_specs: list[str] = []    # raw specs from [BREAKOUT_AGENT ...]

        # Collapse multi-line [BREAKOUT ...] and [BREAKOUT_AGENT ...] blocks into
        # single lines so the line-by-line parser below can handle them.
        # This happens when the orchestrator writes the directive as raw text
        # (rather than via a tool call) and includes a multi-line topic or system prompt.
        text = re.sub(
            r'\[BREAKOUT (?!AGENT\b|COMPLETE\b|SUMMARY\b)(.*?)\]',
            lambda m: '[BREAKOUT ' + ' '.join(m.group(1).split()) + ']',
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(
            r'\[BREAKOUT_AGENT\b(.*?)\]',
            lambda m: '[BREAKOUT_AGENT ' + ' '.join(m.group(1).split()) + ']',
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue

            # [BREAKOUT policy=<p> max_rounds=<n> topic=<t>]
            m = re.match(r"\[BREAKOUT\s+(.+?)\]", line, re.IGNORECASE)
            if m and not re.match(r"\[BREAKOUT_(AGENT|COMPLETE|SUMMARY)", line, re.IGNORECASE):
                spec = m.group(1).strip()
                policy_m = re.search(r"policy=(\S+)", spec)
                rounds_m = re.search(r"max_rounds=(\d+)", spec)
                topic_m = re.search(r"topic=(.+)", spec)
                breakout_header = {
                    "policy": policy_m.group(1) if policy_m else "round_robin",
                    "max_rounds": int(rounds_m.group(1)) if rounds_m else 6,
                    "topic": topic_m.group(1).strip() if topic_m else "General discussion",
                }
                continue

            # [BREAKOUT_AGENT -provider <p> -name <n> -system <s> [-model <m>]]
            m = re.match(r"\[BREAKOUT_AGENT\s+(.+?)\]", line, re.IGNORECASE)
            if m:
                breakout_agent_specs.append(m.group(1).strip())
                continue

            # [ASSIGN AgentName]: task  — set assigned agent, clear stale queue, grant floor
            m = re.match(r"\[ASSIGN\s+(.+?)\]\s*:\s*(.+)", line, re.IGNORECASE)
            if m:
                target_name = m.group(1).strip()
                task = m.group(2).strip()
                if assigned_in_batch:
                    logger.debug(
                        "Orchestrator [ASSIGN]: skipping duplicate assignment to '%s' in same batch",
                        target_name,
                    )
                    continue
                target_uri = self._resolve_agent_uri_by_name(target_name)
                if target_uri:
                    assigned_in_batch = True
                    # Track who is assigned so _handle_request_floor can filter others
                    self._assigned_uri = target_uri
                    # Flush any stale queue entries from agents that saw the orchestrator utterance
                    self._policy._request_queue.clear()
                    # Build directive, injecting manuscript context for local LLM agents only.
                    # Remote agents receive the raw task — they don't maintain narrative context
                    # and echoing the full manuscript confuses their endpoint logic.
                    directive = f"[DIRECTIVE for {target_name}]: {task}"
                    is_remote = "remote-" in target_uri
                    if self._manuscript and not is_remote:
                        manuscript_text = "\n\n".join(self._manuscript)
                        directive += (
                            f"\n\n--- STORY SO FAR ({sum(len(c.split()) for c in self._manuscript)} words) ---\n"
                            f"{manuscript_text}\n"
                            f"--- END OF STORY SO FAR ---\n"
                            f"Continue directly from where the story left off."
                        )
                    if not self._memory_store.is_empty() and not is_remote:
                        memory_summary = self._memory_store.get_summary(max_chars=600)
                        directive += f"\n\n--- SESSION MEMORY ---\n{memory_summary}\n---"
                    # Auto-inject the most recent breakout artifact into the first ASSIGN
                    # after a breakout completes — topic-agnostic: any agent type works.
                    if self._pending_breakout_file and not is_remote:
                        try:
                            artifact_content = self._pending_breakout_file.read_text(encoding="utf-8")
                            directive += (
                                f"\n\n--- BREAKOUT SESSION OUTPUT ---\n"
                                f"{artifact_content}\n"
                                f"--- END OF BREAKOUT OUTPUT ---"
                            )
                        except OSError as e:
                            logger.warning("Could not read breakout artifact: %s", e)
                        finally:
                            self._pending_breakout_file = None  # consume — single use
                    # Send directive FIRST (agent sets its task instruction from this),
                    # then grant floor.  The directive comes from the floor manager as a
                    # private OFP whisper; BaseLLMAgent skips request_floor() for these
                    # so there is no spurious floor request racing the explicit grant.
                    self._orchestrator_idle_grants = 0  # productive action — reset counter
                    await self._send_directed_utterance(directive, target_uri=target_uri)
                    await self.grant_to(target_uri)
                    if self._renderer:
                        self._renderer.show_system_event(
                            f"[Orchestrator → {target_name}]: {task[:60]}"
                        )
                else:
                    logger.warning("Orchestrator [ASSIGN]: agent '%s' not found", target_name)
                    available = [n for u, n in self._agents.items() if "floor-manager" not in u]
                    feedback = (
                        f"[SYSTEM] [ASSIGN {target_name}] FAILED — no such agent on the floor. "
                        f"Available agents: {available}. "
                        f"Use [SPAWN ...] to create new agents or [ASSIGN] an existing one."
                    )
                    await self._send_directed_utterance(feedback, target_uri=self._orchestrator_uri)
                continue

            # [ACCEPT]  — append last worker output to shared manuscript
            if re.match(r"\[ACCEPT\]", line, re.IGNORECASE):
                self._orchestrator_idle_grants = 0
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
                    self._orchestrator_idle_grants = 0
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
                    # OFP: send UninviteEvent before removing from the floor
                    await self.send_uninvite(target_uri, reason="@brokenPolicy")
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
                        logger.error("Orchestrator [SPAWN] failed for '%s': %s", spec_str, e)
                        if self._renderer:
                            self._renderer.show_system_event(f"[Orchestrator] Spawn failed: {e}")
                        if self._orchestrator_uri:
                            feedback = (
                                f"[SYSTEM] [SPAWN] FAILED for '{spec_str}': {e}. "
                                f"Use the flag format: [SPAWN -provider TYPE -name Name -type task-type -system Description]. "
                                f"Valid -provider values: anthropic, openai, google, hf. "
                                f"Valid -type values: text-to-image, text-to-music, text-to-video, orchestrator."
                            )
                            await self._send_directed_utterance(feedback, target_uri=self._orchestrator_uri)
                else:
                    logger.warning("Orchestrator [SPAWN]: no spawn_callback registered")
                continue

            # [SKIP AgentName]: reason  — record skip in manuscript, return floor to orchestrator
            m = re.match(r"\[SKIP\s+(.+?)\]\s*:\s*(.+)", line, re.IGNORECASE)
            if m:
                target_name = m.group(1).strip()
                reason = m.group(2).strip()
                self._manuscript.append(f"[skipped {target_name}]: {reason}")
                self._last_worker_text = ""
                self._assigned_uri = None
                if self._renderer:
                    self._renderer.show_system_event(
                        f"[Orchestrator] Skipped {target_name}: {reason[:80]}"
                    )
                continue

            # [STORE_MEMORY key]: content  — text-directive fallback for memory storage
            m = re.match(r"\[STORE_MEMORY(?:\s+([^\]]+))?\](?:\s*:\s*(.+))?", line, re.IGNORECASE)
            if m:
                mem_key = (m.group(1) or "").strip()
                mem_content = (m.group(2) or "").strip()
                if mem_key and mem_content and self._memory_store:
                    self._memory_store.store("decisions", mem_key, mem_content, author="orchestrator")
                    logger.debug("Orchestrator [STORE_MEMORY]: stored '%s'", mem_key)
                # Bare [STORE_MEMORY] without args is a no-op (LLM artifact)
                continue

            # [TASK_COMPLETE]
            if re.match(r"\[TASK_COMPLETE\]", line, re.IGNORECASE):
                if self._renderer:
                    self._renderer.show_system_event("[Orchestrator] Task complete — stopping")
                self._output_manuscript()
                self.stop()
                return

        # After processing all lines, execute breakout if one was requested
        if breakout_header and breakout_agent_specs:
            await self._execute_breakout(breakout_header, breakout_agent_specs)

    async def _execute_breakout(self, header: dict, agent_specs: list[str]) -> None:
        """Spawn temporary agents, run a breakout session, inject result.

        After the breakout completes, the summary is injected as if a
        worker spoke, and the floor is returned to the orchestrator.
        """
        topic = header["topic"]
        policy_str = header["policy"]
        max_rounds = min(max(header["max_rounds"], 2), 20)

        try:
            policy = FloorPolicy(policy_str)
        except ValueError:
            policy = FloorPolicy.ROUND_ROBIN
            logger.warning("Invalid breakout policy '%s', defaulting to round_robin", policy_str)

        # Prevent nested orchestrator breakouts
        if policy == FloorPolicy.SHOWRUNNER_DRIVEN:
            policy = FloorPolicy.ROUND_ROBIN
            logger.warning("SHOWRUNNER_DRIVEN not allowed in breakout, using round_robin")

        if not self._breakout_callback:
            logger.warning("Orchestrator [BREAKOUT]: no breakout_callback registered")
            if self._renderer:
                self._renderer.show_system_event("[Orchestrator] Breakout failed: no callback registered")
            return

        try:
            callback_result = await self._breakout_callback(topic, policy, max_rounds, agent_specs)
        except Exception as e:
            logger.error("Orchestrator [BREAKOUT] failed: %s", e, exc_info=True)
            callback_result = f"[Breakout: {topic}] — failed: {e}"
            if self._renderer:
                self._renderer.show_system_event(f"[Orchestrator] Breakout failed: {e}")

        # Unpack (compact_text, artifact_path) tuple from new-style callbacks.
        # Accept plain str for backward compatibility.
        if isinstance(callback_result, tuple):
            compact_text, artifact_path = callback_result
            self._pending_breakout_file = artifact_path  # injected into next ASSIGN directive
        else:
            compact_text = callback_result

        # Breakout output is context-only — do NOT set _last_worker_text so
        # it cannot be [ACCEPT]-ed into the manuscript.
        self._assigned_uri = None

        # Send compact notification to orchestrator (not the raw full dump).
        await self._send_directed_utterance(
            f"[BREAKOUT COMPLETE]\n{compact_text}",
            target_uri=self._orchestrator_uri,
        )
        # The orchestrator's yieldFloor (sent before this breakout ran) is still
        # in the queue. Mark it stale so _handle_yield_floor doesn't re-grant.
        self._skip_next_orchestrator_yield = True
        await self.grant_to(self._orchestrator_uri)

        if self._renderer:
            self._renderer.show_system_event(
                f"[Orchestrator] Breakout completed — summary injected"
            )

    def _parse_worker_memory(self, text: str, author: str) -> str:
        """Parse [REMEMBER category]: content directives from worker text output.

        Executes each directive against the session memory store and returns
        the text with [REMEMBER] lines stripped so they don't pollute the manuscript.
        """
        from ofp_playground.memory.tools import parse_remember_directives
        return parse_remember_directives(text, self._memory_store, author)

    def _output_manuscript(self) -> None:
        """Print the assembled manuscript and save it to the session output directory.

        Also writes a companion memory dump (JSON) when the memory store is non-empty.
        """
        if not self._manuscript:
            return
        import json

        text = "\n\n".join(self._manuscript)
        filepath = str(self._output.root / "manuscript.txt")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            logger.warning("Could not save manuscript: %s", e)
            filepath = None

        # Companion memory dump
        if not self._memory_store.is_empty():
            mem_filepath = str(self._output.root / "memory.json")
            try:
                with open(mem_filepath, "w", encoding="utf-8") as f:
                    json.dump(self._memory_store.to_dict(), f, indent=2)
                logger.info("Memory dump saved to %s", mem_filepath)
            except OSError as e:
                logger.warning("Could not save memory dump: %s", e)

        if self._renderer:
            self._renderer.show_manuscript(text, filepath=filepath)
        else:
            print("\n\n=== FINAL MANUSCRIPT ===\n")
            print(text)
            if filepath:
                print(f"\n[saved to {filepath}]")

    def _has_llm_agent(self, agent_uris: set[str]) -> bool:
        """Return True if any of the given agent URIs belongs to a local LLM agent."""
        return any("llm-" in uri for uri in agent_uris)

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

        # If the agent already holds the floor (e.g. got it via round-robin just before
        # its queued request arrived), skip — avoids double-grant loops.
        if self._policy.current_holder == sender_uri:
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

        # If a breakout just re-granted the orchestrator's floor, the yieldFloor
        # now arriving is stale (sent before the breakout ran). Consume it without
        # triggering a second grant or recovery nudge.
        if (
            self._orchestrator_uri
            and sender_uri == self._orchestrator_uri
            and self._skip_next_orchestrator_yield
        ):
            self._skip_next_orchestrator_yield = False
            return

        next_holder = self._policy.yield_floor(sender_uri)
        if next_holder:
            await self._grant_floor(next_holder)
        elif (
            self._orchestrator_uri
            and sender_uri == self._orchestrator_uri
            and self._assigned_uri is None
        ):
            # SHOWRUNNER_DRIVEN: orchestrator owns the floor unless it has actively
            # assigned a worker. If it yields with no pending assignment (e.g. a
            # bare [ACCEPT] with no following [ASSIGN]), re-grant immediately so
            # it can issue the next directive rather than going silent.
            self._orchestrator_idle_grants += 1
            _MAX_IDLE = 8
            if self._orchestrator_idle_grants > _MAX_IDLE:
                logger.error(
                    "Orchestrator stuck: %d consecutive grants with no assignment — stopping",
                    self._orchestrator_idle_grants,
                )
                if self._renderer:
                    self._renderer.show_system_event(
                        f"[Orchestrator] Stuck after {self._orchestrator_idle_grants} idle grants — stopping session"
                    )
                self.stop()
                return
            # Inject a recovery nudge so the orchestrator receives a new user
            # message with pipeline status — this fixes Anthropic's alternating-
            # role requirement and gives the LLM context to issue the next step.
            nudge = self._build_recovery_nudge(self._orchestrator_idle_grants)
            await self._send_directed_utterance(nudge, target_uri=self._orchestrator_uri)
            if self._renderer:
                self._renderer.show_system_event(
                    f"[Orchestrator] Recovery nudge #{self._orchestrator_idle_grants} sent"
                )
            await self._grant_floor(self._orchestrator_uri)

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
            elif event_type == "publishManifests":
                self._handle_publish_manifests(sender_uri, event)
            elif event_type == "acceptInvite":
                agent_name = self._agents.get(sender_uri, sender_uri.split(":")[-1])
                logger.debug("Agent %s accepted invite", agent_name)
                if self._renderer:
                    self._renderer.show_system_event(f"{agent_name} joined the conversation")
            elif event_type == "declineInvite":
                agent_name = self._agents.get(sender_uri, sender_uri.split(":")[-1])
                logger.warning("Agent %s declined invite", agent_name)
                self.unregister_agent(sender_uri)
                await self._bus.unregister(sender_uri)
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
