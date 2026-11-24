"""Shared (remote) ASP status servers, such as the ASP reference server.

The reference server (github.com/AgentStatusProtocol/spec, ``implementations/
python-status-server``) uses one bearer token for every caller, takes the
reporter role from the event's ``source`` field, requires ``register`` before
any other event and orders events per agent and source by ``seq``. Its example
clients read ``ASP_SERVER`` and ``ASP_TOKEN``; this module uses the same names.

Two uses:

- :class:`RemoteStatusServer` lists agents and changes an agent's governance as
  an operator (``ofp-playground governance``), with the same semantics as the
  reference ``governance.py``.
- :class:`RemoteMirror` copies a session's validated state-only events to a
  shared server (opt in with ``OFP_ASP_MIRROR=1``). The local monitor stays the
  source of truth; a slow or failing shared server never affects the session.

Tokens are read from the environment only and are sent in the Authorization
header, never in a URL.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Self
from urllib.parse import quote, urlparse

import httpx

from ofp_playground.status.models import STATES, ASPEvent

logger = logging.getLogger(__name__)

SERVER_ENV = "ASP_SERVER"
TOKEN_ENV = "ASP_TOKEN"
MIRROR_ENV = "OFP_ASP_MIRROR"
MIRROR_PREFIX_ENV = "OFP_ASP_MIRROR_PREFIX"
DEFAULT_MIRROR_PREFIX = "urn:agent:ofplayground:"
GOVERNANCE_STATES = tuple(sorted(STATES["governance"]))


class RemoteStatusError(Exception):
    """A content-free, user-facing failure talking to a shared ASP server."""


def normalize_server(server: str) -> str:
    """Validate a server base URL. Nonlocal servers require HTTPS."""
    server = server.strip().rstrip("/")
    parsed = urlparse(server)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
        raise RemoteStatusError("The ASP server must be an http(s) base URL without a query string.")
    local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme != "https" and not local:
        raise RemoteStatusError("A nonlocal ASP server requires https.")
    return server


def resolve_server(server: str | None) -> str:
    value = server or os.environ.get(SERVER_ENV)
    if not value:
        raise RemoteStatusError(f"Give --server or set {SERVER_ENV}.")
    return normalize_server(value)


def resolve_token() -> str | None:
    return os.environ.get(TOKEN_ENV) or None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RemoteStatusServer:
    """Synchronous operator client for a shared ASP status server."""

    def __init__(self, server: str, token: str | None, *, timeout: float = 90.0,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        # The free hosting tier sleeps when idle: the first request can take ~1 minute.
        self.server = normalize_server(server)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(base_url=self.server, headers=headers, timeout=timeout,
                                    transport=transport, trust_env=False)
        self._sleep = sleep
        self._last_seq: dict[str, int] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        try:
            response = self._client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise RemoteStatusError("The ASP server is not reachable.") from exc
        try:
            data = response.json() if response.content else None
        except ValueError:
            data = None
        return response.status_code, data

    @staticmethod
    def _fail(code: int) -> RemoteStatusError:
        if code == 401:
            return RemoteStatusError(f"The ASP server did not accept the token. Check {TOKEN_ENV}.")
        if code == 404:
            return RemoteStatusError("No agent with that id on the ASP server. List the agents first.")
        return RemoteStatusError(f"The ASP server answered HTTP {code}.")

    def agents(self) -> list[dict]:
        code, data = self._request("GET", "/v1/agents")
        if code != 200:
            raise self._fail(code)
        if isinstance(data, dict):  # a local-profile server wraps the list
            data = data.get("agents", [])
        return [agent for agent in data or [] if isinstance(agent, dict)]

    def agent(self, agent_id: str) -> dict:
        code, data = self._request("GET", "/v1/agents/" + quote(agent_id, safe=""))
        if code != 200 or not isinstance(data, dict):
            raise self._fail(code)
        return data

    def set_governance(self, agent_id: str, state: str) -> dict:
        """Change governance as an operator; returns the agent's snapshot after the change.

        Reads the current value first, as the reference ``governance.py`` does.
        The server ignores an operator event whose ``seq`` (the current second)
        is not higher than the previous one, so one retry follows a short wait.
        """
        if state not in STATES["governance"]:
            raise RemoteStatusError("Unknown governance state: use " + ", ".join(GOVERNANCE_STATES) + ".")
        for _attempt in range(2):
            current = self.agent(agent_id)
            previous = str(current.get("governance", "normal"))
            if previous == state:
                return current
            # Seconds, like the reference tools; strictly increasing within this client.
            seq = max(int(time.time()), self._last_seq.get(agent_id, 0) + 1)
            self._last_seq[agent_id] = seq
            event = ASPEvent.model_validate({
                "aspVersion": "0.1", "type": "state_change", "agentId": agent_id,
                "eventId": str(uuid.uuid4()), "seq": seq, "ts": _now(),
                "source": "operator", "dimension": "governance",
                "from": previous if ASPEvent.known_or_custom("governance", previous) else "normal",
                "to": state, "reason": "operator-action",
            })
            code, _data = self._request("POST", "/v1/events", event.payload())
            if code == 409:
                raise RemoteStatusError("The ASP server refused the change (a higher authority set this value).")
            if code != 202:
                raise self._fail(code)
            self._sleep(1.1)
        after = self.agent(agent_id)
        if after.get("governance") != state:
            raise RemoteStatusError("The change was not applied. Try again in a few seconds.")
        return after


class RemoteMirror:
    """Copy validated session events to a shared ASP server, best effort.

    Agent ids get a configurable prefix so mirrored agents do not collide with
    other participants on a shared server. Operator/guardrail assertions get a
    seconds-based ``seq``, matching the reference operator tools, so those
    tools can still change a mirrored agent's governance afterwards.
    """

    def __init__(self, server: str, token: str | None, *, prefix: str = DEFAULT_MIRROR_PREFIX,
                 capacity: int = 2048, transport: httpx.AsyncBaseTransport | None = None):
        self.server = normalize_server(server)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.AsyncClient(base_url=self.server, headers=headers, timeout=30,
                                        transport=transport, trust_env=False)
        self.prefix = prefix
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=capacity)
        self.problem: str | None = None
        self.sent = 0
        self.dropped = 0
        self._authority_seq: dict[tuple[str, str], int] = {}
        self._worker: asyncio.Task | None = None

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._send(), name="asp:mirror")

    def _rewrite(self, payload: dict) -> dict:
        event = dict(payload)
        # The local extension (speaker URIs, session ids) stays on this machine.
        event.pop("ext", None)
        event["agentId"] = self.prefix + str(payload["agentId"])
        source = event.get("source", "self")
        if source in ("operator", "guardrail"):
            key = (event["agentId"], source)
            seq = max(int(time.time()), self._authority_seq.get(key, 0) + 1)
            self._authority_seq[key] = seq
            event["seq"] = seq
        return event

    def submit(self, payload: dict) -> None:
        """Queue one already-validated event; never blocks or raises."""
        try:
            self.queue.put_nowait(self._rewrite(payload))
        except asyncio.QueueFull:
            self.dropped += 1
            self.problem = "mirror-buffer-overflow"
        except (KeyError, TypeError, ValueError):
            self.dropped += 1

    async def _send(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                for _attempt in range(3):
                    try:
                        response = await self.client.post("/v1/events", json=event)
                    except (httpx.HTTPError, OSError):
                        self.problem = "mirror-unavailable"
                        await asyncio.sleep(2)
                        continue
                    if response.status_code == 202:
                        self.sent += 1
                        self.problem = None
                    elif response.status_code == 429 or response.status_code >= 500:
                        self.problem = "mirror-unavailable"
                        await asyncio.sleep(2)
                        continue
                    else:
                        self.dropped += 1
                        self.problem = "mirror-rejected"
                        logger.info("ASP mirror: event refused (HTTP %s)", response.status_code)
                    break
                else:
                    self.dropped += 1
            finally:
                self.queue.task_done()

    async def close(self, *, drain_timeout: float = 5.0) -> None:
        if self._worker is not None:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=drain_timeout)
            except TimeoutError:
                self.problem = "mirror-unavailable"
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        await self.client.aclose()


def mirror_from_env() -> RemoteMirror | None:
    """A started mirror when ``OFP_ASP_MIRROR`` is enabled and a server is configured."""
    if os.environ.get(MIRROR_ENV, "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    try:
        server = resolve_server(None)
    except RemoteStatusError:
        logger.warning("%s is set but %s is missing or invalid; not mirroring", MIRROR_ENV, SERVER_ENV)
        return None
    prefix = os.environ.get(MIRROR_PREFIX_ENV) or DEFAULT_MIRROR_PREFIX
    if not re.fullmatch(r"[A-Za-z0-9:._-]{1,64}", prefix):
        logger.warning("%s must be 1-64 characters of A-Z a-z 0-9 : . _ -; not mirroring", MIRROR_PREFIX_ENV)
        return None
    mirror = RemoteMirror(server, resolve_token(), prefix=prefix)
    mirror.start()
    return mirror
