"""Real session/agent/floor work publishes ASP at execution boundaries."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.agents.registry import AgentRegistry
from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.floor.manager import FloorManager
from ofp_playground.session import SessionService
from ofp_playground.status.api import build_status_app
from ofp_playground.status.client import StatusConnection
from ofp_playground.status.models import Credential
from ofp_playground.status.store import StatusStore


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = StatusStore(tmp_path / "status.db")
    config = {key: key * 4 for key in ("operatorToken", "observerToken", "supervisorToken", "guardrailToken")}
    app = build_status_app(store, {
        config["operatorToken"]: Credential("operator", "operator", frozenset({"*"}), observe=True),
        config["observerToken"]: Credential("reader", "peer", frozenset({"*"}), observe=True, ingest=False),
        config["supervisorToken"]: Credential("supervisor", "supervisor", frozenset({"*"})),
        config["guardrailToken"]: Credential("guardrail", "guardrail", frozenset({"*"})),
    }, monitor=False)
    connection = StatusConnection(httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://status"), config)
    connection.start()
    bus = MessageBus()
    floor = FloorManager(bus)
    service = SessionService(floor, bus, AgentRegistry())
    await bus.register(floor.speaker_uri, floor.queue)
    await service.enable_status(connection)
    yield service, store, connection
    await service.close()
    store.close()


class Worker(BasePlaygroundAgent):
    def __init__(self, service, *, retry=False, block=False, crash=False):
        super().__init__("urn:test:worker", "Worker", "local://worker", service.bus, service.floor.conversation_id)
        self.entered = asyncio.Event()
        self.completed = asyncio.Event()
        self.release = asyncio.Event()
        self.attempts = 0
        self.retry = retry
        self.block = block
        self.crash = crash
        self._retry_delay = 0

    async def work(self):
        self.entered.set()
        self.attempts += 1
        if self.retry and self.attempts == 1:
            raise RuntimeError("429 rate limit: a secret that must never enter ASP")
        if self.block:
            await self.release.wait()
        return "result"

    async def run(self):
        if self.crash:
            raise RuntimeError("private crash detail")
        await self.request_floor()
        while True:
            envelope = await self.queue.get()
            if any(e.eventType == "grantFloor" for e in envelope.events):
                result = await self._call_with_retry(self.work)
                await self.send_envelope(self._make_utterance_envelope(result))
                await self.yield_floor()
                self.completed.set()


async def add(service, **options):
    agent = Worker(service, **options)
    service.floor.register_agent(agent.speaker_uri, agent.name)
    service.registry.register(agent)
    await service.bus.register(agent.speaker_uri, agent.queue)
    service.registry.start(agent)
    return agent


async def test_actual_provider_retry_task_and_floor_provenance(runtime):
    service, store, connection = runtime
    worker = await add(service, retry=True)
    run = asyncio.create_task(service.run())
    await asyncio.wait_for(worker.completed.wait(), 2)
    await asyncio.wait_for(connection.queue.join(), 2)
    agent = store.agents[worker.instance_id]
    assert worker.instance_id != worker.speaker_uri
    assert agent["ext"]["local.ofp-playground"]["speakerUri"] == worker.speaker_uri
    assert agent["floors"][service.floor.conversation_id]["value"] == "yielded"
    assert agent["floors"][service.floor.conversation_id]["source"] == "supervisor"
    transitions = [r["event"]["to"] for r in store.history() if r["event"].get("dimension") == "task"]
    assert transitions == ["working", "retrying", "working", "completed"]
    assert worker.attempts == 2
    assert "secret" not in str(store.history())
    service.floor.stop()
    await asyncio.wait_for(run, 2)
    assert store.agents[worker.instance_id]["lifecycle"]["value"] == "offline"


async def test_removal_cancels_real_work_and_retains_terminal_status(runtime):
    service, store, connection = runtime
    worker = await add(service, block=True)
    run = asyncio.create_task(service.run())
    await asyncio.wait_for(worker.entered.wait(), 2)
    await service.remove(worker.speaker_uri)
    await asyncio.wait_for(connection.queue.join(), 2)
    agent = store.agents[worker.instance_id]
    assert [t["value"] for t in agent["tasks"].values()] == ["canceled"]
    assert agent["lifecycle"]["value"] == "offline"
    assert agent["floors"][service.floor.conversation_id]["value"] == "left"
    service.floor.stop()
    await asyncio.wait_for(run, 2)


async def test_failed_child_has_independent_crash_assertion(runtime):
    service, store, connection = runtime
    worker = await add(service, crash=True)
    with pytest.raises(RuntimeError, match="private crash"):
        await asyncio.wait_for(service.run(), 2)
    agent = store.agents[worker.instance_id]
    assert agent["lifecycle"]["value"] == "crashed"
    assert agent["lifecycle"]["source"] == "supervisor"
    assert "private crash" not in str(store.history())


async def test_graceful_removal_preserves_active_result_and_excludes_future_grants(runtime):
    service, store, connection = runtime
    worker = await add(service, block=True)
    run = asyncio.create_task(service.run())
    await asyncio.wait_for(worker.entered.wait(), 2)
    remove = asyncio.create_task(service.remove(worker.speaker_uri, mode="graceful", timeout=1))
    await asyncio.sleep(0)
    assert worker._draining
    assert not remove.done()
    await service.floor.grant_to(worker.speaker_uri)  # no additional grant can be queued
    worker.release.set()
    result = await asyncio.wait_for(remove, 2)
    assert result["requested"] and result["acknowledged"] and result["enforced"]
    assert result["outcome"] == "drained"
    assert worker.completed.is_set()
    assert [row.text for row in service.floor.history.all()] == ["result"]
    await asyncio.wait_for(connection.queue.join(), 2)
    assert [task["value"] for task in store.agents[worker.instance_id]["tasks"].values()] == ["completed"]
    assert worker.speaker_uri not in service.bus.registered_agents
    service.floor.stop()
    await asyncio.wait_for(run, 2)


async def test_graceful_deadline_switches_to_force_and_cancels_task(runtime):
    service, store, connection = runtime
    worker = await add(service, block=True)
    run = asyncio.create_task(service.run())
    await asyncio.wait_for(worker.entered.wait(), 2)
    result = await service.remove(worker.speaker_uri, mode="graceful", timeout=0.01)
    assert result["outcome"] == "force-stopped"
    assert result["enforced"]
    await asyncio.wait_for(connection.queue.join(), 2)
    assert [task["value"] for task in store.agents[worker.instance_id]["tasks"].values()] == ["canceled"]
    service.floor.stop()
    await asyncio.wait_for(run, 2)


async def test_quarantine_enforces_removal_and_clearing_retains_offline_history(runtime):
    service, store, connection = runtime
    worker = await add(service, block=True)
    run = asyncio.create_task(service.run())
    await asyncio.wait_for(worker.entered.wait(), 2)
    result = await service.set_governance(worker.speaker_uri, "quarantined", source="guardrail")
    assert result["acknowledged"] and result["enforced"] and result["enforcementAcknowledged"]
    assert service.registry.get(worker.speaker_uri) is None
    assert worker.speaker_uri not in service.floor.active_agents
    assert worker.speaker_uri not in service.bus.registered_agents
    governance = store.agents[worker.instance_id]["governance"]
    assert governance["source"] == "guardrail"
    assert governance["ext"]["local.ofp-playground"]["enforcement"] == "enforced"
    assert governance["reason"] == "policy-restriction"
    assert store.agents[worker.instance_id]["lifecycle"]["value"] == "offline"
    connection.disabled = True
    uncertain = await service.set_governance(worker.speaker_uri, "normal")
    assert uncertain["acknowledged"] is None and not uncertain["enforced"]
    assert uncertain["outcome"] == "clear-unconfirmed-no-restart"
    assert store.agents[worker.instance_id]["governance"]["value"] == "quarantined"
    assert service.registry.get(worker.speaker_uri) is None
    connection.disabled = False
    cleared = await service.set_governance(worker.speaker_uri, "normal")
    assert cleared["outcome"] == "cleared-no-restart" and cleared["acknowledged"]
    assert service.registry.get(worker.speaker_uri) is None
    assert store.agents[worker.instance_id]["governance"]["value"] == "normal"
    service.floor.stop()
    await asyncio.wait_for(run, 2)


async def test_lower_authority_restriction_refusal_does_not_enforce(runtime):
    service, store, connection = runtime
    worker = await add(service, block=True)
    run = asyncio.create_task(service.run())
    await asyncio.wait_for(worker.entered.wait(), 2)
    await service.set_governance(worker.speaker_uri, "under-review")
    result = await service.set_governance(worker.speaker_uri, "quarantined", source="guardrail")
    assert result["acknowledged"] is False and not result["enforced"]
    assert service.registry.get(worker.speaker_uri) is worker
    assert store.agents[worker.instance_id]["governance"]["value"] == "under-review"
    service.floor.stop()
    await asyncio.wait_for(run, 2)


async def test_monitor_outage_does_not_prevent_local_quarantine(runtime):
    service, store, connection = runtime
    worker = await add(service, block=True)
    run = asyncio.create_task(service.run())
    await asyncio.wait_for(worker.entered.wait(), 2)
    connection.disabled = True
    result = await service.set_governance(worker.speaker_uri, "quarantined")
    assert result["acknowledged"] is None and result["enforced"]
    assert result["error"] == "status-control-unavailable"
    assert worker.speaker_uri not in service.floor.active_agents
    assert service.registry.get(worker.speaker_uri) is None
    service.floor.stop()
    await asyncio.wait_for(run, 2)


@pytest.mark.parametrize("kind", ["breakout", "coding"])
async def test_child_floor_instrumentation_and_parent_scope_survive_cleanup(runtime, tmp_path, kind):
    from ofp_playground.agents.demo import DemoAgent
    from ofp_playground.floor.breakout import run_breakout_session
    from ofp_playground.floor.coding_session import run_coding_session
    from ofp_playground.floor.policy import FloorPolicy

    service, store, connection = runtime
    agents = [DemoAgent(name=f"Child{i}", synopsis="Synthetic child work", bus=service.bus,
                        conversation_id=service.floor.conversation_id) for i in range(2)]
    options = dict(topic="Synthetic nested status test", agents=agents, policy=FloorPolicy.ROUND_ROBIN,
                   parent_conversation_id=service.floor.conversation_id, max_rounds=2,
                   status_runtime=service.status)
    if kind == "coding":
        options["sandbox_dir"] = tmp_path / "sandbox"
    result = await asyncio.wait_for((run_coding_session if kind == "coding" else run_breakout_session)(**options), 10)
    await asyncio.wait_for(connection.queue.join(), 2)
    assert result.round_count == 2
    assert len(service.status.children) == 1
    child = service.status.children[0]
    assert child.conversation_id != service.floor.conversation_id
    assert {agent.instance_id for agent in service.status.all_agents()} == {a.instance_id for a in agents}
    for agent in agents:
        state = store.agents[agent.instance_id]
        assert state["ext"]["local.ofp-playground"]["parentConversationId"] == service.floor.conversation_id
        assert state["floors"][child.conversation_id]["value"] == "left"
        assert state["lifecycle"]["value"] == "offline"
        assert state["tasks"] and all(task["value"] in {"completed", "canceled"} for task in state["tasks"].values())
        assert agent._status is None
    assert not connection.problem
