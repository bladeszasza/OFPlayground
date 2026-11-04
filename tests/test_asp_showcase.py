"""Provider-free scenarios exercise actual work, liveness and control boundaries."""
from __future__ import annotations

import asyncio

import httpx

from ofp_playground.agents.demo import DemoAgent
from ofp_playground.agents.registry import AgentRegistry
from ofp_playground.agents.web_human import WebHumanAgent
from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.floor.manager import FloorManager
from ofp_playground.floor.policy import FloorPolicy
from ofp_playground.session import SessionService
from ofp_playground.status.api import build_status_app
from ofp_playground.status.client import StatusConnection
from ofp_playground.status.models import Credential
from ofp_playground.status.store import StatusStore


async def test_synthetic_runtime_failures_and_independent_enforcement(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = StatusStore(tmp_path / "status.db")
    config = {role + "Token": role * 9 for role in ("operator", "observer", "supervisor", "guardrail")}
    app = build_status_app(store, {
        config["operatorToken"]: Credential("operator", "operator", frozenset({"*"}), observe=True),
        config["observerToken"]: Credential("reader", "peer", frozenset({"*"}), observe=True, ingest=False),
        config["supervisorToken"]: Credential("supervisor", "supervisor", frozenset({"*"})),
        config["guardrailToken"]: Credential("guardrail", "guardrail", frozenset({"*"})),
    }, monitor=False)
    connection = StatusConnection(httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://status"), config)
    connection.start()
    bus = MessageBus()
    floor = FloorManager(bus, policy=FloorPolicy.FREE_FOR_ALL)
    session = SessionService(floor, bus, AgentRegistry())
    await session.enable_status(connection)
    await bus.register(floor.speaker_uri, floor.queue)
    demos = [DemoAgent(mode=mode, name=mode, synopsis="Synthetic local demonstration", bus=bus,
                       conversation_id=floor.conversation_id)
             for mode in ("normal", "rate-limit", "input-wait", "silent", "stalled")]
    human = WebHumanAgent(name="Operator", bus=bus, conversation_id=floor.conversation_id)
    for agent in [*demos, human]:
        floor.register_agent(agent.speaker_uri, agent.name)
        session.registry.register(agent)
        session.registry.start(agent)
    runner = asyncio.create_task(session.run())
    try:
        for _ in range(100):
            if len(store.agents) == 6:
                break
            await asyncio.sleep(.02)
        assert len(store.agents) == 6, (connection.problem, connection.agent_problems, runner.done(),
                                        [repr(t.exception()) if t.done() and not t.cancelled() else 'running' for t in session.registry.tasks])
        for agent in demos:
            await floor.grant_to(agent.speaker_uri)
        await asyncio.sleep(5.5)
        await asyncio.wait_for(connection.queue.join(), 3)
        store.tick()
        states = {agent.mode: store.agents[agent.instance_id] for agent in demos}
        assert states["silent"]["lifecycle"]["value"] == "unreachable"
        assert states["silent"]["lifecycle"]["source"] == "server"
        assert states["stalled"]["health"]["value"] == "stalled"
        assert states["input-wait"]["health"]["value"] != "stalled"
        assert any(t["value"] == "input-required" for t in states["input-wait"]["tasks"].values())
        assert any(t["value"] == "input-required" for t in store.agents[human.instance_id]["tasks"].values())
        retry_id = demos[1].instance_id
        assert any(r["event"].get("to") == "rate-limited" for r in store.history(agent_id=retry_id))
        assert any(r["event"].get("to") == "completed" for r in store.history(agent_id=retry_id))
        demos[2].resume_input()
        await asyncio.sleep(1)
        await asyncio.wait_for(connection.queue.join(), 3)
        assert any(t["value"] == "completed" for t in store.agents[demos[2].instance_id]["tasks"].values())
        operation = await session.set_governance(demos[4].speaker_uri, "quarantined", source="guardrail")
        assert operation["acknowledged"] is True and operation["enforced"] is True
        assert session.registry.get(demos[4].speaker_uri) is None
        await asyncio.wait_for(connection.queue.join(), 3)
        restricted = store.agents[demos[4].instance_id]
        assert restricted["governance"]["source"] == "guardrail"
        assert restricted["governance"]["ext"]["local.ofp-playground"]["enforcement"] == "enforced"
        assert restricted["lifecycle"]["value"] == "offline"
        assert not connection.problem
    finally:
        floor.stop()
        await asyncio.wait_for(runner, 4)
        store.close()
