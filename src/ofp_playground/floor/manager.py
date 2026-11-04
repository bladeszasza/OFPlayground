"""Floor Manager: the OFP conversation coordinator."""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from openfloor import (
    Capability,
    Conversation,
    DialogEvent,
    Envelope,
    GrantFloorEvent,
    Identification,
    InviteEvent,
    Manifest,
    RequestFloorEvent,
    RevokeFloorEvent,
    Sender,
    SupportedLayers,
    TextFeature,
    Token,
    UninviteEvent,
    UtteranceEvent,
)

from ofp_playground.agents import uris
from ofp_playground.bus.message_bus import FLOOR_MANAGER_URI, MessageBus
from ofp_playground.config.output import SessionOutputManager
from ofp_playground.floor.history import ConversationHistory
from ofp_playground.floor.policy import FloorController, FloorPolicy
from ofp_playground.floor.sandbox_context import (
    PHASES_DIRNAME,
    build_sandbox_context_note,
    sync_sandbox_context,
)
from ofp_playground.memory.artifact_store import ArtifactStore
from ofp_playground.memory.artifact_store import _slugify as _artifact_slugify
from ofp_playground.memory.store import MemoryStore
from ofp_playground.models.artifact import Utterance
from ofp_playground.renderer.terminal import TerminalRenderer
from ofp_playground.trace import EventCollector, render_trace_html

logger = logging.getLogger(__name__)

FLOOR_MANAGER_NAME = "Floor Manager"


def _looks_like_internal_coding_progress(sender_name: str, text: str) -> bool:
    """Return True for coding-agent progress whispers that should stay internal.

    Coding agents emit private liveness/tool messages like:
      [DevAlpha] Starting coding task...
      [DevAlpha] [read_file] ...
      [DevAlpha] [write_file] Created: main.js

    These are not completed worker outputs and must not enter conversation
    history or return the floor to the orchestrator.
    """
    if not text or not text.startswith(f"[{sender_name}]"):
        return False

    suffix = text[len(f"[{sender_name}]"):].lstrip()
    if suffix.startswith("[") and "]" in suffix:
        return True

    return any(
        marker in text
        for marker in (
            "Starting coding task",
            "Running code_interpreter",
            "Sending to Gemini code execution",
            "Sending to Anthropic code_execution",
            "Output:",
            "Saved ",
            "Directive requested text-only mode",
            "Tool mode unavailable, retrying without tools",
        )
    )
