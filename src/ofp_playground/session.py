"""Shared session lifetime and participant control for CLI, Canvas and web."""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ofp_playground.status.client import StatusConnection
    from ofp_playground.status.runtime import SessionStatusRuntime

from ofp_playground.agents.registry import AgentRegistry
from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.floor.manager import FloorManager


async def seed_topic(topic: str, floor: FloorManager, bus: MessageBus) -> None:
    """Broadcast the session topic as a floor-manager utterance."""
    from openfloor import (
        Conversation,
        DialogEvent,
        Envelope,
        Sender,
        TextFeature,
        Token,
        UtteranceEvent,
    )

    from ofp_playground.bus.message_bus import FLOOR_MANAGER_URI

    de = DialogEvent(
        speakerUri=FLOOR_MANAGER_URI,
        id=str(uuid.uuid4()),
        features={"text": TextFeature(tokens=[Token(value=topic)])},
    )
    envelope = Envelope(
        sender=Sender(speakerUri=FLOOR_MANAGER_URI, serviceUrl="local://floor-manager"),
        conversation=Conversation(id=floor.conversation_id),
        events=[UtteranceEvent(dialogEvent=de)],
    )
    await bus.send(envelope)


async def kickoff(
    floor: FloorManager,
    bus: MessageBus,
    *,
    topic: str | None,
    human: Any | None = None,
    notify: Callable[[str], None] | None = None,
    delay: float = 1.0,
    seed: Callable[[str, FloorManager, MessageBus], Coroutine[Any, Any, None]] | None = None,
) -> None:
    """Start a session the same way in the CLI, the web UI and the canvas.

    Seeds the topic (and the memory goal), frees a floor the human was holding
    at start-up so agents can answer the topic, then gives the first grant to
    :meth:`FloorManager.kickoff_target`.  There is no sleep between seeding and
    granting: the topic is already in every agent queue (``put_nowait``), and a
    gap would let early floor requests race the deliberate first grant.
    """
    await asyncio.sleep(delay)  # let freshly spawned agents register and publish manifests
    if floor._stop_event.is_set():
        return
    if topic:
        if notify:
            notify(f'Topic: "{topic}"')
        floor._memory_store.seed_goal(topic)
        await (seed or seed_topic)(topic, floor, bus)
        if floor._stop_event.is_set():
            return
        if human is not None and floor.floor_holder == human.speaker_uri:
            # The human requested the floor at start-up (SEQUENTIAL). Release it
            # so agents answer the topic first; the human re-queues for a turn.
            human._has_floor = False
            await human.yield_floor()
            await human.request_floor()
    target = floor.kickoff_target(has_topic=bool(topic), has_human=human is not None)
    if target is not None and floor.floor_holder != target:
        await floor.grant_to(target)


