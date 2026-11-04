"""Authenticated HTTP/SSE binding for the local ASP profile."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from ofp_playground import __version__
from ofp_playground.status.models import ASPEvent, Credential, StatusError
from ofp_playground.status.store import StatusStore

MAX_EVENT = 16 * 1024
MAX_BATCH = 64 * MAX_EVENT


class BoundedStreamingResponse(StreamingResponse):
    write_timeout = 5.0

    async def stream_response(self, send):
        async def bounded_send(message):
            await asyncio.wait_for(send(message), timeout=self.write_timeout)
        try:
            await super().stream_response(bounded_send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close:
                await close()


class CredentialRegistry:
    """Only token hashes are durable; an issued token is returned once."""

    def __init__(self, store: StatusStore, tokens: dict[str, Credential]):
        self.store = store
        store.db.execute("""CREATE TABLE IF NOT EXISTS credentials (
            hash TEXT PRIMARY KEY, principal TEXT NOT NULL, payload TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1)""")
        with store.db:
            for token, credential in tokens.items():
                if len(token) < 32:
                    raise ValueError("status credentials require at least 32 characters")
                self.add(token, credential, replace=False)

    def add(self, token: str, credential: Credential, *, replace: bool = True) -> None:
        payload = asdict(credential)
        payload["agent_ids"] = sorted(credential.agent_ids)
        verb = "REPLACE" if replace else "IGNORE"
        self.store.db.execute(f"INSERT OR {verb} INTO credentials VALUES(?,?,?,1)", (
            hashlib.sha256(token.encode()).hexdigest(), credential.principal_id, json.dumps(payload),
        ))

    def authenticate(self, request: Request) -> Credential:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or len(header) > 1024:
            raise StatusError(401, "valid bearer credential required")
        token_hash = hashlib.sha256(header[7:].encode()).hexdigest()
        row = self.store.db.execute("SELECT payload FROM credentials WHERE hash=? AND active=1", (token_hash,)).fetchone()
        if row is None:
            raise StatusError(401, "valid bearer credential required")
        data = json.loads(row[0])
        data["agent_ids"] = frozenset(data["agent_ids"])
        return Credential(**data)

    def revoke(self, principal: str) -> None:
        with self.store.db:
            self.store.db.execute("UPDATE credentials SET active=0 WHERE principal=?", (principal,))


async def _read_json(request: Request, maximum: int) -> Any:
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > maximum:
            raise StatusError(413, "status payload exceeds the byte limit")
    try:
        return json.loads(content)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise StatusError(400, "malformed status JSON") from exc


def _parse_event(data: Any) -> ASPEvent:
    try:
        encoded = json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > MAX_EVENT:
            raise StatusError(413, "status event exceeds 16 KiB")
        return ASPEvent.model_validate(data)
    except (ValidationError, ValueError, TypeError, RecursionError) as exc:
        # Pydantic's default errors include the input: never echo that input here.
        raise StatusError(400, "event does not match the state-only ASP profile") from exc


def encode_sse(record: dict) -> bytes:
    kind = record.get("kind")
    if kind:
        event_type = kind
        data = record
    else:
        event_type = record["event"]["type"]
        data = record["event"]
    return f"id: {record['cursor']}\nevent: {event_type}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def build_status_app(store: StatusStore, tokens: dict[str, Credential], *, rate_limit: int = 300,
                     monitor: bool = True) -> FastAPI:
    credentials = CredentialRegistry(store, tokens)
    rates: dict[str, deque[float]] = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def watch():
            while True:
                await asyncio.sleep(0.25)
                store.tick()
        task = asyncio.create_task(watch(), name="asp:liveness") if monitor else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            for queue in tuple(store.subscribers):
                store._disconnect(queue)

    app = FastAPI(title="OFPlayground Agent Status", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.credentials = credentials
    app.state.store = store

    @app.exception_handler(StatusError)
    async def refused(_request: Request, error: StatusError):
        return JSONResponse({"detail": error.reason}, status_code=error.status,
                            headers={"Retry-After": "1"} if error.status == 429 else {})

    def observer(request: Request, agent_id: str | None = None) -> Credential:
        credential = credentials.authenticate(request)
        if not credential.observe or (agent_id is not None and not credential.permits(agent_id)):
            raise StatusError(403, "observer scope does not authorize this view")
        return credential

    def snapshot_for(credential: Credential) -> dict:
        snapshot = store.snapshot()
        snapshot["agents"] = [a for a in snapshot["agents"] if credential.permits(a["agentId"])]
        return snapshot

    def charge(credential: Credential, count: int) -> None:
        if not credential.ingest:
            raise StatusError(403, "credential is read-only")
        now = store.clock()
        window = rates[credential.principal_id]
        while window and window[0] <= now - 60:
            window.popleft()
        if len(window) + count > rate_limit:
            raise StatusError(429, "status ingest rate exceeded")
        window.extend([now] * count)

    @app.post("/v1/events", status_code=202)
    async def ingest(request: Request):
        credential = credentials.authenticate(request)
        charge(credential, 1)
        event = _parse_event(await _read_json(request, MAX_EVENT))
        return store.ingest(event, credential)

    @app.post("/v1/events:batch", status_code=202)
    async def batch(request: Request):
        credential = credentials.authenticate(request)
        data = await _read_json(request, MAX_BATCH)
        if not isinstance(data, list) or not 1 <= len(data) <= 64:
            raise StatusError(400, "batch requires 1 to 64 events")
        charge(credential, len(data))
        # Each event has its own outcome; successfully committed events survive
        # a later event's rejection, and retrying their IDs is safe.
        results = []
        for item in data:
            try:
                results.append({"status": 202, **store.ingest(_parse_event(item), credential)})
            except StatusError as error:
                results.append({"status": error.status, "detail": error.reason})
        return {"results": results}

    @app.get("/v1/agents")
    async def agents(request: Request):
        return snapshot_for(observer(request))

    @app.get("/v1/agents/{agent_id:path}")
    async def agent(request: Request, agent_id: str):
        observer(request, agent_id)
        snapshot = store.snapshot(agent_id)
        if not snapshot["agents"]:
            raise StatusError(404, "agent not found")
        return snapshot

    @app.get("/v1/history")
    async def history(request: Request, after: int = 0, agentId: str | None = None, limit: int = 200,
                      latest: bool = False, transitions: bool = False):
        credential = observer(request, agentId)
        if after < 0 or not 1 <= limit <= 500:
            raise StatusError(400, "invalid history cursor or limit")
        return {"cursor": store.cursor, "events": [r for r in store.history(after, agent_id=agentId, limit=limit, latest=latest, transitions_only=transitions)
                                                     if credential.permits(r["event"]["agentId"])]}

    @app.get("/v1/stream")
    async def stream(request: Request):
        credential = observer(request)
        header = request.headers.get("last-event-id")
        if header is not None and (not header.isdecimal() or len(header) > 20):
            raise StatusError(400, "invalid Last-Event-ID")
        queue, initial = store.subscribe(int(header) if header is not None else None)

        def visible(record: dict) -> dict | None:
            if record.get("kind") == "snapshot":
                return {**record, "agents": [a for a in record["agents"] if credential.permits(a["agentId"])]}
            if record.get("kind") or credential.permits(record["event"]["agentId"]):
                return record
            return None

        async def events():
            try:
                for record in initial:
                    credentials.authenticate(request)
                    filtered = visible(record)
                    if filtered is not None:
                        yield encode_sse(filtered)
                while True:
                    # Revoked observers lose existing streams as well as new ones.
                    credentials.authenticate(request)
                    try:
                        record = await asyncio.wait_for(queue.get(), timeout=10)
                    except TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    credentials.authenticate(request)
                    if record.get("kind") == "disconnect":
                        yield encode_sse(record)
                        break
                    filtered = visible(record)
                    if filtered is not None:
                        yield encode_sse(filtered)
            except StatusError:
                yield b"event: unauthorized\ndata: {}\n\n"
            finally:
                store.unsubscribe(queue)

        return BoundedStreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/v1/credentials", status_code=201)
    async def issue(request: Request):
        credential = credentials.authenticate(request)
        if credential.role != "operator" or not credential.ingest:
            raise StatusError(403, "operator credential required")
        charge(credential, 1)
        data = await _read_json(request, 2048)
        if not isinstance(data, dict) or set(data) != {"agentId"}:
            raise StatusError(400, "credential request requires only agentId")
        agent_id = data["agentId"]
        if not isinstance(agent_id, str) or not 1 <= len(agent_id) <= 256 or not credential.permits(agent_id):
            raise StatusError(403, "agent is outside operator scope")
        token = secrets.token_urlsafe(32)
        principal = "agent:" + agent_id
        issued = Credential(principal, "self", frozenset({agent_id}))
        if store.db.execute("SELECT COUNT(*) FROM credentials").fetchone()[0] >= 10000:
            raise StatusError(429, "credential capacity exceeded")
        with store.db:
            credentials.add(token, issued)
        return {"token": token, "principalId": principal, "agentId": agent_id}

    @app.delete("/v1/credentials/{principal:path}")
    async def revoke(request: Request, principal: str):
        credential = credentials.authenticate(request)
        if credential.role != "operator" or not credential.ingest:
            raise StatusError(403, "operator credential required")
        row = store.db.execute("SELECT payload FROM credentials WHERE principal=?", (principal,)).fetchone()
        if row is None:
            raise StatusError(404, "credential principal not found")
        target = json.loads(row[0])
        if any(not credential.permits(a) for a in target["agent_ids"]):
            raise StatusError(403, "principal is outside operator scope")
        credentials.revoke(principal)
        return {"revoked": True, "principalId": principal}

    @app.post("/v1/local/shutdown")
    async def shutdown(request: Request):
        credential = credentials.authenticate(request)
        if credential.role != "operator" or not credential.ingest or "*" not in credential.agent_ids:
            raise StatusError(403, "deployment operator credential required")
        callback = getattr(app.state, "shutdown", None)
        if callback is None:
            raise StatusError(409, "this service is owned by its embedding application")
        callback()
        return {"stopping": True}

    return app
