"""Shared session lifetime and participant control for CLI, Canvas and web."""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ofp_playground.status.client import StatusConnection
    from ofp_playground.status.runtime import SessionStatusRuntime

from ofp_playground.agents.registry import AgentRegistry
from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.floor.manager import FloorManager


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
            next_holder = self.floor.unregister_agent(speaker_uri)
            await self.bus.unregister(speaker_uri)
            await self.registry.stop_agent(speaker_uri)
            if next_holder is not None:
                await self.floor.grant_to(next_holder)
            elif self.floor._orchestrator_uri and self.floor._assigned_uri is None:
                await self.floor._maybe_return_floor_to_orchestrator()

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
