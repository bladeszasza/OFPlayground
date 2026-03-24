"""Breakout session — a self-contained OFP sub-floor.

A breakout session is a short-lived, independent conversation with its own
MessageBus, FloorManager, policy, and topic.  It reuses the *same* agent
instances (re-wired to a private bus for the duration) so that agents keep
their API clients but participate on an isolated floor.

Only one level of nesting is allowed — a breakout cannot spawn another.

Lifecycle (OFP-compliant):
    1. Parent orchestrator calls ``create_breakout_session`` tool.
    2. FloorManager creates agents for the breakout, wires them to a
       private bus, and starts a BreakoutFloorManager.
    3. Agents exchange utterances under the chosen policy.
    4. When max_rounds elapse or an agent emits ``[BREAKOUT_COMPLETE]``,
       the session ends.
    5. A summary is extracted from the conversation history and returned
       to the parent floor as an ``[ASSIGN]`` result from the breakout.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Optional

from openfloor import (
    Conversation,
    DialogEvent,
    Envelope,
    GrantFloorEvent,
    RequestFloorEvent,
    Sender,
    TextFeature,
    Token,
    UtteranceEvent,
)

from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.floor.history import ConversationHistory
from ofp_playground.floor.policy import FloorController, FloorPolicy
from ofp_playground.models.artifact import Utterance

logger = logging.getLogger(__name__)

BREAKOUT_FM_URI = "tag:ofp-playground.local,2025:breakout-floor-manager"
BREAKOUT_OUTPUT_DIR = Path("ofp-breakout")


class BreakoutResult(NamedTuple):
    """Return value from run_breakout_session."""
    history: list  # list[Utterance]
    topic: str
    agent_names: list  # list[str]
    round_count: int


class BreakoutFloorManager:
    """Lightweight floor manager for a breakout session.

    Differences from the main FloorManager:
    - No renderer (output is collected, not displayed).
    - No orchestrator / showrunner / director modes.
    - Runs for a bounded number of rounds then auto-stops.
    - Exposes `run_to_completion()` which returns the conversation
      history as a list of Utterance objects.
    """

    def __init__(
        self,
        bus: MessageBus,
        policy: FloorPolicy,
        parent_conversation_id: str,
        max_rounds: int = 6,
    ):
        self._bus = bus
        self._conversation_id = f"breakout:{uuid.uuid4()}"
        self._parent_conversation_id = parent_conversation_id
        self._policy = FloorController(policy)
        self._history = ConversationHistory()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._agents: dict[str, str] = {}
        self._running = False
        self._max_rounds = max_rounds
        self._utterance_count = 0

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def speaker_uri(self) -> str:
        return BREAKOUT_FM_URI

    def register_agent(self, speaker_uri: str, name: str) -> None:
        self._agents[speaker_uri] = name
        self._policy.add_to_rotation(speaker_uri)

    def _make_sender(self) -> Sender:
        return Sender(speakerUri=self.speaker_uri, serviceUrl="local://breakout-fm")

    def _make_conversation(self) -> Conversation:
        return Conversation(id=self._conversation_id)

    async def _send(self, envelope: Envelope) -> None:
        await self._bus.send(envelope)

    async def _grant_floor(self, speaker_uri: str) -> None:
        from openfloor import To
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[
                GrantFloorEvent(
                    to=To(speakerUri=speaker_uri),
                    reason="Your turn in breakout",
                )
            ],
        )
        await self._send(envelope)

    async def run_to_completion(self, topic: str) -> list[Utterance]:
        """Run the breakout session and return conversation history.

        1. Register with bus, seed topic.
        2. Process events until max_rounds utterances or [BREAKOUT_COMPLETE].
        3. Clean up and return history.
        """
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)

        # Seed topic
        await self._seed_topic(topic)

        try:
            while self._running:
                try:
                    envelope = await asyncio.wait_for(self._queue.get(), timeout=120.0)
                    await self._process_envelope(envelope)
                except asyncio.TimeoutError:
                    logger.warning("Breakout session timed out")
                    break
                except Exception as e:
                    logger.error("Breakout error: %s", e, exc_info=True)
        finally:
            self._running = False
            await self._bus.unregister(self.speaker_uri)

        return self._history.all()

    async def _seed_topic(self, topic: str) -> None:
        de = DialogEvent(
            speakerUri=self.speaker_uri,
            id=str(uuid.uuid4()),
            features={"text": TextFeature(tokens=[Token(value=topic)])},
        )
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[UtteranceEvent(dialogEvent=de)],
        )
        await self._send(envelope)

    async def _process_envelope(self, envelope: Envelope) -> None:
        sender_uri = envelope.sender.speakerUri if envelope.sender else "unknown"

        for event in (envelope.events or []):
            event_type = event.eventType if hasattr(event, "eventType") else type(event).__name__

            if event_type == "utterance":
                await self._handle_utterance(envelope, event)
            elif event_type == "requestFloor":
                await self._handle_request_floor(sender_uri)
            elif event_type == "yieldFloor":
                await self._handle_yield_floor(sender_uri)
            # Other events (publishManifests, acceptInvite, etc.) are ignored

    async def _handle_utterance(self, envelope: Envelope, event) -> None:
        sender_uri = envelope.sender.speakerUri if envelope.sender else "unknown"
        sender_name = self._agents.get(sender_uri, sender_uri.split(":")[-1])

        text = ""
        de = getattr(event, "dialogEvent", None)
        if de and de.features:
            text_feat = de.features.get("text")
            if text_feat and text_feat.tokens:
                text = " ".join(t.value for t in text_feat.tokens if t.value)

        if not text:
            return

        utterance = Utterance.from_text(sender_uri, sender_name, text)
        self._history.add(utterance)

        if sender_uri != self.speaker_uri:
            self._utterance_count += 1

            # Check for explicit completion signal
            if "[BREAKOUT_COMPLETE]" in text:
                self._running = False
                return

            # Check round limit
            if self._utterance_count >= self._max_rounds:
                self._running = False
                return

            # Track round for sequential/round_robin
            text_agents = {
                uri for uri in self._agents
                if "image" not in uri and "video" not in uri and "audio" not in uri
            }
            # After utterance, yield floor for next agent
            next_uri = self._policy.yield_floor(sender_uri)
            if next_uri:
                await self._grant_floor(next_uri)

    async def _handle_request_floor(self, sender_uri: str) -> None:
        granted = self._policy.request_floor(sender_uri)
        if granted:
            await self._grant_floor(sender_uri)

    async def _handle_yield_floor(self, sender_uri: str) -> None:
        next_uri = self._policy.yield_floor(sender_uri)
        if next_uri:
            await self._grant_floor(next_uri)


def _agent_turns(history: list) -> list:
    """Filter history to non-floor-manager utterances."""
    return [
        u for u in history
        if "floor-manager" not in u.speaker_uri and "breakout-floor" not in u.speaker_uri
    ]


def save_breakout_artifact(result: BreakoutResult, output_dir: Path = BREAKOUT_OUTPUT_DIR) -> Path:
    """Save full breakout session history to a structured Markdown file.

    Returns the path to the saved file.
    """
    output_dir.mkdir(exist_ok=True)
    slug = re.sub(r"[^\w]+", "_", result.topic[:40]).strip("_").lower()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"{timestamp}_{slug}.md"

    lines = [
        f"# Breakout Session: {result.topic}",
        f"Date: {datetime.now().isoformat(timespec='seconds')}",
        f"Agents: {', '.join(result.agent_names)} | Rounds: {result.round_count}",
        "",
        "---",
        "",
    ]
    for u in _agent_turns(result.history):
        lines.append(f"## {u.speaker_name}")
        lines.append(u.text)
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def build_compact_notification(
    result: BreakoutResult,
    file_path: Path,
    session_num: int,
) -> str:
    """Build a short (~200-word) notification for the parent orchestrator.

    Includes stats and the final (most-refined) contribution from each agent
    — enough context for the orchestrator to write a good [ASSIGN] directive
    without receiving the full raw transcript.
    """
    agent_str = ", ".join(result.agent_names)
    lines = [
        f"[BREAKOUT COMPLETE]: {result.topic}",
        f"{len(result.agent_names)} agents | {result.round_count} rounds | {agent_str}",
        f"Artifact: {file_path}",
        f"Memory key: breakout_{session_num}",
        "",
        "Highlights (final contribution per agent):",
    ]

    turns = _agent_turns(result.history)
    # Last utterance per agent (most refined version)
    last_by_agent: dict[str, str] = {}
    for u in turns:
        last_by_agent[u.speaker_name] = u.text

    for name, text in last_by_agent.items():
        snippet = text[:150].replace("\n", " ")
        if len(text) > 150:
            snippet += "..."
        lines.append(f"[{name}]: {snippet}")

    return "\n".join(lines)


def extract_breakout_summary(
    history: list,
    topic: str,
    max_chars: int = 16000,
) -> str:
    """Build a plain-text summary from a breakout session's history.

    Kept for backward-compatibility and error-path fallback.
    """
    if not history:
        return f"[Breakout: {topic}] — no contributions recorded."

    lines: list[str] = [f"[BREAKOUT SUMMARY: {topic}]"]
    for u in _agent_turns(history):
        text = u.text
        if len(text) > 400:
            text = text[:397] + "..."
        lines.append(f"  [{u.speaker_name}]: {text}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n  ...(truncated)"
    return text


async def run_breakout_session(
    topic: str,
    agents: list,
    policy: FloorPolicy,
    parent_conversation_id: str,
    max_rounds: int = 6,
    parent_renderer=None,
) -> BreakoutResult:
    """Run a full breakout session and return a BreakoutResult.

    This is the main entry point called by the FloorManager when the
    orchestrator requests a breakout via tool calling.

    The agents should be **fresh** instances created specifically for
    this breakout — they are wired to an isolated bus for the session
    and discarded afterward.

    Args:
        topic: Discussion topic / question for the breakout.
        agents: List of fresh agent instances (BaseLLMAgent subclasses).
        policy: Floor policy for the breakout session.
        parent_conversation_id: ID of the parent conversation (for OFP
                                traceability).
        max_rounds: Maximum number of utterances before auto-stop.
        parent_renderer: Optional TerminalRenderer for status messages.

    Returns:
        BreakoutResult with history, topic, agent_names, and round_count.
    """
    if len(agents) < 2:
        return BreakoutResult(
            history=[],
            topic=topic,
            agent_names=[a.name for a in agents],
            round_count=0,
        )

    # Create isolated bus and floor manager for the breakout
    breakout_bus = MessageBus()
    breakout_fm = BreakoutFloorManager(
        bus=breakout_bus,
        policy=policy,
        parent_conversation_id=parent_conversation_id,
        max_rounds=max_rounds,
    )

    if parent_renderer:
        agent_names = ", ".join(a.name for a in agents)
        parent_renderer.show_system_event(
            f"[Breakout] Starting: {topic[:60]} | policy={policy.value} | "
            f"agents=[{agent_names}] | max_rounds={max_rounds}"
        )

    # Wire fresh agents to the breakout bus
    for agent in agents:
        agent._bus = breakout_bus
        agent._conversation_id = breakout_fm.conversation_id
        agent._queue = asyncio.Queue()
        agent._conversation_history = []
        agent._pending_context = []
        agent._has_floor = False
        agent._pending_floor_request = False
        breakout_fm.register_agent(agent.speaker_uri, agent.name)
        await breakout_bus.register(agent.speaker_uri, agent._queue)

    # Give each agent a name registry so they know who's in the breakout
    name_registry = dict(breakout_fm._agents)
    for agent in agents:
        if hasattr(agent, "set_name_registry"):
            agent.set_name_registry(name_registry)

    # Start agent run loops and breakout floor
    agent_tasks = [asyncio.create_task(_run_agent_for_breakout(a)) for a in agents]

    try:
        history = await asyncio.wait_for(
            breakout_fm.run_to_completion(topic),
            timeout=300.0,  # 5-minute hard timeout
        )
    except asyncio.TimeoutError:
        logger.warning("Breakout hard timeout after 300s")
        history = breakout_fm._history.all()
    finally:
        # Cancel agent loops and clean up
        for t in agent_tasks:
            t.cancel()
        await asyncio.gather(*agent_tasks, return_exceptions=True)
        for agent in agents:
            await breakout_bus.unregister(agent.speaker_uri)

    agent_names = [a.name for a in agents]
    round_count = len(_agent_turns(history))

    if parent_renderer:
        parent_renderer.show_system_event(
            f"[Breakout] Completed: {topic[:60]} — "
            f"{round_count} utterances → artifact pending"
        )

    return BreakoutResult(
        history=history,
        topic=topic,
        agent_names=agent_names,
        round_count=round_count,
    )


async def _run_agent_for_breakout(agent) -> None:
    """Run an agent's event loop for the duration of a breakout.

    Mirrors BaseLLMAgent.run() but without bus registration (already
    done by run_breakout_session) or manifest publication.
    """
    try:
        while True:
            try:
                envelope = await asyncio.wait_for(agent._queue.get(), timeout=1.0)
                await agent._dispatch(envelope)
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        return
