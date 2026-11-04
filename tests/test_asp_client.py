"""Publishing isolation, stream cleanup and an independent liveness monitor."""
from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from ofp_playground.status.api import BoundedStreamingResponse, build_status_app
from ofp_playground.status.client import AgentStatusPublisher, StatusConnection, connect_local_service
from ofp_playground.status.models import Credential
from ofp_playground.status.store import StatusStore
from tests.test_asp_api import ADMIN_TOKEN, READ_TOKEN, auth


async def test_publisher_uses_real_ingest_order_and_work_boundaries(tmp_path):
    store = StatusStore(tmp_path / "db")
    app = build_status_app(store, {
        ADMIN_TOKEN: Credential("admin", "operator", frozenset({"*"}), observe=True),
        READ_TOKEN: Credential("reader", "peer", frozenset({"*"}), observe=True, ingest=False),
    }, monitor=False)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://status")
    connection = StatusConnection(client, {"operatorToken": ADMIN_TOKEN, "observerToken": READ_TOKEN})
    connection.start()
    try:
        token = await connection.issue("instance")
        assert token
        publisher = AgentStatusPublisher(connection, "instance", token)
        publisher.emit("register", heartbeatIntervalSec=2, stallAfterSec=60)
        publisher.transition("floor", "joined", conversation_id="conv")
        task = publisher.begin_work()
        publisher.finish_work(task, "completed", "work-completed")
        await asyncio.wait_for(connection.queue.join(), 1)
        agent = (await connection.snapshot())["agents"][0]
        assert agent["tasks"][task]["value"] == "completed"
        assert agent["lifecycle"]["value"] == "idle"
        assert agent["floors"]["conv"]["value"] == "joined"
        assert all(r["applied"] for r in store.history())
        assert connection.problem is None
    finally:
        await connection.close()
        store.close()


async def test_outage_does_not_block_work_and_overflow_stops_false_heartbeats():
    async def unavailable(_request):
        return httpx.Response(503)

    connection = StatusConnection(httpx.AsyncClient(transport=httpx.MockTransport(unavailable), base_url="http://status"), {}, capacity=1)
    connection.start()
    publisher = AgentStatusPublisher(connection, "instance", "token")
    publisher.emit("register", heartbeatIntervalSec=1)
    await asyncio.sleep(0)
    publisher.emit("heartbeat", progressSeq=0)
    publisher.emit("heartbeat", progressSeq=1)
    assert connection.disabled and connection.problem == "observation-buffer-overflow"
    await connection.close()
    assert connection.worker.done()


async def test_blocked_sse_writer_closes_generator():
    finished = asyncio.Event()

    async def records():
        try:
            yield b"data: {}\n\n"
            await asyncio.Event().wait()
        finally:
            finished.set()

    response = BoundedStreamingResponse(records())
    response.write_timeout = 0.01

    async def send(message):
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await response.stream_response(send)
    assert finished.is_set()


async def test_independent_service_outlives_publisher_and_stream_resumes(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    connection = await connect_local_service(data_dir=tmp_path, port=port)
    config = connection.config
    base_url = f"http://127.0.0.1:{port}"
    try:
        token = await connection.issue("survivor")
        assert token
        publisher = AgentStatusPublisher(connection, "survivor", token)
        publisher.emit("register", heartbeatIntervalSec=1)
        await asyncio.wait_for(connection.queue.join(), 3)
        snapshot = await connection.snapshot()
        assert snapshot["agents"][0]["lifecycle"]["value"] == "online"
        async with httpx.AsyncClient(base_url=base_url, timeout=4, trust_env=False) as client:
            async with client.stream("GET", "/v1/stream", headers={**auth(config["observerToken"]), "Last-Event-ID": str(snapshot["cursor"])}) as stream:
                assert stream.status_code == 200
                publisher.emit("heartbeat", progressSeq=1)
                lines = stream.aiter_lines()
                received = []
                while not received or received[-1] != "":
                    received.append(await asyncio.wait_for(anext(lines), 3))
                assert "event: heartbeat" in received
                payload = json.loads(next(line[6:] for line in received if line.startswith("data: ")))
                assert payload["progressSeq"] == 1
            await connection.close()  # must leave the separate monitor running
            await asyncio.sleep(2.9)
            observed = await client.get("/v1/agents", headers=auth(config["observerToken"]))
            assert observed.status_code == 200
            agent = observed.json()["agents"][0]
            assert agent["lifecycle"]["value"] == "unreachable"
            assert agent["lifecycle"]["source"] == "server"
    finally:
        await connection.close()
        async with httpx.AsyncClient(base_url=base_url, timeout=10, trust_env=False) as client:
            async with client.stream("GET", "/v1/stream", headers=auth(config["observerToken"])) as stream:
                assert stream.status_code == 200
                await client.post("/v1/local/shutdown", headers=auth(config["operatorToken"]))
                async with asyncio.timeout(8):
                    body = b"".join([chunk async for chunk in stream.aiter_bytes()])
                assert b"event: disconnect" in body
