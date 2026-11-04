"""Provider-free regression tests for lifetime, removal and real floor traffic."""
from __future__ import annotations

import asyncio

import pytest

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.agents.registry import AgentRegistry
from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.floor.manager import FloorManager
from ofp_playground.floor.policy import FloorPolicy
from ofp_playground.session import SessionService


class ScriptedAgent(BasePlaygroundAgent):
    def __init__(self, name, bus, floor, *, speak=False):
        super().__init__(f"tag:test:{name}", name, "local://test", bus, floor.conversation_id)
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.speak = speak

    async def run(self):
        self.started.set()
        try:
            if self.speak:
                await self.request_floor()
            while True:
                envelope = await self.queue.get()
                if self.speak and any(e.eventType == "grantFloor" for e in envelope.events):
                    await self.send_envelope(self._make_utterance_envelope(f"Result from {self.name}"))
                    await self.yield_floor()
        finally:
            self.finished.set()


@pytest.fixture
async def session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bus = MessageBus()
    floor = FloorManager(bus)
    registry = AgentRegistry()
    service = SessionService(floor, bus, registry)
    await bus.register(floor.speaker_uri, floor._queue)
    yield service
    await service.close()


async def add(service, name, *, speak=False):
    agent = ScriptedAgent(name, service.bus, service.floor, speak=speak)
    service.floor.register_agent(agent.speaker_uri, agent.name)
    service.registry.register(agent)
    await service.bus.register(agent.speaker_uri, agent.queue)
    service.registry.start(agent)
    await agent.started.wait()
    return agent


async def test_early_floor_completion_cancels_watchdog_and_joins_agents(session):
    agent = await add(session, "A")
    watchdog_finished = asyncio.Event()

    async def watchdog():
        try:
            await asyncio.Event().wait()
        finally:
            watchdog_finished.set()

    run = asyncio.create_task(session.run(watchdog()))
    await asyncio.sleep(0)
    session.floor.stop()  # e.g. TASK_COMPLETE before max_turns
    await asyncio.wait_for(run, timeout=1)
    assert agent.finished.is_set()
    assert watchdog_finished.is_set()
    assert session.registry.tasks == ()
    assert session.bus.registered_agents == []


async def test_removing_holder_transfers_floor_and_suppresses_late_output(session):
    a = await add(session, "A")
    b = await add(session, "B")
    session.floor._policy.grant_to(a.speaker_uri)
    session.floor._policy.request_floor(b.speaker_uri)
    await session.remove(a.speaker_uri)
    assert a.finished.is_set()
    assert session.floor.floor_holder == b.speaker_uri
    assert session.floor._policy.queue == []
    grant = b.queue.get_nowait()
    assert grant.events[0].eventType == "grantFloor"
    late = a._make_utterance_envelope("stale provider result")
    await session.bus.send(late)
    assert b.queue.empty()
    await session.floor.process_envelope(late)  # already queued before removal
    assert len(session.floor.history) == 0


async def test_removing_queued_agent_preserves_holder(session):
    a = await add(session, "A")
    b = await add(session, "B")
    session.floor._policy.grant_to(a.speaker_uri)
    session.floor._policy.request_floor(b.speaker_uri)
    await session.remove(b.speaker_uri)
    assert session.floor.floor_holder == a.speaker_uri
    assert session.floor._policy.queue == []


async def test_two_scripted_agents_complete_through_real_bus_and_floor(session):
    await add(session, "A", speak=True)
    await add(session, "B", speak=True)

    async def stop_at_two():
        while len(session.floor.history) < 2:
            await asyncio.sleep(0.01)
        session.floor.stop()

    await asyncio.wait_for(session.run(stop_at_two()), timeout=2)
    assert [entry.speaker_name for entry in session.floor.history.all()] == ["A", "B"]
    assert session.registry.tasks == ()
    assert session.bus.registered_agents == []


async def test_failed_child_propagates_error_and_cleans_siblings(session):
    sibling = await add(session, "A")
    failed = await add(session, "B")
    await session.registry.stop_agent(failed.speaker_uri)

    async def fail():
        raise RuntimeError("scripted failure")

    failed.run = fail
    session.registry.register(failed)
    session.registry.start(failed)
    with pytest.raises(RuntimeError, match="scripted failure"):
        await session.run()
    assert sibling.finished.is_set()
    assert session.bus.registered_agents == []


@pytest.mark.parametrize("policy", list(FloorPolicy))
async def test_non_holder_cannot_advance_a_waiting_queue(session, policy):
    controller = session.floor._policy
    controller.policy = policy
    controller.add_to_rotation("A")
    controller.add_to_rotation("B")
    controller.grant_to("A")
    if policy != FloorPolicy.FREE_FOR_ALL:
        controller.request_floor("B")
    before = controller.queue
    assert controller.yield_floor("intruder") is None
    assert controller.current_holder == "A"
    assert controller.queue == before


async def test_round_robin_removing_holder_uses_its_actual_successor(session):
    controller = session.floor._policy
    controller.policy = FloorPolicy.ROUND_ROBIN
    for uri in ("A", "B", "C"):
        controller.add_to_rotation(uri)
    controller.grant_to("B")
    assert controller.remove_from_rotation("B") == "C"
    assert controller.yield_floor("C") == "A"


async def test_revoking_previous_holder_does_not_revoke_new_holder(session):
    a = await add(session, "A")
    b = await add(session, "B")
    session.floor._policy.grant_to(b.speaker_uri)
    await session.floor._revoke_floor(a.speaker_uri)
    assert session.floor.floor_holder == b.speaker_uri
    assert b.queue.empty()