FLOOR_MANAGER_URI_STR = FLOOR_MANAGER_URI
# A line that starts a new orchestrator directive (ends a multi-line task).
_DIRECTIVE_LINE_RE = (
    r"\[(?:ASSIGN|ASSIGN_PARALLEL|ACCEPT|REJECT|KICK|SPAWN|SKIP|STORE_MEMORY|TASK_COMPLETE|"
    r"BREAKOUT|BREAKOUT_AGENT|CODING_SESSION|CODING_CONTEXT|CODING_TODO|CODING_AGENT)[\s\]]"
)
ORCHESTRATOR_RECOVERY_BASE_DELAY_S = 0.5
ORCHESTRATOR_RECOVERY_MAX_DELAY_S = 3.0


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
        renderer: TerminalRenderer | None = None,
    ):
        self._bus = bus
        self._manifest = make_floor_manager_manifest()
        self._conversation_id = f"conv:{uuid.uuid4()}"
        self._output = SessionOutputManager(self._conversation_id)
        self._trace_collector = EventCollector(self._conversation_id)
        self._trace_collector.register_agent(self.speaker_uri, FLOOR_MANAGER_NAME)
        self._bus.set_collector(self._trace_collector)
        self._policy = FloorController(policy)
        self._history = ConversationHistory()
        self._renderer = renderer
        self._queue: asyncio.Queue = asyncio.Queue()
        self._agents: dict[str, str] = {}  # speakerUri -> conversationalName
        self._departed_agents: set[str] = set()
        self._draining_agents: set[str] = set()
        self._concurrent_holders: set[str] = set()
        self._remove_callback: Callable[[str], Awaitable[None]] | None = None
        self._status_callback: Callable[[str, str], None] | None = None
        self._running = False
        self._stop_event = asyncio.Event()
        self._round_count = 0
        self._agents_spoken_this_round: set[str] = set()
        self._director_uri: str | None = None
        self._showrunner_uri: str | None = None
        self._orchestrator_uri: str | None = None
        self._assigned_uri: str | None = None  # currently assigned agent in showrunner_driven mode
        self._parallel_batch: set[str] = set()  # URIs assigned simultaneously via [ASSIGN_PARALLEL]
        self._parallel_outputs: list[tuple[str, str]] = []  # (name, text) collected from the batch
        self._last_task_by_uri: dict[str, str] = {}  # last task per worker, reused by [REJECT]
        self._utterance_count: int = 0  # total agent utterances (history keeps only the last 100)
        self._rr_idle_yields: int = 0  # consecutive round-robin turns that produced no utterance
        self._moderators: set[str] = set()  # human moderators (MODERATED policy)
        self.max_turns: int | None = None  # stop after this many agent utterances
        self._last_orchestrator_directive_text: str = ""  # suppress duplicate replay while assignment is in flight
        self._spawn_callback: Callable[..., Any] | None = None  # set externally after creation
        self._orchestrator_idle_grants: int = 0  # consecutive floor grants with no productive action
        self._skip_next_orchestrator_yield: bool = False  # absorb stale yieldFloor after breakout re-grant
        self._breakout_callback: Callable[..., Any] | None = None  # set externally for breakout sessions
        self._coding_session_callback: Callable[..., Any] | None = None  # set externally for coding sessions
        self._on_artifact_saved: Callable[..., Any] | None = None  # set by canvas bridge
        self._pending_breakout_file = None  # Path to last breakout artifact — injected into next ASSIGN
        self._breakout_artifact_count: int = 0  # sequential counter for breakout-beat-N slugs
        self._manuscript: list[str] = []  # accumulated accepted chunks (showrunner_driven)
        self._last_worker_text: str = ""  # most recent non-orchestrator utterance text
        self._last_worker_name: str = ""  # speaker name for the above
        self._last_accepted_text: str = ""  # snapshot of just-[ACCEPT]ed text for truncation recovery
        self._manifests: dict[str, Manifest] = {}  # speakerUri → Manifest
        self._memory_store: MemoryStore = MemoryStore()  # ephemeral session memory
        self._artifact_store: ArtifactStore = ArtifactStore(self._output.root)  # phase artifact files

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
    def trace_collector(self) -> EventCollector:
        return self._trace_collector

    @property
    def active_agents(self) -> dict[str, str]:
        return dict(self._agents)

    @property
    def floor_holder(self) -> str | None:
        return self._policy.current_holder

    @property
    def policy(self) -> FloorPolicy:
        return self._policy.policy

    @property
    def floor_queue(self) -> list[str]:
        """URIs waiting for the floor: moderator call-ons first, then requests."""
        return self._policy.directed_queue + [uri for uri, _ in self._policy.queue]

    @property
    def utterance_count(self) -> int:
        """Total agent utterances so far. Unlike ``history`` this is never capped."""
        return self._utterance_count

    @property
    def round_boundary_uris(self) -> set[str]:
        """Director / show runner / orchestrator: granted by the floor, never by rotation."""
        return {u for u in (self._director_uri, self._showrunner_uri, self._orchestrator_uri) if u}

    @property
    def floor_holders(self) -> tuple[str, ...]:
        if self._policy.policy == FloorPolicy.FREE_FOR_ALL:
            return tuple(sorted(self._concurrent_holders))
        return (self.floor_holder,) if self.floor_holder else ()

    def kickoff_target(self, *, has_topic: bool, has_human: bool) -> str | None:
        """Who gets the first grant when a session starts (same for every UI).

        * SHOWRUNNER_DRIVEN → the orchestrator (its mission is in its prompt).
        * A director → the director (it opens every round).
        * MODERATED with a human moderator → nobody: the chair calls on agents.
        * ROUND_ROBIN → the first ring member (a non-human one when a topic was
          seeded, so agents answer it; otherwise the human may open).
        * Autonomous session without a topic → the first agent.
        * Otherwise nobody: agents request the floor in response to the topic.
        """
        if self._orchestrator_uri:
            return self._orchestrator_uri
        if self._director_uri:
            return self._director_uri
        if self._policy.policy == FloorPolicy.MODERATED and self._moderators:
            return None
        ring = [u for u in self._policy.rotation if u not in self._draining_agents]
        if self._policy.policy == FloorPolicy.ROUND_ROBIN and ring:
            if has_topic or not has_human:
                return next((u for u in ring if not uris.is_human_uri(u)), ring[0])
            return ring[0]
        if not has_topic and not has_human:
            return next(iter(self._agents), None)
        return None

    def _sync_sandbox_context(self) -> str:
        """Mirror phase and memory context into the shared sandbox and render guidance."""
        context = sync_sandbox_context(
            self._output.sandbox,
            self._artifact_store,
            self._memory_store,
        )
        return build_sandbox_context_note(context)

    def register_director(self, speaker_uri: str) -> None:
        """Mark an agent as the narrative director: it opens every round."""
        self._director_uri = speaker_uri
        self._policy.remove_from_rotation(speaker_uri)

    def register_showrunner(self, speaker_uri: str) -> None:
        """Mark an agent as the show runner: it closes every round."""
        self._showrunner_uri = speaker_uri
        self._policy.remove_from_rotation(speaker_uri)

    def register_orchestrator(self, speaker_uri: str) -> None:
        """Mark an agent as the SHOWRUNNER_DRIVEN orchestrator.

        The orchestrator gets the floor first and after every non-orchestrator utterance.
        Its utterances are parsed for structured directives ([ASSIGN], [REJECT], etc.).
        """
        self._orchestrator_uri = speaker_uri
        self._policy.remove_from_rotation(speaker_uri)

    async def grant_to(self, speaker_uri: str) -> None:
        """Directly grant floor to a specific agent, bypassing the request queue."""
        if speaker_uri in self._departed_agents or speaker_uri in self._draining_agents:
            return
        previous = self._policy.current_holder
        if previous and previous != speaker_uri and self._policy.policy != FloorPolicy.FREE_FOR_ALL:
            await self._revoke_floor(previous)
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
        self._departed_agents.discard(speaker_uri)
        self._draining_agents.discard(speaker_uri)
        self._trace_collector.register_agent(speaker_uri, name)
        # Round-boundary roles are granted explicitly and never take a rotation turn.
        if speaker_uri not in self.round_boundary_uris:
            self._policy.add_to_rotation(speaker_uri)
        # MODERATED: a human participant is the moderator (chair).
        if self._policy.policy == FloorPolicy.MODERATED and uris.is_human_uri(speaker_uri):
            self._moderators.add(speaker_uri)
            self._policy.manual = True
        if self._renderer:
            self._renderer.add_agent(speaker_uri, name)

    def unregister_agent(self, speaker_uri: str) -> str | None:
        """Detach membership synchronously; caller sends any returned floor grant."""
        self._report_floor(speaker_uri, "left")
        self._agents.pop(speaker_uri, None)
        self._departed_agents.add(speaker_uri)
        self._draining_agents.discard(speaker_uri)
        self._concurrent_holders.discard(speaker_uri)
        self._manifests.pop(speaker_uri, None)
        self._agents_spoken_this_round.discard(speaker_uri)
        self._parallel_batch.discard(speaker_uri)
        if speaker_uri in self._moderators:
            self._moderators.discard(speaker_uri)
            self._policy.manual = bool(self._moderators)
        for attr in ("_assigned_uri", "_orchestrator_uri", "_director_uri", "_showrunner_uri"):
            if getattr(self, attr, None) == speaker_uri:
                setattr(self, attr, None)
        return self._policy.remove_from_rotation(speaker_uri)

    def drain_agent(self, speaker_uri: str) -> None:
        """Exclude new grants and assignments; let an active turn yield normally."""
        self._draining_agents.add(speaker_uri)
        self._policy.suspend(speaker_uri)

    async def remove_agent(self, speaker_uri: str) -> None:
        if self._remove_callback is not None:
            await self._remove_callback(speaker_uri)
            return
        next_holder = self.unregister_agent(speaker_uri)
        await self._bus.unregister(speaker_uri)
        if next_holder is not None:
            await self._grant_floor(next_holder)

    def store_manifest(self, speaker_uri: str, manifest: Manifest) -> None:
        """Store a manifest published by an agent."""
        self._manifests[speaker_uri] = manifest

    def find_agent_by_manifest(self, name: str, task_type: str) -> tuple[str, Manifest] | None:
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

        name_match: tuple[str, Manifest] | None = None
        capability_match: tuple[str, Manifest] | None = None

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
                    if (layers and any(o in (layers.output or []) for o in requested_outputs)
                            and any(task_lower in kp.lower() for kp in (cap.keyphrases or []))):
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

    def _report_floor(self, speaker_uri: str, state: str) -> None:
        if self._status_callback:
            self._status_callback(speaker_uri, state)

    async def _grant_floor(self, speaker_uri: str) -> None:
        """Send grantFloor event to the specified agent."""
        if speaker_uri in self._departed_agents or speaker_uri in self._draining_agents:
            return
        if self._policy.policy == FloorPolicy.FREE_FOR_ALL:
            self._concurrent_holders.add(speaker_uri)
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
        self._report_floor(speaker_uri, "has-floor")
        if self._renderer and self._renderer.show_floor_events:
            agent_name = self._agents.get(speaker_uri, speaker_uri)
            self._renderer.show_system_event(f"Floor granted to {agent_name}")

    async def _revoke_floor(self, speaker_uri: str, reason: str = "@timedOut") -> None:
        """Revoke floor from the specified agent."""
        if self._policy.current_holder != speaker_uri and speaker_uri not in self._concurrent_holders:
            return
        self._concurrent_holders.discard(speaker_uri)
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
        if self._policy.current_holder == speaker_uri:
            self._policy.revoke_floor()
        self._report_floor(speaker_uri, "floor-revoked")

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
            except Exception:
                logger.warning('Failed to parse manifest from %s: see traceback', sender_uri, exc_info=True)

    async def _maybe_return_floor_to_orchestrator(self) -> None:
        """Return floor to orchestrator only when no parallel batch agents are still pending.

        In parallel-assign mode, multiple agents are in-flight simultaneously.
        Each one calls this helper when it finishes; only the last one actually
        triggers the grant so the orchestrator doesn't regain the floor too early.
        """
        if self._parallel_batch:
            return  # more agents still pending — hold the floor
        if self._orchestrator_uri:
            if self._orchestrator_uri is not None:
                await self.grant_to(self._orchestrator_uri)
            if self._renderer:
                self._renderer.show_system_event(
                    f"Floor returned to {self._agents.get(self._orchestrator_uri, 'Orchestrator')}"
                )

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

            media_mime: str | None = None
            for key in ("image", "video", "audio", "3d"):
                feat = de.features.get(key)
                if feat and feat.tokens and feat.tokens[0].value:
                    media_key = key
                    media_path = feat.tokens[0].value
                    media_mime = getattr(feat, "mimeType", None) or None
                    break

        # Coding agents send private liveness/tool whispers while they work.
        # Those are not completed worker outputs and must not return the floor.
        if (
            self._orchestrator_uri
            and self._assigned_uri
            and sender_uri == self._assigned_uri
            and text
            and _looks_like_internal_coding_progress(sender_name, text)
        ):
            return

        # Build typed Utterance and store in history
        if media_key == "image" and media_path:
            utterance = Utterance.from_image(sender_uri, sender_name, text, media_path,
                                             mime_type=media_mime or "image/png")
        elif media_key == "video" and media_path:
            utterance = Utterance.from_video(sender_uri, sender_name, text, media_path)
        else:
            utterance = Utterance.from_text(sender_uri, sender_name, text)

        self._history.add(utterance)
        if sender_uri != self.speaker_uri:
            self._utterance_count += 1
            self._rr_idle_yields = 0
            if self.max_turns and self._utterance_count >= self.max_turns:
                # Counted here (not via len(history), which is capped at 100) so
                # --max-turns works for any N and stops exactly on the Nth turn.
                if self._renderer:
                    self._renderer.show_utterance(
                        sender_uri, sender_name, text,
                        media_key=media_key, media_path=media_path,
                    )
                    self._renderer.show_system_event(
                        f"Reached {self.max_turns} turns — stopping conversation."
                    )
                self.stop()
                return

        # ----------------------------------------------------------------
        # SHOWRUNNER_DRIVEN: orchestrator utterance → parse its directives
        # ----------------------------------------------------------------
        if self._orchestrator_uri and sender_uri == self._orchestrator_uri:
            # Suppress replayed duplicate directives while a worker assignment is pending.
            if self._assigned_uri and text == self._last_orchestrator_directive_text:
                logger.debug("Ignoring duplicate orchestrator directives while assignment is pending")
                return
            self._last_orchestrator_directive_text = text
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
                await self._handle_showrunner_worker_utterance(
                    sender_uri, sender_name, text, media_key, media_path,
                )
            else:
                if sender_uri in self._moderators:
                    await self._call_on_addressed_agents(sender_uri, text)
                # ROUND_ROBIN rounds are closed when the last ring member yields
                # (_handle_round_robin_yield); other policies close them here.
                if self._policy.policy != FloorPolicy.ROUND_ROBIN:
                    await self._maybe_close_round()

        # Display
        if self._renderer:
            self._renderer.show_utterance(
                sender_uri, sender_name, text,
                media_key=media_key, media_path=media_path,
            )

    # ── SHOWRUNNER_DRIVEN worker bookkeeping ───────────────────────────────

    def _is_expected_worker(self, speaker_uri: str) -> bool:
        return speaker_uri == self._assigned_uri or speaker_uri in self._parallel_batch

    def _mark_worker_done(self, speaker_uri: str) -> None:
        if speaker_uri == self._assigned_uri:
            self._assigned_uri = None
        if speaker_uri in self._parallel_batch:
            self._parallel_batch.discard(speaker_uri)
            if not self._parallel_batch and self._parallel_outputs:
                # Combine every parallel answer so [ACCEPT] keeps all of them,
                # not just whichever agent happened to finish last.
                outputs, self._parallel_outputs = self._parallel_outputs, []
                self._last_worker_name = ", ".join(name for name, _ in outputs)
                self._last_worker_text = (
                    outputs[0][1] if len(outputs) == 1
                    else "\n\n".join(f"[{name}]:\n{text}" for name, text in outputs)
                )

    async def _handle_showrunner_worker_utterance(
        self,
        sender_uri: str,
        sender_name: str,
        text: str,
        media_key: str | None,
        media_path: str | None,
    ) -> None:
        """Record a worker delivery and hand the floor back to the orchestrator.

        Only the assigned worker (or a member of the active parallel batch)
        counts as a delivery.  Anything else — a human interjecting, a remote
        agent answering a broadcast, a late reply — stays conversation context
        and must not clear an assignment that is still in flight.
        """
        if not self._is_expected_worker(sender_uri):
            if (
                uris.is_human_uri(sender_uri)
                and self._assigned_uri is None
                and not self._parallel_batch
                and self._policy.current_holder is None
            ):
                # Idle floor (e.g. session started without a topic): human
                # input is what wakes the orchestrator up.
                await self._maybe_return_floor_to_orchestrator()
            elif not uris.is_human_uri(sender_uri):
                logger.info(
                    "Unsolicited output from %s kept as context (assigned: %s)",
                    sender_name, self._agents.get(self._assigned_uri or "", "nobody"),
                )
            return

        if media_key is None:
            cleaned_text = self._parse_worker_memory(text, sender_name)
            if sender_uri in self._parallel_batch:
                self._parallel_outputs.append((sender_name, cleaned_text))
            else:
                self._last_worker_text = cleaned_text
                self._last_worker_name = sender_name
        else:
            # Auto-accept media output — it cannot be text-verified via [ACCEPT]/[REJECT]
            entry = (
                f"[{media_key} by {sender_name}]: {media_path}"
                if media_path
                else f"[{media_key} by {sender_name}]: {text}"
            )
            self._manuscript.append(entry)
            if self._renderer:
                self._renderer.show_system_event(
                    f"[Auto-accepted] {sender_name}'s {media_key} output"
                )
        self._mark_worker_done(sender_uri)
        await self._maybe_return_floor_to_orchestrator()

    async def _handle_worker_without_output(
        self,
        speaker_uri: str,
        why: str = "yielded the floor without delivering any output",
    ) -> None:
        """An assigned worker finished its turn silently (error, timeout, empty reply).

        Without this the orchestrator never regained the floor and the whole
        SHOWRUNNER_DRIVEN session stalled.
        """
        name = self._agents.get(speaker_uri, speaker_uri.split(":")[-1])
        self._mark_worker_done(speaker_uri)
        if self._renderer:
            self._renderer.show_system_event(f"[Orchestrator] {name} {why}")
        if self._orchestrator_uri:
            await self._send_directed_utterance(
                f"[SYSTEM] {name} {why}. Decide the next step: "
                f"[REJECT {name}]: clearer instructions, re-[ASSIGN] the task to "
                f"another agent, or [SKIP {name}]: reason.",
                target_uri=self._orchestrator_uri,
            )
        await self._maybe_return_floor_to_orchestrator()

    async def _report_worker_removed(self, name: str) -> None:
        """An in-flight worker was removed by a UI/operator: tell the orchestrator."""
        if self._renderer:
            self._renderer.show_system_event(f"[Orchestrator] {name} was removed mid-task")
        if self._orchestrator_uri:
            await self._send_directed_utterance(
                f"[SYSTEM] {name} was removed from the floor before delivering its task. "
                f"Re-[ASSIGN] the work to another agent or [SKIP {name}]: reason.",
                target_uri=self._orchestrator_uri,
            )
        await self._maybe_return_floor_to_orchestrator()

    def _is_media_worker(self, speaker_uri: str) -> bool:
        """True for agents that turn a prompt into media rather than text."""
        if uris.uri_kind(speaker_uri) in uris.MEDIA_KINDS:
            return True
        outputs = uris.manifest_output_kinds(self._manifests.get(speaker_uri))
        return bool(outputs) and "text" not in outputs

    def _build_directive(
        self,
        target_name: str,
        target_uri: str,
        task: str,
        *,
        breakout_output: str | None = None,
    ) -> str:
        """Build the private ``[DIRECTIVE for X]`` sent ahead of a worker's grant.

        Text workers get the compact artifact index, sandbox note, session memory
        and (once) the latest breakout transcript.  Remote and media workers get
        the bare task: remote endpoints don't keep narrative context, and media
        generators would paste those blocks straight into their prompt.
        """
        directive = f"[DIRECTIVE for {target_name}]: {task}"
        if uris.is_remote_uri(target_uri) or self._is_media_worker(target_uri):
            return directive
        artifact_index = self._artifact_store.get_index()
        if artifact_index:
            directive += f"\n\n{artifact_index}"
        sandbox_note = self._sync_sandbox_context()
        if sandbox_note:
            directive += f"\n\n{sandbox_note}"
        if not self._memory_store.is_empty():
            memory_summary = self._memory_store.get_summary(max_chars=600)
            directive += f"\n\n--- SESSION MEMORY ---\n{memory_summary}\n---"
        if breakout_output:
            directive += (
                f"\n\n--- BREAKOUT SESSION OUTPUT ---\n"
                f"{breakout_output}\n"
                f"--- END OF BREAKOUT OUTPUT ---"
            )
        return directive

    def _take_pending_breakout_output(self) -> str | None:
        """Read (and consume) the latest breakout transcript for the next text worker."""
        if not self._pending_breakout_file:
            return None
        try:
            return self._pending_breakout_file.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Could not read breakout artifact: %s", e)
            return None
        finally:
            self._pending_breakout_file = None  # consume — single use

    async def _dispatch_assignment(self, target_name: str, target_uri: str, task: str) -> None:
        """Directive first, then the grant — the worker reads its task before it speaks."""
        breakout_output = None
        if not uris.is_remote_uri(target_uri) and not self._is_media_worker(target_uri):
            breakout_output = self._take_pending_breakout_output()
        directive = self._build_directive(
            target_name, target_uri, task, breakout_output=breakout_output,
        )
        self._last_task_by_uri[target_uri] = task
        await self._send_directed_utterance(directive, target_uri=target_uri)
        await self.grant_to(target_uri)

    # ── Round boundaries & moderation (non-orchestrated policies) ───────────

    def _round_participants(self) -> set[str]:
        """Agents whose turn counts toward completing a round."""
        excluded_kinds = uris.MEDIA_KINDS | {uris.PERCEPTION, uris.VISION, uris.OBSERVER, uris.FLOOR}
        return {
            uri for uri in self._agents
            if uri not in self.round_boundary_uris
            and uri not in self._draining_agents
            and uris.uri_kind(uri) not in excluded_kinds
        }

    async def _maybe_close_round(self) -> None:
        participants = self._round_participants()
        if not participants or not participants.issubset(self._agents_spoken_this_round):
            return
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
        elif self._has_llm_agent(participants):
            # Only inject a round summary when at least one LLM agent is present —
            # remote agents don't keep narrative context and would echo it.
            await self._inject_round_summary()

    def _addressed_agents(self, speaker_uri: str, text: str) -> list[str]:
        """Agents a moderator addressed by name, in the order they were mentioned."""
        import re as _re

        hits: list[tuple[int, str]] = []
        for uri, name in self._agents.items():
            if uri == speaker_uri or uri in self._moderators or uri in self._draining_agents:
                continue
            variants = {name, name.replace("_", " "), name.replace(" ", "")}
            positions = [
                m.start()
                for variant in variants if variant
                for m in [_re.search(rf"(?<![\w-]){_re.escape(variant)}(?![\w-])", text, _re.IGNORECASE)]
                if m
            ]
            if positions:
                hits.append((min(positions), uri))
        return [uri for _, uri in sorted(hits)]

    async def _call_on_addressed_agents(self, speaker_uri: str, text: str) -> None:
        """MODERATED: the moderator gives the floor by addressing agents by name."""
        if self._policy.policy != FloorPolicy.MODERATED:
            return
        for uri in self._addressed_agents(speaker_uri, text):
            if self._policy.call_on(uri):
                await self._grant_floor(uri)
            elif self._renderer and self._renderer.show_floor_events:
                self._renderer.show_system_event(
                    f"{self._agents.get(uri, uri)} will speak next (called on by the moderator)"
                )

    async def moderator_grant(self, speaker_uri: str) -> None:
        """Moderator override (/grant): take the floor from the holder and give it away."""
        if speaker_uri not in self._agents:
            raise ValueError("Unknown participant")
        await self.grant_to(speaker_uri)

    def _resolve_agent_uri_by_name(self, name: str) -> str | None:
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
            (f"[SYSTEM] You yielded without issuing an assignment. "
            f"Manuscript progress: {len(self._manuscript)} accepted entries ({word_count} words). "
            f"Available agents: {available}. "
            f"Issue your next [ASSIGN AgentName]: task directive, or [TASK_COMPLETE] if done."),
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

    async def _send_directed_utterance(self, text: str, target_uri: str | None = None) -> None:
        """Send a directive utterance, privately if target_uri given (OFP private message)."""
        if target_uri in self._draining_agents or target_uri in self._departed_agents:
            return
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

    def _save_character_memory_blocks(self, text: str, agent_name: str) -> None:
        """Split ``=== CHARACTER MEMORY: Name ===`` blocks into individual artifacts.

        When MemoryKeeper (or any agent) outputs multiple character memory blocks
        delimited by ``=== CHARACTER MEMORY: Name ===`` … ``=== END ===``, this
        method saves each block as a separate artifact with slug
        ``character-memory-<name>`` so the orchestrator can call
        ``read_artifact('character-memory-raphael')`` per character rather than
        loading the entire MemoryKeeper output.
        """
        import re as _re

        pattern = _re.compile(
            r"===\s*CHARACTER MEMORY:\s*(.+?)\s*===\s*(.*?)\s*===\s*END\s*===",
            _re.DOTALL | _re.IGNORECASE,
        )
        for m in pattern.finditer(text):
            char_name = m.group(1).strip()
            char_content = m.group(2).strip()
            slug = "character-memory-" + _artifact_slugify(char_name)
            artifact = self._artifact_store.save(
                agent_name=agent_name,
                content=char_content,
                slug=slug,
                summary=f"Character memory for {char_name}",
            )
            logger.debug("Saved per-character artifact: %s", slug)
            if self._on_artifact_saved:
                self._on_artifact_saved({
                    "type": "artifact_saved",
                    "slug": artifact.slug,
                    "agent": agent_name,
                    "kind": "phase",
                    "preview": char_content[:120],
                })

    def _expand_artifact_refs_in_task(self, task: str) -> str:
        """Replace ``read_artifact('slug')`` calls in a task string with actual content.

        Workers (especially HuggingFace text agents) cannot call ``read_artifact``
        as a tool.  Expanding the references inline ensures they receive the
        actual artifact content without needing tool support.

        References that cannot be resolved are left unchanged.
        """
        import re as _re

        def _replace(m: _re.Match) -> str:
            slug = m.group(1)
            content = self._artifact_store.read(slug)
            if content:
                return f"\n\n--- ARTIFACT: {slug} ---\n{content}\n--- END ARTIFACT: {slug} ---"
            return m.group(0)

        return _re.sub(r"read_artifact\(['\"](.+?)['\"]\)", _replace, task)

    @staticmethod
    def _expand_truncated_assign(task: str, accepted_text: str) -> str:
        """Replace a truncated [ASSIGN] task with the full [ACCEPT]ed content.

        When the Showrunner copy-pastes an agent's output into an [ASSIGN]
        directive it sometimes truncates after the first line.  This method
        detects the truncation by prefix-matching the task against the
        just-accepted text and substitutes the full content.

        Returns the original *task* unchanged if no truncation is detected.
        """
        import re as _re

        if not accepted_text or len(task) >= len(accepted_text):
            return task

        # Strip Showrunner wrappers: "Generate this music — PROMPT:" etc.
        def _core(t: str) -> str:
            return _re.sub(
                r"^(?:Generate\s+this\s+\w+\s*[-\u2014]\s*)?(?:PROMPT:\s*\n?)",
                "", t, flags=_re.IGNORECASE,
            ).lstrip()

        task_core = _core(task)
        accepted_core = _core(accepted_text)
        if not task_core or not accepted_core:
            return task

        # Prefix match: does the task look like a truncated start of the accepted text?
        match_len = min(len(task_core), 50)
        if task_core[:match_len] != accepted_core[:match_len]:
            return task
        if len(accepted_core) <= len(task_core) * 1.5:
            return task  # not significantly longer — no truncation

        logger.info(
            "Auto-expanding truncated ASSIGN task (%d → %d chars)",
            len(task_core), len(accepted_core),
        )
        return accepted_text

    async def _handle_orchestrator_directives(self, text: str) -> None:
        """Parse and execute structured directives emitted by the OrchestratorAgent.

        Directives (one per line):
            [ASSIGN AgentName]: task
            [ASSIGN_PARALLEL Agent1, Agent2, ...]: task
            [ACCEPT]
            [REJECT AgentName]: reason
            [KICK AgentName]
            [SPAWN -provider hf -name X -system Y -model Z]
            [BREAKOUT policy=<p> max_rounds=<n> topic=<t>]
            [BREAKOUT_AGENT -provider <p> -name <n> -system <s> [-model <m>]]
            [CODING_SESSION policy=<p> max_rounds=<n> topic=<t>]
            [CODING_CONTEXT artifact=<slug-or-phase>]
            [CODING_CONTEXT file=<sandbox-relative-path>]
            [CODING_AGENT -provider <p> -name <n> -system <s> [-model <m>]]
            [TASK_COMPLETE]
        """
        import re

        # Strip hallucinated model formatting artifacts (e.g. numerusform{...})
        text = re.sub(r'numerusform\{[^}]*\}', '', text)

        assigned_in_batch = False  # guard: only one [ASSIGN] per directive batch
        breakout_header: dict | None = None  # parsed from [BREAKOUT ...]
        breakout_agent_specs: list[str] = []    # raw specs from [BREAKOUT_AGENT ...]
        coding_session_header: dict | None = None  # parsed from [CODING_SESSION ...]
        coding_agent_specs: list[str] = []             # raw specs from [CODING_AGENT ...]
        coding_context_refs: dict[str, list[str]] = {
            "artifact_refs": [], "context_files": [], "todo_items": []
        }

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
        # Same collapsing for [CODING_SESSION ...] and [CODING_AGENT ...] blocks.
        text = re.sub(
            r'\[CODING_SESSION (?!COMPLETE\b)(.*?)\]',
            lambda m: '[CODING_SESSION ' + ' '.join(m.group(1).split()) + ']',
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(
            r'\[CODING_AGENT\b(.*?)\]',
            lambda m: '[CODING_AGENT ' + ' '.join(m.group(1).split()) + ']',
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(
            r'\[CODING_CONTEXT\b(.*?)\]',
            lambda m: '[CODING_CONTEXT ' + ' '.join(m.group(1).split()) + ']',
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            i += 1
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

            # [CODING_SESSION policy=<p> max_rounds=<n> topic=<t>]
            m = re.match(r"\[CODING_SESSION\s+(.+?)\]", line, re.IGNORECASE)
            if m and not re.match(r"\[CODING_SESSION\s*COMPLETE", line, re.IGNORECASE):
                spec = m.group(1).strip()
                policy_m = re.search(r"policy=(\S+)", spec)
                rounds_m = re.search(r"max_rounds=(\d+)", spec)
                topic_m = re.search(r"topic=(.+)", spec)
                coding_session_header = {
                    "policy": policy_m.group(1) if policy_m else "round_robin",
                    "max_rounds": int(rounds_m.group(1)) if rounds_m else 16,
                    "topic": topic_m.group(1).strip() if topic_m else "Coding session",
                }
                continue

            m = re.match(r"\[CODING_CONTEXT\s+(.+?)\]", line, re.IGNORECASE)
            if m:
                spec = m.group(1).strip()
                artifact_m = re.search(r"artifact=(\S+)", spec)
                file_m = re.search(r"file=(\S+)", spec)
                if artifact_m:
                    coding_context_refs["artifact_refs"].append(artifact_m.group(1).strip())
                if file_m:
                    coding_context_refs["context_files"].append(file_m.group(1).strip())
                continue

            m = re.match(r"\[CODING_TODO\s+(.+?)\]", line, re.IGNORECASE)
            if m:
                item = m.group(1).strip()
                if item:
                    coding_context_refs["todo_items"].append(item)
                continue

            # [CODING_AGENT -provider <p> -name <n> -system <s> [-model <m>]]
            m = re.match(r"\[CODING_AGENT\s+(.+?)\]", line, re.IGNORECASE)
            if m:
                coding_agent_specs.append(m.group(1).strip())
                continue

            # [ASSIGN AgentName]: task  — set assigned agent, clear stale queue, grant floor
            # The task may be multi-line (e.g. a Lyria prompt with timestamps and lyrics).
            # Collect continuation lines until the next directive or end of text.
            m = re.match(r"\[ASSIGN\s+(.+?)\]\s*:\s*(.*)", line, re.IGNORECASE)
            if m:
                target_name = m.group(1).strip()
                task_parts = [m.group(2)]
                while i < len(lines):
                    peek = lines[i].strip()
                    if re.match(_DIRECTIVE_LINE_RE, peek, re.IGNORECASE):
                        break
                    task_parts.append(lines[i])
                    i += 1
                task = "\n".join(task_parts).strip()
                # Auto-expand truncated task from just-[ACCEPT]ed content.
                # The Showrunner sometimes pastes only the first line of a
                # multi-line agent output (e.g. a Lyria PROMPT with timestamps).
                if self._last_accepted_text:
                    task = self._expand_truncated_assign(task, self._last_accepted_text)
                    self._last_accepted_text = ""
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
                    self._parallel_batch = set()
                    self._parallel_outputs = []
                    self._last_orchestrator_directive_text = line
                    # Flush stale requests from agents that saw the orchestrator utterance
                    self._policy.clear_requests()
                    # HF text agents have no tool support: inline read_artifact('slug') refs.
                    task = self._expand_artifact_refs_in_task(task)
                    self._orchestrator_idle_grants = 0  # productive action — reset counter
                    await self._dispatch_assignment(target_name, target_uri, task)
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

            # [ASSIGN_PARALLEL Agent1, Agent2, ...]: task
            # Dispatches the same task to several agents at once (fan-out). Each gets a
            # regular [DIRECTIVE for X] so every agent type understands it; the
            # orchestrator regains the floor once ALL of them delivered or failed, and
            # [ACCEPT] then keeps every answer.
            m = re.match(r"\[ASSIGN_PARALLEL\s+(.+?)\]\s*:\s*(.*)", line, re.IGNORECASE)
            if m:
                names = [n.strip() for n in m.group(1).split(",") if n.strip()]
                task_parts = [m.group(2)]
                while i < len(lines):
                    peek = lines[i].strip()
                    if re.match(_DIRECTIVE_LINE_RE, peek, re.IGNORECASE):
                        break
                    task_parts.append(lines[i])
                    i += 1
                task = self._expand_artifact_refs_in_task("\n".join(task_parts).strip())
                targets: list[tuple[str, str]] = []
                for n in names:
                    uri = self._resolve_agent_uri_by_name(n)
                    if uri and uri not in self._draining_agents and all(uri != u for _, u in targets):
                        targets.append((self._agents.get(uri, n), uri))
                if not targets or not task:
                    logger.warning("Orchestrator [ASSIGN_PARALLEL]: no valid agents/task in %s", names)
                    available = [n for u, n in self._agents.items() if u != self._orchestrator_uri]
                    await self._send_directed_utterance(
                        f"[SYSTEM] [ASSIGN_PARALLEL] FAILED — none of {names} found on the floor "
                        f"(or the task was empty). Available agents: {available}.",
                        target_uri=self._orchestrator_uri,
                    )
                    continue
                if assigned_in_batch:
                    logger.debug("Orchestrator [ASSIGN_PARALLEL] ignored: already assigned this turn")
                    continue
                assigned_in_batch = True
                # Parallel mode is mutually exclusive with single-assign mode
                self._assigned_uri = None
                self._parallel_batch = {uri for _, uri in targets}
                self._parallel_outputs = []
                self._last_orchestrator_directive_text = line
                self._policy.clear_requests()
                self._orchestrator_idle_grants = 0
                if self._renderer:
                    self._renderer.show_system_event(
                        f"[Orchestrator → PARALLEL {[n for n, _ in targets]}]: {task[:60]}"
                    )
                breakout_output = self._take_pending_breakout_output()
                for target_name, uri in targets:
                    peers = [n for n, u in targets if u != uri]
                    member_task = task
                    if peers and not self._is_media_worker(uri):
                        member_task += (
                            f"\n\n(Parallel assignment — {', '.join(peers)} work on the same "
                            f"task independently; give your own answer.)"
                        )
                    directive = self._build_directive(
                        target_name, uri, member_task,
                        breakout_output=None if uris.is_remote_uri(uri) or self._is_media_worker(uri)
                        else breakout_output,
                    )
                    self._last_task_by_uri[uri] = task
                    await self._send_directed_utterance(directive, target_uri=uri)
                # Grant every member without revoking the others: all of them work at
                # once. The controller tracks the first; later yields are no-ops.
                self._policy.grant_to(targets[0][1])
                for _, uri in targets:
                    await self._grant_floor(uri)
                continue

            # [ACCEPT] / [ACCEPT prose] / [ACCEPT plan] / [ACCEPT image]
            # - [ACCEPT] and [ACCEPT prose] → append to manuscript (backward compat)
            # - [ACCEPT plan] / [ACCEPT image] → save artifact only; keep manuscript clean
            m_accept = re.match(
                r"\[ACCEPT(?:\s+(prose|plan|image|music|audio|video))?\]", line, re.IGNORECASE
            )
            if m_accept:
                accept_type = (m_accept.group(1) or "").lower()
                add_to_manuscript = accept_type not in ("plan", "image", "music", "audio", "video")
                self._orchestrator_idle_grants = 0
                if self._last_worker_text:
                    self._last_accepted_text = self._last_worker_text
                    if add_to_manuscript:
                        self._manuscript.append(self._last_worker_text)
                    # Save as phase artifact for interlinked memory access
                    _accepted_artifact = self._artifact_store.save(
                        agent_name=self._last_worker_name or "unknown",
                        content=self._last_worker_text,
                    )
                    if self._on_artifact_saved:
                        self._on_artifact_saved({
                            "type": "artifact_saved",
                            "slug": _accepted_artifact.slug,
                            "agent": self._last_worker_name or "unknown",
                            "kind": "phase",
                            "preview": self._last_worker_text[:120],
                        })
                    # Split out any === CHARACTER MEMORY: Name === blocks into
                    # individual artifacts so the orchestrator can call
                    # read_artifact('character-memory-<name>') per character.
                    self._save_character_memory_blocks(
                        self._last_worker_text,
                        self._last_worker_name or "MemoryKeeper",
                    )
                    self._sync_sandbox_context()
                    self._last_worker_text = ""
                if self._renderer:
                    word_count = sum(len(chunk.split()) for chunk in self._manuscript)
                    label = "prose" if add_to_manuscript else accept_type or "plan"
                    self._renderer.show_system_event(
                        f"[Orchestrator] Accepted ({label}) — manuscript: {word_count} words"
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
                    self._parallel_batch = set()
                    self._parallel_outputs = []
                    self._last_worker_text = ""  # the rejected output must not be [ACCEPT]ed later
                    self._orchestrator_idle_grants = 0
                    self._policy.clear_requests()
                    previous_task = self._last_task_by_uri.get(target_uri, "")
                    if self._is_media_worker(target_uri) and previous_task:
                        # Media agents build their prompt from the first line: keep the
                        # original prompt first so a revision is not just the critique.
                        task = f"{previous_task}\nRevision notes: {reason}"
                    else:
                        task = f"Revision requested — {reason}"
                        if previous_task:
                            task += f"\n\nOriginal task: {previous_task}"
                    # Directive first, grant second — previously the grant went out first
                    # and the agent regenerated before it ever saw the revision reason.
                    await self._dispatch_assignment(target_name, target_uri, task)
                    self._last_task_by_uri[target_uri] = previous_task or task
                    if self._renderer:
                        self._renderer.show_system_event(
                            f"[Orchestrator] Rejected {target_name}: {reason[:60]}"
                        )
                else:
                    logger.warning("Orchestrator [REJECT]: agent '%s' not found", target_name)
                    available = [n for u, n in self._agents.items() if u != self._orchestrator_uri]
                    await self._send_directed_utterance(
                        f"[SYSTEM] [REJECT {target_name}] FAILED — no such agent on the floor. "
                        f"Available agents: {available}.",
                        target_uri=self._orchestrator_uri,
                    )
                continue

            # [KICK AgentName]
            m = re.match(r"\[KICK\s+(.+?)\]", line, re.IGNORECASE)
            if m:
                target_name = m.group(1).strip()
                target_uri = self._resolve_agent_uri_by_name(target_name)
                if target_uri:
                    # OFP: send UninviteEvent before removing from the floor
                    await self.send_uninvite(target_uri, reason="@brokenPolicy")
                    await self.remove_agent(target_uri)
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
                        logger.exception("Orchestrator [SPAWN] failed for '%s': see traceback", spec_str)
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

        wants_session = bool(
            (breakout_header and breakout_agent_specs)
            or (coding_session_header and coding_agent_specs)
        )
        if wants_session and assigned_in_batch:
            # A sub-floor re-grants the orchestrator when it finishes; running one
            # while a worker is also assigned would give two agents the floor.
            await self._send_directed_utterance(
                "[SYSTEM] Breakout/coding session NOT started: it cannot run in the same "
                "turn as an [ASSIGN]. Wait for the worker's result, then request the "
                "session on its own turn.",
                target_uri=self._orchestrator_uri,
            )
            return

        # After processing all lines, execute breakout if one was requested
        if breakout_header and breakout_agent_specs:
            await self._execute_breakout(breakout_header, breakout_agent_specs)

        # Execute coding session if one was requested
        if coding_session_header and coding_agent_specs:
            await self._execute_coding_session(
                coding_session_header,
                coding_agent_specs,
                coding_context_refs,
            )

    @staticmethod
    def _breakout_topic_keywords(topic: str) -> set[str]:
        """Extract normalised keywords from a breakout topic string."""
        import re as _re
        _STOP = {
            "and", "or", "the", "for", "of", "in", "to", "a", "an", "on",
            "is", "are", "was", "were", "be", "been", "with", "from", "by",
            "at", "as", "its", "it", "how", "what", "when", "where", "why",
            "versus", "vs", "that", "this", "which", "into",
        }
        words = set(_re.findall(r"[a-z0-9]+", topic.lower()))
        return words - _STOP

    def _find_duplicate_breakout(self, topic: str) -> str | None:
        """Return the key of an existing breakout whose topic overlaps significantly."""
        from ofp_playground.memory.store import MemoryCategory
        new_kw = self._breakout_topic_keywords(topic)
        if not new_kw:
            return None
        for entry in self._memory_store.recall(category=MemoryCategory.BREAKOUTS):
            existing_kw = self._breakout_topic_keywords(entry.key)
            if not existing_kw:
                continue
            overlap = len(new_kw & existing_kw) / min(len(new_kw), len(existing_kw))
            if overlap >= 0.6:
                return entry.key
        return None

    async def _execute_breakout(self, header: dict, agent_specs: list[str]) -> None:
        """Spawn temporary agents, run a breakout session, inject result.

        After the breakout completes, the summary is injected as if a
        worker spoke, and the floor is returned to the orchestrator.
        """
        topic = header["topic"]
        policy_str = header["policy"]
        max_rounds = min(max(header["max_rounds"], 2), 20)

        # ── Deduplication: reject topics that overlap heavily with prior breakouts ──
        existing = self._find_duplicate_breakout(topic)
        if existing:
            logger.warning(
                "Orchestrator [BREAKOUT]: topic '%s' duplicates completed breakout '%s' — skipping",
                topic, existing,
            )
            feedback = (
                f"[SYSTEM] Breakout topic \"{topic}\" is too similar to already-completed "
                f"breakout \"{existing}\". Choose a substantially different angle or "
                f"move to the next phase of your mission."
            )
            await self._send_directed_utterance(feedback, target_uri=self._orchestrator_uri)
            self._skip_next_orchestrator_yield = True
            if self._orchestrator_uri is not None:
                await self.grant_to(self._orchestrator_uri)
            if self._renderer:
                self._renderer.show_system_event(
                    f"[Orchestrator] Breakout skipped — duplicate of '{existing[:60]}'"
                )
            return

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
            logger.exception('Orchestrator [BREAKOUT] failed: see traceback')
            callback_result = f"[Breakout: {topic}] — failed: {e}"
            if self._renderer:
                self._renderer.show_system_event(f"[Orchestrator] Breakout failed: {e}")

        # Unpack (compact_text, artifact_path) tuple from new-style callbacks.
        # Accept plain str for backward compatibility.
        if isinstance(callback_result, tuple):
            compact_text, artifact_path = callback_result
            self._pending_breakout_file = artifact_path  # injected into next ASSIGN directive
            # Also persist to ArtifactStore with a stable slug so agents can call
            # read_artifact('breakout-beat-N') on retry (pending_breakout_file is single-use).
            try:
                self._breakout_artifact_count += 1
                beat_slug = f"breakout-beat-{self._breakout_artifact_count}"
                transcript_content = artifact_path.read_text(encoding="utf-8")
                _breakout_artifact = self._artifact_store.save(
                    agent_name="BreakoutSession",
                    content=transcript_content,
                    slug=beat_slug,
                    summary=f"Breakout transcript for beat {self._breakout_artifact_count}: {topic[:80]}",
                )
                logger.debug("Breakout transcript saved to ArtifactStore as %s", beat_slug)
                if self._on_artifact_saved:
                    self._on_artifact_saved({
                        "type": "artifact_saved",
                        "slug": _breakout_artifact.slug,
                        "agent": "BreakoutSession",
                        "kind": "phase",
                        "preview": transcript_content[:120],
                    })
            except Exception:
                logger.warning('Could not save breakout transcript to ArtifactStore: see traceback', exc_info=True)
        else:
            compact_text = callback_result

        # ── Record completed breakout topic in session memory for dedup ──
        from ofp_playground.memory.store import MemoryCategory
        summary_preview = (compact_text or "")[:200]
        self._memory_store.store(
            category=MemoryCategory.BREAKOUTS,
            key=topic,
            content=f"Completed. {summary_preview}",
            author="system",
        )

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
        if self._orchestrator_uri is not None:
            await self.grant_to(self._orchestrator_uri)

        if self._renderer:
            self._renderer.show_system_event(
                "[Orchestrator] Breakout completed — summary injected"
            )

    def _resolve_coding_context_files(self, context_refs: dict) -> list[str]:
        """Resolve coding-session artifact and file refs to sandbox-relative paths."""
        sandbox_context = sync_sandbox_context(
            self._output.sandbox,
            self._artifact_store,
            self._memory_store,
        )
        known_paths = set(sandbox_context.phase_files) | set(sandbox_context.memory_files)
        resolved: list[str] = []
        seen: set[str] = set()

        for ref in context_refs.get("artifact_refs", []):
            query = str(ref).strip()
            if not query:
                continue
            artifact = self._artifact_store.resolve(query) if self._artifact_store else None
            if artifact is None:
                logger.warning("Unknown coding-session artifact ref '%s'", query)
                continue
            relative_path = f"{PHASES_DIRNAME}/{artifact.file_path.name}"
            if relative_path not in seen:
                resolved.append(relative_path)
                seen.add(relative_path)

        for path in context_refs.get("context_files", []):
            relative_path = str(path).strip()
            if not relative_path or relative_path in seen:
                continue
            absolute_path = self._output.sandbox / relative_path
            if relative_path in known_paths or absolute_path.exists():
                resolved.append(relative_path)
                seen.add(relative_path)
                continue
            logger.warning("Missing coding-session context file '%s'", relative_path)

        return resolved

    async def _execute_coding_session(
        self,
        header: dict,
        agent_specs: list[str],
        context_refs: dict,
    ) -> None:
        """Spawn coding agents, run a coding session, inject result.

        After the session completes, the project manifest is sent to the
        orchestrator and the floor is returned.
        """
        topic = header["topic"]
        policy_str = header["policy"]
        max_rounds = min(max(header["max_rounds"], 2), 50)
        required_context_files = self._resolve_coding_context_files(context_refs)
        initial_todo_items = context_refs.get("todo_items", [])

        try:
            policy = FloorPolicy(policy_str)
        except ValueError:
            policy = FloorPolicy.ROUND_ROBIN
            logger.warning("Invalid coding session policy '%s', defaulting to round_robin", policy_str)

        if policy == FloorPolicy.SHOWRUNNER_DRIVEN:
            policy = FloorPolicy.ROUND_ROBIN
            logger.warning("SHOWRUNNER_DRIVEN not allowed in coding session, using round_robin")

        if not self._coding_session_callback:
            logger.warning("Orchestrator [CODING_SESSION]: no coding_session_callback registered")
            if self._renderer:
                self._renderer.show_system_event("[Orchestrator] Coding session failed: no callback registered")
            return

        try:
            callback_result = await self._coding_session_callback(
                topic,
                policy,
                max_rounds,
                agent_specs,
                required_context_files,
                initial_todo_items,
            )
        except Exception as e:
            logger.exception('Orchestrator [CODING_SESSION] failed: see traceback')
            callback_result = f"[Coding session: {topic[:60]}] — failed: {e}"
            if self._renderer:
                self._renderer.show_system_event(f"[Orchestrator] Coding session failed: {e}")

        if isinstance(callback_result, tuple):
            compact_text, artifact_path = callback_result
            self._pending_breakout_file = artifact_path  # reuse injection mechanism
        else:
            compact_text = callback_result

        # Record in session memory
        from ofp_playground.memory.store import MemoryCategory
        summary_preview = (compact_text or "")[:200]
        self._memory_store.store(
            category=MemoryCategory.TASKS,
            key=f"coding_session:{topic[:40]}",
            content=f"Completed. {summary_preview}",
            author="system",
        )

        self._assigned_uri = None

        await self._send_directed_utterance(
            f"[CODING SESSION COMPLETE]\n{compact_text}",
            target_uri=self._orchestrator_uri,
        )
        self._skip_next_orchestrator_yield = True
        if self._orchestrator_uri is not None:
            await self.grant_to(self._orchestrator_uri)

        if self._renderer:
            self._renderer.show_system_event(
                "[Orchestrator] Coding session completed — project files ready"
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
        filepath: str | None = str(self._output.root / "manuscript.txt")
        try:
            with (self._output.root / "manuscript.txt").open("w", encoding="utf-8") as f:
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

    def _output_trace_timeline(self) -> None:
        """Write a standalone HTML trace timeline for all captured OFP events."""
        filepath = self._output.root / "trace.html"
        try:
            render_trace_html(self._trace_collector, filepath)
            logger.info("Trace timeline saved to %s", filepath)
            if self._renderer:
                self._renderer.show_system_event(f"Trace timeline saved: {filepath}")
        except Exception:
            logger.warning('Could not save trace timeline: see traceback', exc_info=True)

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
            f"[FLOOR — Round {self._round_count} complete]\n"
            f"What just happened:\n" + "\n".join(recap_parts) + "\n\n"
            "Use the preceding contributions as context for the current task. "
            "Distinguish evidence from assumptions, challenge unsupported claims, "
            "and identify useful next steps. Follow your assigned role."
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
                f"[Floor] Round {self._round_count} summary injected"
            )

    async def _handle_request_floor(self, envelope: Envelope, event: RequestFloorEvent) -> None:
        sender_uri = envelope.sender.speakerUri if envelope.sender else "unknown"
        if sender_uri in self._draining_agents or sender_uri in self._departed_agents:
            return

        # In showrunner_driven mode only the assigned agent (or any member of an
        # active parallel batch) may request floor — all others are silently dropped.
        if self._orchestrator_uri and (
            sender_uri not in (self._orchestrator_uri, self._assigned_uri)
            and sender_uri not in self._parallel_batch
        ):
            return

        # If the agent already holds the floor (e.g. got it via round-robin just before
        # its queued request arrived), skip — avoids double-grant loops.
        if self._policy.current_holder == sender_uri:
            return

        reason = getattr(event, "reason", "") or ""
        granted = self._policy.request_floor(sender_uri, reason)
        self._report_floor(sender_uri, "requesting-floor")

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

        # SHOWRUNNER_DRIVEN: a worker that yields while still assigned delivered
        # nothing (error, timeout, empty reply, drained). Hand the floor back.
        if (
            self._orchestrator_uri
            and sender_uri != self._orchestrator_uri
            and self._is_expected_worker(sender_uri)
        ):
            if self._policy.current_holder == sender_uri:
                self._policy.revoke_floor()
            self._report_floor(sender_uri, "yielded")
            await self._handle_worker_without_output(sender_uri)
            return

        if self._policy.policy == FloorPolicy.ROUND_ROBIN and not self._orchestrator_uri:
            await self._handle_round_robin_yield(sender_uri)
            return

        if self._policy.policy == FloorPolicy.FREE_FOR_ALL and sender_uri in self._concurrent_holders:
            self._concurrent_holders.discard(sender_uri)
            if self._policy.current_holder == sender_uri:
                self._policy.yield_floor(sender_uri)
            self._report_floor(sender_uri, "yielded")
            return
        if self._policy.current_holder != sender_uri:
            return
        next_holder = self._policy.yield_floor(sender_uri)
        self._report_floor(sender_uri, "yielded")
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
            backoff = min(
                ORCHESTRATOR_RECOVERY_BASE_DELAY_S * self._orchestrator_idle_grants,
                ORCHESTRATOR_RECOVERY_MAX_DELAY_S,
            )
            if backoff > 0:
                await asyncio.sleep(backoff)
            # grant_to (not _grant_floor) so the controller records the holder;
            # otherwise the orchestrator's next yield is ignored as stale and the
            # session stalls after the second idle turn.
            if self._orchestrator_uri is not None:
                await self.grant_to(self._orchestrator_uri)

    async def _handle_round_robin_yield(self, sender_uri: str) -> None:
        """Strict ring order with explicit round boundaries.

        Round: [director] → every ring member in join order → [show runner].
        The director opens and the show runner closes each round; without them
        the floor manager injects a short recap when the ring wraps.
        """
        if self._policy.current_holder != sender_uri:
            return  # stale / out-of-turn yield
        self._report_floor(sender_uri, "yielded")
        ring = self._policy.rotation
        self._rr_idle_yields += 1
        idle_limit = max(4, 2 * (len(ring) + len(self.round_boundary_uris)))
        if self._rr_idle_yields > idle_limit:
            # Nobody in a full cycle produced anything (e.g. only perception agents
            # with nothing to analyse). Park the floor instead of spinning forever;
            # the next request (new human input, a spawn) restarts the ring.
            self._policy.revoke_floor()
            if self._renderer:
                self._renderer.show_system_event(
                    "Round-robin paused: a full cycle produced no output — waiting for input"
                )
            return

        target: str | None = None
        if sender_uri == self._director_uri:
            target = ring[0] if ring else None
        elif sender_uri == self._showrunner_uri:
            target = self._director_uri or (ring[0] if ring else None)
        elif sender_uri in ring and self._policy.is_last_in_rotation(sender_uri):
            self._round_count += 1
            self._agents_spoken_this_round.clear()
            if self._showrunner_uri:
                target = self._showrunner_uri
                label = "ShowRunner"
            elif self._director_uri:
                target = self._director_uri
                label = "Director"
            else:
                label = ""
                if self._has_llm_agent(set(ring)):
                    await self._inject_round_summary()
            if target and self._renderer:
                self._renderer.show_system_event(
                    f"Round {self._round_count} complete — {label} has the floor"
                )

        if target is not None:
            self._policy.revoke_floor()
            await self.grant_to(target)
            return
        next_holder = self._policy.yield_floor(sender_uri)
        if next_holder:
            await self._grant_floor(next_holder)

    async def process_envelope(self, envelope: Envelope) -> None:
        """Process an incoming envelope from the bus."""
        sender_uri = envelope.sender.speakerUri if envelope.sender else "unknown"
        if sender_uri in self._departed_agents:
            return

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
                await self.remove_agent(sender_uri)
            elif event_type == "bye":
                agent_name = self._agents.get(sender_uri, sender_uri)
                await self.remove_agent(sender_uri)
                if self._renderer:
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
                except TimeoutError:
                    if self._stop_event.is_set():
                        break
                except Exception:
                    logger.exception('Floor manager error: see traceback')
        finally:
            self._running = False
            self._output_trace_timeline()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
