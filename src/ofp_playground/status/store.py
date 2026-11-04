"""Durable ASP reduction and replay; all changes are serialized on one service loop."""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ofp_playground.status.models import STATES, TERMINAL, ASPEvent, Credential, StatusError

AUTHORITY = {"self": 0, "guardrail": 1, "operator": 2}


class StatusStore:
    """SQLite commits journal, ordering watermark and snapshot before notification.

    Use from one asyncio event loop. No network await occurs while reducing or
    subscribing, which makes the snapshot/subscription cutoff atomic on that loop.
    """

    def __init__(self, path: str | Path = ":memory:", *, retention: int = 10000,
                 clock: Callable[[], float] = time.time, subscriber_size: int = 256):
        if retention < 1 or subscriber_size < 1:
            raise ValueError("retention and subscriber size must be positive")
        self.clock = clock
        self.retention = retention
        self.subscriber_size = subscriber_size
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ordering (
                agent TEXT NOT NULL, sender TEXT NOT NULL, seq INTEGER NOT NULL,
                PRIMARY KEY (agent, sender));
            CREATE TABLE IF NOT EXISTS events (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                agent TEXT NOT NULL, record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (event_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL);
        """)
        self.agents: dict[str, dict[str, Any]] = {
            row[0]: json.loads(row[1]) for row in self.db.execute("SELECT id,snapshot FROM agents")
        }
        self.ordering: dict[tuple[str, str], int] = {
            (row[0], row[1]): row[2] for row in self.db.execute("SELECT agent,sender,seq FROM ordering")
        }
        # sqlite_sequence survives retention and an empty journal.
        row = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
        self.cursor: int = row[0] if row else 0
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def close(self) -> None:
        for queue in tuple(self.subscribers):
            self._disconnect(queue)
        self.db.close()

    def _cell(self, value: str, event: ASPEvent, credential: Credential, now: float) -> dict:
        dimension = "floor" if event.type == "floor_change" else event.dimension
        return {"value": value, "source": event.source, "reporterId": credential.principal_id,
                "at": now, "eventId": event.eventId, "stale": False,
                "reason": event.reason,
                "ext": event.ext or {},
                "known": value in STATES.get(dimension or "", frozenset())}

    def _new_agent(self, event: ASPEvent, credential: Credential, now: float) -> dict:
        agent: dict[str, Any] = {
            "agentId": event.agentId, "name": event.name or event.agentId,
            "version": event.version, "protocols": event.protocols or [],
            "capabilities": event.capabilities or [], "ext": event.ext or {},
            "heartbeatIntervalSec": event.heartbeatIntervalSec,
            "stallAfterSec": event.stallAfterSec, "lastSelfEventAt": now,
            "lastProgressAt": now, "progressSeq": 0, "selfLifecycle": "online",
            "selfHealth": "healthy", "tasks": {}, "floors": {}, "observations": {},
            "activity": "idle", "metrics": {}, "deregistered": False,
        }
        for dimension, value in (("lifecycle", "online"), ("health", "healthy"), ("governance", "normal")):
            agent[dimension] = {**self._cell(value, event, credential, now), "known": True}
        return agent

    @staticmethod
    def _authorize(event: ASPEvent, credential: Credential, internal: bool) -> None:
        if not credential.ingest:
            raise StatusError(403, "credential is read-only")
        if event.source != credential.role or not credential.permits(event.agentId):
            raise StatusError(403, "credential scope does not authorize this assertion")
        if credential.role == "server" and not internal:
            raise StatusError(403, "server assertions are internal")
        if credential.role == "self" and len(credential.agent_ids) != 1:
            raise StatusError(403, "self credentials must be bound to one agent")
        dimension = "floor" if event.type == "floor_change" else event.dimension
        values = {event.to, event.lifecycle, event.health}
        if credential.role != "server" and values & {"unreachable", "stalled"}:
            raise StatusError(400, "unreachable and stalled are server-assigned")
        if credential.role == "self" and "crashed" in values:
            raise StatusError(403, "crashed requires supervisor authority")
        if dimension == "governance" and credential.role not in AUTHORITY:
            raise StatusError(403, "governance requires self, guardrail or operator authority")
        if dimension == "lifecycle" and credential.role not in {"self", "supervisor", "server"}:
            raise StatusError(403, "lifecycle requires self or supervisor authority")
        if event.type in {"register", "deregister", "heartbeat"} and credential.role != "self":
            raise StatusError(403, "registration, heartbeat and deregistration require self authority")
        if dimension == "task" and credential.role not in {"self", "supervisor", "server"}:
            raise StatusError(403, "task assertion requires self or supervisor authority")
        if dimension == "floor" and credential.role not in {"self", "supervisor", "peer"}:
            raise StatusError(403, "floor assertion requires participant or convener authority")

    def ingest(self, event: ASPEvent, credential: Credential, *, internal: bool = False) -> dict:
        self._authorize(event, credential, internal)
        existing = self.db.execute("SELECT cursor FROM seen WHERE event_id=?", (event.eventId,)).fetchone()
        if existing:
            return {"accepted": True, "duplicate": True, "cursor": existing[0], "applied": False}
        now = self.clock()
        previous = self.agents.get(event.agentId)
        if previous is None and event.type != "register":
            raise StatusError(409, "register before sending events")
        if previous is None and len(self.agents) >= 5000:
            raise StatusError(429, "agent capacity exceeded")
        key = (event.agentId, credential.principal_id)
        newer = event.seq > self.ordering.get(key, -1)
        agent = copy.deepcopy(previous) if previous is not None else self._new_agent(event, credential, now)
        if newer:
            self._reduce(agent, event, credential, now, is_new=previous is None)
        record = {"event": event.payload(), "reporterId": credential.principal_id,
                  "receivedAt": now, "applied": newer,
                  "disposition": "applied" if newer else "out-of-order"}
        with self.db:
            result = self.db.execute("INSERT INTO events(event_id,agent,record) VALUES(?,?,?)",
                                     (event.eventId, event.agentId, "{}"))
            cursor = int(result.lastrowid or 0)
            record["cursor"] = cursor
            self.db.execute("INSERT INTO seen VALUES(?,?)", (event.eventId, cursor))
            self.db.execute("UPDATE events SET record=? WHERE cursor=?", (json.dumps(record), cursor))
            if newer:
                self.db.execute("INSERT OR REPLACE INTO agents VALUES(?,?)", (event.agentId, json.dumps(agent)))
                self.db.execute("INSERT OR REPLACE INTO ordering VALUES(?,?,?)", (*key, event.seq))
            self.db.execute("DELETE FROM events WHERE cursor<=?", (cursor - self.retention,))
        if newer:
            self.agents[event.agentId] = agent
            self.ordering[key] = event.seq
        self.cursor = cursor
        self._publish(record)
        return {"accepted": True, "duplicate": False, "cursor": cursor, "applied": newer}

    def _reduce(self, agent: dict, event: ASPEvent, credential: Credential, now: float, *, is_new: bool) -> None:
        if event.type == "register":
            if not is_new and not agent["deregistered"]:
                raise StatusError(409, "agent is already registered")
            # Re-registration keeps historical tasks, terminal states and governance.
            agent.update(heartbeatIntervalSec=event.heartbeatIntervalSec, stallAfterSec=event.stallAfterSec,
                         deregistered=False, name=event.name or agent["name"], lastProgressAt=now,
                         selfLifecycle="online")
        elif agent["deregistered"] and not (
            event.type == "state_change" and event.dimension == "governance"
            and event.source in {"operator", "guardrail"}
        ):
            raise StatusError(409, "deregistered agent must register again")
        if event.source == "self":
            agent["lastSelfEventAt"] = now
            if agent["lifecycle"]["source"] != "self" or event.type == "register":
                lifecycle = event.lifecycle or (event.to if event.dimension == "lifecycle" else None) or "online"
                agent["lifecycle"] = {**self._cell(lifecycle, event, credential, now), "known": lifecycle in STATES["lifecycle"]}
            for group in ("tasks", "floors"):
                for cell in agent[group].values():
                    cell["stale"] = False
        if event.type == "deregister":
            agent["deregistered"] = True
            agent["selfLifecycle"] = "offline"
            agent["lifecycle"] = {**self._cell("offline", event, credential, now), "known": True}
            for group in ("tasks", "floors"):
                for cell in agent[group].values():
                    cell["stale"] = True
        elif event.type == "heartbeat":
            if agent["stallAfterSec"] and event.progressSeq is None:
                raise StatusError(400, "progressSeq required when stall detection is declared")
            if event.lifecycle is not None:
                agent["selfLifecycle"] = event.lifecycle
                agent["lifecycle"] = {**self._cell(event.lifecycle, event, credential, now), "known": event.lifecycle in STATES["lifecycle"]}
            if event.health is not None:
                agent["selfHealth"] = event.health
                if agent["health"]["value"] != "stalled":
                    agent["health"] = {**self._cell(event.health, event, credential, now), "known": event.health in STATES["health"]}
            if event.progressSeq is not None and event.progressSeq > agent["progressSeq"]:
                agent["progressSeq"] = event.progressSeq
                agent["lastProgressAt"] = now
                if agent["health"]["value"] == "stalled":
                    agent["health"] = {**self._cell(agent["selfHealth"], event, credential, now), "known": agent["selfHealth"] in STATES["health"]}
            agent["activity"] = event.activity or agent["activity"]
            agent["metrics"] = event.metrics or agent["metrics"]
        elif event.type == "floor_change":
            floor_id = event.conversationId
            previous = agent["floors"].get(floor_id)
            if previous and previous["value"] == "left" and event.to != "left":
                raise StatusError(409, "a left conversation requires a new agent instance to rejoin")
            self._capacity(agent["floors"], floor_id)
            agent["floors"][floor_id] = {**self._cell(event.to or "", event, credential, now), "role": event.role}
        elif event.type == "state_change":
            dimension = event.dimension
            value = event.to or ""
            cell = self._cell(value, event, credential, now)
            if dimension == "task":
                task_id = event.taskId
                previous = agent["tasks"].get(task_id)
                if previous and previous["value"] in TERMINAL and previous["value"] != value:
                    raise StatusError(409, "terminal task state cannot be changed")
                self._capacity(agent["tasks"], task_id)
                deadline = (now + event.maxDurationSec) if event.maxDurationSec else (previous or {}).get("deadline")
                agent["tasks"][task_id] = {**cell, "deadline": deadline, "contextId": event.contextId}
                if value in {"working", "retrying"} and (not previous or previous["value"] not in {"working", "retrying"}) and not any(
                    task["value"] in {"working", "retrying"} for key, task in agent["tasks"].items() if key != task_id
                ):
                    agent["lastProgressAt"] = now
            elif dimension == "governance":
                if AUTHORITY[event.source] < AUTHORITY[agent["governance"]["source"]]:
                    raise StatusError(409, "governance is held by a higher authority")
                agent["governance"] = cell
            elif dimension == "health" and event.source != "self" and event.source != "server":
                self._capacity(agent["observations"], credential.principal_id)
                agent["observations"][credential.principal_id] = cell
            elif dimension in {"lifecycle", "health"}:
                if event.source == "self":
                    agent["selfLifecycle" if dimension == "lifecycle" else "selfHealth"] = value
                    # A busy heartbeat cannot hide a stalled work loop.
                    if dimension == "health" and agent["health"]["value"] == "stalled":
                        return
                agent[dimension] = cell
                if dimension == "lifecycle" and value in {"unreachable", "crashed"}:
                    for group in ("tasks", "floors"):
                        for scoped_cell in agent[group].values():
                            scoped_cell["stale"] = True

    @staticmethod
    def _capacity(values: dict, key: Any) -> None:
        if key not in values and len(values) >= 1024:
            raise StatusError(429, "scoped state capacity exceeded")

    def snapshot(self, agent_id: str | None = None) -> dict:
        agents = [self.agents[agent_id]] if agent_id in self.agents else ([] if agent_id else list(self.agents.values()))
        # Public fields contain state and reporter identifiers; no credential/token.
        return {"aspVersion": "0.1", "cursor": self.cursor, "observedAt": self.clock(),
                "agents": copy.deepcopy(agents)}

    def history(self, after: int = 0, *, agent_id: str | None = None, limit: int = 500,
                latest: bool = False, transitions_only: bool = False) -> list[dict]:
        sql = "SELECT record FROM events WHERE cursor>?"
        params: list[Any] = [after]
        if agent_id:
            sql += " AND agent=?"
            params.append(agent_id)
        if transitions_only:
            sql += " AND json_extract(record, '$.event.type') != 'heartbeat'"
        sql += " ORDER BY cursor " + ("DESC" if latest else "ASC") + " LIMIT ?"
        params.append(min(max(limit, 1), self.retention))
        records = [json.loads(row[0]) for row in self.db.execute(sql, params)]
        return list(reversed(records)) if latest else records

    def subscribe(self, after: int | None = None) -> tuple[asyncio.Queue, list[dict]]:
        if len(self.subscribers) >= 128:
            raise StatusError(429, "observer capacity exceeded")
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.subscriber_size)
        oldest = self.db.execute("SELECT MIN(cursor) FROM events").fetchone()[0] or self.cursor + 1
        initial: list[dict] = []
        if after is None or after > self.cursor or after < oldest - 1:
            if after is not None:
                initial.append({"kind": "gap", "cursor": self.cursor, "oldestCursor": oldest})
            initial.append({"kind": "snapshot", **self.snapshot()})
        else:
            initial = self.history(after, limit=self.retention)
        self.subscribers.add(queue)
        return queue, initial

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def _disconnect(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait({"kind": "disconnect", "cursor": self.cursor})

    def _publish(self, record: dict) -> None:
        for queue in tuple(self.subscribers):
            try:
                queue.put_nowait(copy.deepcopy(record))
            except asyncio.QueueFull:
                self._disconnect(queue)

    def tick(self) -> None:
        now = self.clock()
        for agent_id, agent in list(self.agents.items()):
            if agent["deregistered"]:
                continue
            if agent["lifecycle"]["value"] == "crashed" and agent["lifecycle"]["source"] == "supervisor":
                continue  # preserve a known process failure until a self event returns
            if now - agent["lastSelfEventAt"] >= 2.5 * agent["heartbeatIntervalSec"]:
                if agent["lifecycle"]["value"] != "unreachable":
                    self._infer(agent_id, "lifecycle", "unreachable", "heartbeat-timeout")
                continue
            stall = agent["stallAfterSec"]
            progressing = any(t["value"] in {"working", "retrying"} for t in agent["tasks"].values())
            if stall and progressing and now - agent["lastProgressAt"] >= stall and agent["health"]["value"] != "stalled":
                self._infer(agent_id, "health", "stalled", "no-progress")
            for task_id, task in list(agent["tasks"].items()):
                if task.get("deadline") and now >= task["deadline"] and task["value"] not in TERMINAL:
                    self._infer(agent_id, "task", "timed-out", "task-deadline", task_id=task_id)

    def _infer(self, agent_id: str, dimension: str, value: str, reason: str, *, task_id: str | None = None) -> None:
        agent = self.agents[agent_id]
        previous = agent["tasks"][task_id]["value"] if task_id else agent[dimension]["value"]
        event = ASPEvent.model_validate({
            "aspVersion": "0.1", "type": "state_change", "agentId": agent_id,
            "eventId": str(uuid.uuid4()), "seq": self.ordering.get((agent_id, "status-server"), -1) + 1,
            "ts": datetime.fromtimestamp(self.clock(), UTC).isoformat(), "source": "server",
            "dimension": dimension, "from": previous, "to": value, "reason": reason,
            **({"taskId": task_id} if task_id else {}),
        })
        self.ingest(event, Credential("status-server", "server", frozenset({agent_id})), internal=True)