class SessionService:
    def __init__(self, floor: FloorManager, bus: MessageBus, registry: AgentRegistry):
        self.floor = floor
        self.bus = bus
        self.registry = registry
        floor._remove_callback = self._remove_from_floor
        self._lock = asyncio.Lock()
        self._closed = False
        self.status: SessionStatusRuntime | None = None
        self.status_problem: str | None = None
        self.operations: list[dict[str, Any]] = []
        self._retired: dict[str, Any] = {}
        self._governance_lock = asyncio.Lock()

    @property
    def participants(self) -> dict[str, Any]:
        """Current and retired main-session instances for history/control lookup."""
        return {**self._retired, **{agent.speaker_uri: agent for agent in self.registry.all()}}

    def notify_input(self) -> None:
        """Release explicit synthetic input waits at an actual user-input boundary."""
        from ofp_playground.agents.demo import DemoAgent

        for agent in self.registry.all():
            if isinstance(agent, DemoAgent):
                agent.resume_input()

    async def enqueue_input(self, speaker_uri: str, text: str) -> None:
        from ofp_playground.agents.web_human import WebHumanAgent

        agent = self.registry.get(speaker_uri)
        if not isinstance(agent, WebHumanAgent) or speaker_uri not in self.floor.active_agents or self.floor._stop_event.is_set():
            raise ValueError("Human input is no longer available")
        if not text.strip() or len(text) > 10000:
            raise ValueError("Input must contain 1–10000 characters")
        try:
            agent.input_queue.put_nowait(text)
        except asyncio.QueueFull as exc:
            raise ValueError("Human input queue is full; wait for pending input") from exc
        self.notify_input()

    # ── Floor commands shared by every human input (CLI, web chat, canvas) ──

    FLOOR_COMMANDS = ("grant", "pass", "floor", "agents")

    def _human(self, speaker_uri: str | None) -> Any | None:
        return self.registry.get(speaker_uri) if speaker_uri else None

    async def floor_command(self, text: str, human_uri: str | None = None) -> str | None:
        """Run a ``/grant`` / ``/pass`` / ``/floor`` / ``/agents`` command.

        Returns the message to show the user, or ``None`` when *text* is not a
        floor command (so the caller can handle or reject it).
        """
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        parts = stripped[1:].split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        args = parts[1].strip() if len(parts) > 1 else ""
        if cmd not in self.FLOOR_COMMANDS:
            return None
        floor = self.floor
        if cmd == "agents":
            names = [
                f"{name}{' 🎤' if uri in floor.floor_holders else ''}"
                for uri, name in floor.active_agents.items()
            ]
            return "Participants: " + (", ".join(names) or "none")
        if cmd == "floor":
            holders = [floor.active_agents.get(u, u) for u in floor.floor_holders]
            waiting = [floor.active_agents.get(u, u) for u in floor.floor_queue]
            msg = f"Floor ({floor.policy.value}): {', '.join(holders) or 'nobody'}"
            if waiting:
                msg += f" · waiting: {', '.join(waiting)}"
            return msg
        if cmd == "pass":
            human = self._human(human_uri)
            if human is None or not hasattr(human, "pass_turn"):
                return "Only a human participant can pass a turn"
            return "Turn passed" if await human.pass_turn() else "You don't hold the floor"
        # /grant NAME — moderator override (any policy)
        if not args:
            return "Usage: /grant <agent name>"
        uri = floor._resolve_agent_uri_by_name(args)
        if uri is None:
            return f"Agent '{args}' not found. Use /agents to list participants."
        if uri in floor._draining_agents:
            return f"{args} is leaving the session and cannot take the floor"
        await floor.moderator_grant(uri)
        return f"Floor granted to {floor.active_agents.get(uri, args)}"

    async def enable_status(self, connection: StatusConnection | None = None) -> None:
        import httpx

        from ofp_playground.status.client import connect_local_service
        from ofp_playground.status.runtime import SessionStatusRuntime

        try:
            connection = connection or await connect_local_service()
        except (httpx.HTTPError, RuntimeError, OSError, ValueError):
            self.status_problem = "Status service unavailable; agent status is unknown"
            return
        self.status = SessionStatusRuntime(connection, self.floor.conversation_id)
        self.registry.status_runtime = self.status
        self.floor._status_callback = self.status.floor_transition

    async def _remove_from_floor(self, speaker_uri: str) -> None:
        await self.remove(speaker_uri)

    def _operation(self, speaker_uri: str, action: str) -> dict[str, Any]:
        agent = self.registry.get(speaker_uri) or self._retired.get(speaker_uri)
        if agent is None and self.status:
            agent = self.status.agents.get(speaker_uri)
        operation = {"operationId": str(uuid.uuid4()), "speakerUri": speaker_uri,
                     "agentId": agent.instance_id if agent else None, "action": action,
                     "requested": True, "requestedAt": time.time(),
                     "acknowledged": None, "enforced": False, "outcome": "requested"}
        self.operations.append(operation)
        del self.operations[:-100]
        return operation

    async def remove(self, speaker_uri: str, *, mode: str = "force", timeout: float = 10.0) -> dict[str, Any]:
        """Drain the current turn or detach immediately, then cancel and join.

        Draining excludes new turns/assignments but keeps the current result on
        the bus. A deadline switches to force removal; it never waits forever.
        """
        if mode not in {"force", "graceful"} or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("Removal requires force/graceful mode and a timeout in (0, 300]")
        operation = self._operation(speaker_uri, "remove:" + mode)
        async with self._lock:
            agent = self.registry.get(speaker_uri)
            if agent is None:
                operation.update(acknowledged=True, enforced=True, outcome="already-removed")
                return operation
            self._retired[speaker_uri] = agent
            agent._draining = True
            self.floor.drain_agent(speaker_uri)
            if mode == "graceful" and agent._status:
                agent._status.transition("lifecycle", "draining", reason="removed")
                agent._status.activity = "draining"
            operation.update(acknowledged=True, outcome="draining" if mode == "graceful" else "stopping")
        forced = mode == "force"
        if mode == "graceful":
            deadline = asyncio.get_running_loop().time() + timeout
            while agent.work_in_progress and self.registry.get(speaker_uri) is agent:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    forced = True
                    break
                await asyncio.sleep(min(0.02, remaining))
        await self._detach(speaker_uri)
        operation.update(enforced=True, outcome="force-stopped" if forced else "drained")
        return operation

    async def _detach(self, speaker_uri: str) -> None:
        """Detach first so queued/late output cannot re-enter the conversation."""
        async with self._lock:
            # Only an in-flight worker's removal hands the floor back to the
            # orchestrator. Removing anyone else (e.g. the orchestrator's own
            # [KICK] while it holds the floor) must not grant it a second turn.
            was_in_flight_worker = (
                self.floor._orchestrator_uri is not None
                and speaker_uri != self.floor._orchestrator_uri
                and self.floor._is_expected_worker(speaker_uri)
            )
            name = self.floor.active_agents.get(speaker_uri, speaker_uri.split(":")[-1])
            next_holder = self.floor.unregister_agent(speaker_uri)
            await self.bus.unregister(speaker_uri)
            await self.registry.stop_agent(speaker_uri)
            if next_holder is not None:
                await self.floor.grant_to(next_holder)
            elif was_in_flight_worker:
                await self.floor._report_worker_removed(name)

    async def set_governance(self, speaker_uri: str, value: str, *, source: str = "operator") -> dict[str, Any]:
        async with self._governance_lock:
            return await self._set_governance(speaker_uri, value, source=source)

    async def _set_governance(self, speaker_uri: str, value: str, *, source: str) -> dict[str, Any]:
        """Record independent authority and apply the local quarantine adapter.

        Quarantine cancels and removes this instance. Clearing its retained
        assertion never restarts it or repeats a billable request.
        """
        if value not in {"normal", "quarantined", "under-review"} or source not in {"operator", "guardrail"}:
            raise ValueError("Unsupported governance operation")
        operation = self._operation(speaker_uri, "governance:" + value)
        agent = self.registry.get(speaker_uri) or self._retired.get(speaker_uri)
        if agent is None and self.status:
            agent = self.status.agents.get(speaker_uri)
        if agent is None:
            operation.update(acknowledged=False, error="unknown-agent", outcome="rejected")
            return operation
        evidence = {"acknowledged": None, "error": "status-control-unavailable"}
        if self.status:
            evidence = await self.status.connection.assert_governance(
                agent.instance_id, value, source=source, operation_id=operation["operationId"])
        operation.update(evidence)
        if evidence["acknowledged"] is False:
            operation["outcome"] = "rejected"
            return operation
        if value == "quarantined":
            self._retired[speaker_uri] = agent
            agent._draining = True
            self.floor.drain_agent(speaker_uri)
            await self._detach(speaker_uri)
            operation.update(enforced=True, outcome="quarantine-enforced")
        elif value == "normal":
            cleared = evidence["acknowledged"] is True
            operation.update(enforced=cleared, outcome="cleared-no-restart" if cleared else "clear-unconfirmed-no-restart")
        else:
            operation.update(enforced=False, outcome="observation-only")
        if self.status and evidence["acknowledged"] is True:
            final = await self.status.connection.assert_governance(
                agent.instance_id, value, source=source, operation_id=operation["operationId"],
                enforcement="enforced" if operation["enforced"] else "observation-only")
            operation["enforcementAcknowledged"] = final["acknowledged"]
        return operation

    async def close(self) -> None:
        self.floor.stop()
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            for uri in self.floor.active_agents:
                self.floor.unregister_agent(uri)
                await self.bus.unregister(uri)
            await self.registry.close()
            await self.bus.unregister(self.floor.speaker_uri)
            if self.status:
                await self.status.connection.close()

    async def run(self, *jobs: Coroutine[Any, Any, None]) -> None:
        """A floor ending (or a child failing) ends the session and all its jobs."""
        if self._closed:
            for job in jobs:
                job.close()
            raise RuntimeError("Session is closed")
        floor_task = asyncio.create_task(self.floor.run(), name="session:floor")
        background = [asyncio.create_task(job, name="session:job") for job in jobs]
        stop_task = asyncio.create_task(self.floor._stop_event.wait(), name="session:stop")
        try:
            while not floor_task.done() and not stop_task.done():
                watched = {floor_task, stop_task, *background, *self.registry.tasks}
                done, _ = await asyncio.wait(
                    watched, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    if not task.cancelled():
                        error = task.exception()
                        if error is not None:
                            raise error
                    if task in self.registry.tasks:
                        self.floor.stop()
                background = [task for task in background if not task.done()]
            if floor_task.done() and not floor_task.cancelled():
                floor_task.result()
        finally:
            self.floor.stop()
            for task in [floor_task, stop_task, *background]:
                task.cancel()
            await asyncio.gather(floor_task, stop_task, *background, return_exceptions=True)
            await self.close()
