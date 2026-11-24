"""Shared ASP status server support: operator governance and the opt-in session mirror.

The fake server below follows the reference implementation's rules
(github.com/AgentStatusProtocol/spec, implementations/python-status-server):
one bearer token, register before anything else (409), per-agent/per-source
``seq`` ordering, and operator > guardrail > supervisor > self for governance.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import unquote

import httpx
import pytest
from click.testing import CliRunner

from ofp_playground.status import remote
from ofp_playground.status.remote import (
    RemoteMirror,
    RemoteStatusError,
    RemoteStatusServer,
    mirror_from_env,
    normalize_server,
)

TOKEN = "t" * 40
AUTHORITY = {"self": 0, "peer": 0, "supervisor": 1, "guardrail": 2, "operator": 3}


class FakeReferenceServer:
    def __init__(self) -> None:
        self.agents: dict[str, dict] = {}
        self.events: list[dict] = []
        self.ignored: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})
        assert "token" not in str(request.url), "tokens must never travel in URLs"
        path = request.url.path
        if request.method == "GET" and path == "/v1/agents":
            return httpx.Response(200, json=list(self.agents.values()))
        if request.method == "GET" and path.startswith("/v1/agents/"):
            agent = self.agents.get(unquote(path[len("/v1/agents/"):]))
            return httpx.Response(200, json=agent) if agent else httpx.Response(404, json={"error": "unknown agent"})
        if request.method == "POST" and path == "/v1/events":
            return self.ingest(json.loads(request.content))
        return httpx.Response(404, json={"error": "not found"})

    def ingest(self, event: dict) -> httpx.Response:
        source = event.get("source", "self")
        agent = self.agents.get(event["agentId"])
        if event["type"] == "register":
            agent = self.agents[event["agentId"]] = {
                "agentId": event["agentId"], "name": event.get("name", event["agentId"]),
                "lifecycle": "online", "health": "healthy", "governance": "normal",
                "governanceSetBy": "self", "lastSeq": {},
            }
        elif agent is None:
            return httpx.Response(409, json={"error": "event sent before register"})
        if event["seq"] <= agent["lastSeq"].get(source, -1):
            self.ignored.append(event)
            return httpx.Response(202, json={"accepted": 1})
        if event.get("dimension") == "governance":
            if AUTHORITY[source] < AUTHORITY[agent["governanceSetBy"]]:
                return httpx.Response(409, json={"error": "lower authority"})
            agent["governance"], agent["governanceSetBy"] = event["to"], source
        if event["type"] == "deregister":
            agent["lifecycle"] = "offline"
        agent["lastSeq"][source] = event["seq"]
        self.events.append(event)
        return httpx.Response(202, json={"accepted": 1})


def _register(server: FakeReferenceServer, agent_id: str = "urn:agent:test:mine") -> None:
    server.ingest({"aspVersion": "0.1", "type": "register", "agentId": agent_id, "eventId": "e0",
                   "seq": 0, "ts": "2026-11-24T10:00:00Z", "name": "Mine", "heartbeatIntervalSec": 5})


def _client(server: FakeReferenceServer, sleeps: list[float] | None = None) -> RemoteStatusServer:
    return RemoteStatusServer("https://asp.example", TOKEN, transport=httpx.MockTransport(server.handle),
                              sleep=(sleeps.append if sleeps is not None else (lambda _s: None)))


def test_server_urls_require_https_unless_local() -> None:
    assert normalize_server("https://asp.example/") == "https://asp.example"
    assert normalize_server("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    for bad in ("http://asp.example", "ftp://asp.example", "https://asp.example/?token=x", "asp.example"):
        with pytest.raises(RemoteStatusError):
            normalize_server(bad)


def test_list_and_quarantine_then_release_as_operator() -> None:
    server = FakeReferenceServer()
    _register(server)
    with _client(server) as client:
        assert [a["agentId"] for a in client.agents()] == ["urn:agent:test:mine"]
        after = client.set_governance("urn:agent:test:mine", "quarantined")
        assert (after["governance"], after["governanceSetBy"]) == ("quarantined", "operator")
        sent = server.events[-1]
        assert (sent["source"], sent["from"], sent["to"]) == ("operator", "normal", "quarantined")
        assert sent["reason"] == "operator-action"
        assert sent["seq"] > 1_000_000_000  # seconds, like the reference operator tools
        client.set_governance("urn:agent:test:mine", "normal")
    assert server.agents["urn:agent:test:mine"]["governance"] == "normal"


def test_same_second_operator_event_is_retried_once() -> None:
    server = FakeReferenceServer()
    _register(server)
    server.agents["urn:agent:test:mine"]["lastSeq"]["operator"] = 10**12  # a later operator seq
    sleeps: list[float] = []
    with _client(server, sleeps) as client, pytest.raises(RemoteStatusError, match="not applied"):
        client.set_governance("urn:agent:test:mine", "sandboxed")
    assert len(server.ignored) == 2 and sleeps == [1.1, 1.1]


def test_unknown_agent_and_bad_token_have_clear_messages() -> None:
    server = FakeReferenceServer()
    with _client(server) as client, pytest.raises(RemoteStatusError, match="No agent"):
        client.set_governance("urn:agent:test:missing", "quarantined")
    wrong = RemoteStatusServer("https://asp.example", "wrong",
                               transport=httpx.MockTransport(server.handle))
    with wrong as client, pytest.raises(RemoteStatusError, match="ASP_TOKEN"):
        client.agents()


def test_governance_cli_lists_and_sets(monkeypatch) -> None:
    from ofp_playground import cli

    server = FakeReferenceServer()
    _register(server)

    class Patched(RemoteStatusServer):
        def __init__(self, url, token, **kwargs):
            super().__init__(url, token, transport=httpx.MockTransport(server.handle), sleep=lambda _s: None)

    monkeypatch.setattr(remote, "RemoteStatusServer", Patched)
    monkeypatch.setenv("ASP_SERVER", "https://asp.example")
    monkeypatch.setenv("ASP_TOKEN", TOKEN)
    runner = CliRunner()
    listed = runner.invoke(cli.main, ["governance"])
    assert listed.exit_code == 0, listed.output
    assert "urn:agent:test:mine  (Mine)" in listed.output and "governance normal" in listed.output
    changed = runner.invoke(cli.main, ["governance", "urn:agent:test:mine", "under-review"])
    assert changed.exit_code == 0, changed.output
    assert "under-review (set by operator)" in changed.output
    assert TOKEN not in listed.output + changed.output
    missing_state = runner.invoke(cli.main, ["governance", "urn:agent:test:mine"])
    assert missing_state.exit_code != 0


def test_governance_cli_without_server_explains(monkeypatch) -> None:
    from ofp_playground import cli

    monkeypatch.delenv("ASP_SERVER", raising=False)
    result = CliRunner().invoke(cli.main, ["governance"])
    assert result.exit_code != 0 and "ASP_SERVER" in result.output


async def test_mirror_prefixes_ids_strips_ext_and_keeps_order() -> None:
    server = FakeReferenceServer()
    mirror = RemoteMirror("https://asp.example", TOKEN, prefix="urn:agent:ofplayground:",
                          transport=httpx.MockTransport(server.handle))
    mirror.start()
    base = {"aspVersion": "0.1", "agentId": "abc", "ts": "2026-11-24T10:00:00Z"}
    mirror.submit({**base, "type": "register", "eventId": "1", "seq": 0, "name": "Ada",
                   "heartbeatIntervalSec": 2, "ext": {"local.ofp-playground": {"speakerUri": "tag:x"}}})
    mirror.submit({**base, "type": "heartbeat", "eventId": "2", "seq": 1, "activity": "idle"})
    mirror.submit({**base, "type": "state_change", "eventId": "3", "seq": 0, "source": "supervisor",
                   "dimension": "lifecycle", "from": "online", "to": "busy"})
    mirror.submit({**base, "type": "state_change", "eventId": "4", "seq": 10**18, "source": "operator",
                   "dimension": "governance", "from": "normal", "to": "quarantined"})
    await mirror.close()
    assert [e["eventId"] for e in server.events] == ["1", "2", "3", "4"]
    assert all(e["agentId"] == "urn:agent:ofplayground:abc" for e in server.events)
    assert all("ext" not in e for e in server.events)
    assert server.events[3]["seq"] < 10**12  # seconds, so reference operator tools still work
    assert server.agents["urn:agent:ofplayground:abc"]["governance"] == "quarantined"
    assert mirror.sent == 4 and mirror.dropped == 0


async def test_mirror_failure_never_raises() -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    mirror = RemoteMirror("https://asp.example", TOKEN, transport=httpx.MockTransport(down))
    mirror.start()
    mirror.submit({"aspVersion": "0.1", "type": "deregister", "agentId": "a", "eventId": "1",
                   "seq": 0, "ts": "2026-11-24T10:00:00Z"})
    await mirror.close(drain_timeout=0.2)
    assert mirror.problem == "mirror-unavailable"


async def test_status_connection_copies_validated_events_to_the_mirror() -> None:
    from ofp_playground.status.client import StatusConnection

    class Sink:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def submit(self, payload: dict) -> None:
            self.events.append(payload)

        async def close(self) -> None:
            pass

    local = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(202, json={})),
                              base_url="http://127.0.0.1:8787")
    connection = StatusConnection(local, {})
    sink = Sink()
    connection.mirror = sink  # type: ignore[assignment]
    connection.submit({"aspVersion": "0.1", "type": "heartbeat", "agentId": "a", "eventId": "1",
                       "seq": 0, "ts": "2026-11-24T10:00:00+00:00", "activity": "idle"}, "tok")
    connection.submit({"aspVersion": "0.1", "type": "heartbeat", "agentId": "a", "eventId": "2",
                       "seq": 1, "ts": "2026-11-24T10:00:00+00:00", "activity": "not-allowlisted"}, "tok")
    assert [e["eventId"] for e in sink.events] == ["1"]  # invalid events never leave the machine
    await connection.close()


def test_mirror_is_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("OFP_ASP_MIRROR", raising=False)
    monkeypatch.setenv("ASP_SERVER", "https://asp.example")
    assert mirror_from_env() is None
    monkeypatch.setenv("OFP_ASP_MIRROR", "1")
    monkeypatch.setenv("OFP_ASP_MIRROR_PREFIX", "bad prefix!")
    assert mirror_from_env() is None
    monkeypatch.delenv("OFP_ASP_MIRROR_PREFIX")
    monkeypatch.setenv("ASP_SERVER", "http://not-local.example")
    assert mirror_from_env() is None  # nonlocal http is refused


async def test_mirror_from_env_starts_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("OFP_ASP_MIRROR", "1")
    monkeypatch.setenv("ASP_SERVER", "http://127.0.0.1:9")
    monkeypatch.setenv("ASP_TOKEN", TOKEN)
    mirror = mirror_from_env()
    assert mirror is not None and mirror.prefix == "urn:agent:ofplayground:"
    await asyncio.wait_for(mirror.close(drain_timeout=0.1), timeout=5)
