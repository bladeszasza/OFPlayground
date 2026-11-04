"""Authenticated ingest/observation and safe error boundaries for ASP HTTP."""
from __future__ import annotations

import json

import httpx
import pytest

from ofp_playground.status.api import build_status_app, encode_sse
from ofp_playground.status.models import Credential
from ofp_playground.status.store import StatusStore
from tests.test_asp_store import Clock, event, SELF

SELF_TOKEN = "s" * 40
READ_TOKEN = "r" * 40
ADMIN_TOKEN = "a" * 40


@pytest.fixture
async def api(tmp_path):
    status = StatusStore(tmp_path / "status.db", clock=Clock())
    app = build_status_app(status, {
        SELF_TOKEN: SELF,
        READ_TOKEN: Credential("reader", "peer", frozenset({"a"}), observe=True, ingest=False),
        ADMIN_TOKEN: Credential("admin", "operator", frozenset({"*"}), observe=True),
    }, monitor=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://status") as client:
            yield client, status, app
    status.close()


def auth(token=SELF_TOKEN):
    return {"Authorization": "Bearer " + token}


async def register(client):
    response = await client.post("/v1/events", json=event(0, "register", heartbeatIntervalSec=2).payload(), headers=auth())
    assert response.status_code == 202


@pytest.mark.parametrize("path", ["/v1/events", "/v1/events:batch", "/v1/credentials"])
async def test_ingest_requires_credentials_before_reading_content(api, path):
    client, _, _ = api
    response = await client.post(path, content=b"not JSON")
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/v1/agents", "/v1/agents/a", "/v1/history", "/v1/stream"])
async def test_observation_requires_authorized_observer(api, path):
    client, _, _ = api
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=auth())).status_code == 403


async def test_bound_identity_source_and_read_only_credentials(api):
    client, _, _ = api
    await register(client)
    impersonated = event(1, "state_change", source="operator", dimension="governance", **{"from": "normal", "to": "normal"})
    assert (await client.post("/v1/events", json=impersonated.payload(), headers=auth())).status_code == 403
    assert (await client.post("/v1/events", json=event(1).payload(), headers=auth(READ_TOKEN))).status_code == 403
    wrong_target = event(1).payload()
    wrong_target["agentId"] = "other"
    assert (await client.post("/v1/events", json=wrong_target, headers=auth())).status_code == 403


async def test_validation_rejects_content_without_echoing_it(api):
    client, _, _ = api
    await register(client)
    payload = {**event(1).payload(), "prompt": "secret-key-never-echo-this"}
    response = await client.post("/v1/events", json=payload, headers=auth())
    assert response.status_code == 400 and "secret-key" not in response.text
    response = await client.post("/v1/events", content=b"x" * 17000, headers=auth())
    assert response.status_code == 413
    assert (await client.post("/v1/events", content=b"{", headers=auth())).status_code == 400


async def test_batch_has_explicit_partial_outcomes_and_dedup(api):
    client, status, _ = api
    register_event = event(0, "register", heartbeatIntervalSec=2).payload()
    result = await client.post("/v1/events:batch", json=[register_event, {"prompt": "x"}, register_event, event(1).payload()], headers=auth())
    assert result.status_code == 202
    outcomes = result.json()["results"]
    assert [r["status"] for r in outcomes] == [202, 400, 202, 202]
    assert outcomes[2]["duplicate"]
    assert status.cursor == 2
    assert (await client.post("/v1/events:batch", json=[register_event] * 65, headers=auth())).status_code == 400


async def test_snapshots_history_and_credential_issuance_revocation(api):
    client, status, app = api
    await register(client)
    snapshot = await client.get("/v1/agents", headers=auth(READ_TOKEN))
    assert snapshot.json()["cursor"] == 1
    assert snapshot.json()["agents"][0]["agentId"] == "a"
    assert (await client.get("/v1/agents/other", headers=auth(READ_TOKEN))).status_code == 403
    assert (await client.get("/v1/agents/other", headers=auth(ADMIN_TOKEN))).status_code == 404
    history = await client.get("/v1/history", headers=auth(READ_TOKEN))
    assert len(history.json()["events"]) == 1
    issued = await client.post("/v1/credentials", json={"agentId": "new-instance"}, headers=auth(ADMIN_TOKEN))
    assert issued.status_code == 201
    token = issued.json()["token"]
    payload = event(0, "register", heartbeatIntervalSec=2).payload()
    payload["agentId"] = "new-instance"
    assert (await client.post("/v1/events", json=payload, headers=auth(token))).status_code == 202
    assert (await client.delete("/v1/credentials/agent:new-instance", headers=auth(ADMIN_TOKEN))).status_code == 200
    assert (await client.post("/v1/events", json=payload, headers=auth(token))).status_code == 401
    assert token not in json.dumps(status.snapshot()) and token not in json.dumps(status.history())
    assert token not in str(status.db.execute("SELECT * FROM credentials").fetchall())


async def test_ingest_rate_limit_is_per_principal(tmp_path):
    status = StatusStore(tmp_path / "db")
    app = build_status_app(status, {SELF_TOKEN: SELF}, rate_limit=1, monitor=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://status") as client:
        await register(client)
        refused = await client.post("/v1/events", json=event(1).payload(), headers=auth())
        assert refused.status_code == 429 and "Retry-After" in refused.headers
    status.close()


def test_sse_uses_server_cursor_and_raw_asp_event():
    payload = event(100).payload()
    encoded = encode_sse({"cursor": 4, "event": payload}).decode()
    assert "id: 4\n" in encoded and "event: heartbeat\n" in encoded
    assert json.loads(encoded.split("data: ")[1]) == payload


async def test_transition_history_keeps_recent_changes_when_heartbeats_dominate(api):
    client, _store, _app = api
    await register(client)
    transition = event(1, "state_change", dimension="health", **{"from": "healthy", "to": "degraded"}).payload()
    assert (await client.post("/v1/events", json=transition, headers=auth())).status_code == 202
    for seq in range(2, 20):
        assert (await client.post("/v1/events", json=event(seq).payload(), headers=auth())).status_code == 202
    response = await client.get("/v1/history", params={"latest": "true", "transitions": "true", "limit": 1}, headers=auth(READ_TOKEN))
    assert [record["event"]["to"] for record in response.json()["events"]] == ["degraded"]
    invalid_metric = {**event(21).payload(), "metrics": {"activeTasks": True}}
    assert (await client.post("/v1/events", json=invalid_metric, headers=auth())).status_code == 400
