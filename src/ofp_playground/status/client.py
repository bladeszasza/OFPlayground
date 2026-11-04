"""Bounded, state-only runtime publishing; a failed monitor cannot fail the task."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ofp_playground.status.models import ASPEvent
from ofp_playground.status.server import DEFAULT_DATA_DIR, load_local_credentials

logger = logging.getLogger(__name__)


def _spawn_background(command: list[str], options: dict[str, Any]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(command, **options)


class StatusConnection:
    """One bounded FIFO preserves registration/order across all local publishers."""

    def __init__(self, client: httpx.AsyncClient, config: dict[str, str], *, capacity: int = 1024):
        self.client = client
        self.config = config
        self.queue: asyncio.Queue[tuple[dict, str]] = asyncio.Queue(maxsize=capacity)
        self.problem: str | None = None
        self.agent_problems: dict[str, str] = {}
        self.disabled = False
        self.worker: asyncio.Task | None = None
        self.closed = False
        # Keep operator assertions ordered across reconnecting runner processes.
        # Nanoseconds fit a signed 64-bit integer; sequence increments per event.
        self._authority_seq = time.time_ns()

    def start(self) -> None:
        self.worker = asyncio.create_task(self._send(), name="asp:publisher")

    def submit(self, payload: dict, token: str) -> None:
        if self.disabled or self.closed:
            return
        try:
            # Validate before queueing; application strings cannot leak into ASP.
            event = ASPEvent.model_validate(payload)
            self.queue.put_nowait((event.payload(), token))
        except asyncio.QueueFull:
            # Continuing heartbeats after losing task transitions would fabricate
            # reassuring state. Stop all publication and let liveness expire.
            self.disabled = True
            self.problem = "observation-buffer-overflow"
            if self.worker:
                self.worker.cancel()
        except ValueError:
            self.disabled = True
            self.problem = "invalid-runtime-status"
            if self.worker:
                self.worker.cancel()

    async def _send(self) -> None:
        while True:
            payload, token = await self.queue.get()
            try:
                while True:
                    try:
                        response = await self.client.post("/v1/events", json=payload,
                                                          headers={"Authorization": "Bearer " + token})
                        if response.status_code == 202:
                            self.problem = None
                            self.agent_problems.pop(payload["agentId"], None)
                            break
                        if response.status_code == 429 or response.status_code >= 500:
                            self.problem = "status-service-unavailable"
                            await asyncio.sleep(1)
                            continue
                        self.problem = "status-assertion-rejected"
                        self.agent_problems[payload["agentId"]] = "status-assertion-rejected"
                        logger.warning("ASP assertion rejected (HTTP %s)", response.status_code)
                        break
                    except (httpx.HTTPError, OSError):
                        self.problem = "status-service-unavailable"
                        await asyncio.sleep(1)
            finally:
                self.queue.task_done()

    async def issue(self, agent_id: str) -> str | None:
        try:
            result = await self.client.post("/v1/credentials", json={"agentId": agent_id},
                                            headers={"Authorization": "Bearer " + self.config["operatorToken"]})
            result.raise_for_status()
            return result.json()["token"]
        except (httpx.HTTPError, OSError, ValueError, KeyError):
            self.problem = "status-registration-unavailable"
            self.agent_problems[agent_id] = "status-registration-unavailable"
            return None

    async def snapshot(self) -> dict:
        response = await self.client.get("/v1/agents", headers={"Authorization": "Bearer " + self.config["observerToken"]})
        response.raise_for_status()
        return response.json()

    async def assert_governance(self, agent_id: str, value: str, *, source: str = "operator",
                                operation_id: str, enforcement: str = "requested") -> dict:
        """Wait for an authenticated acknowledgement, without claiming enforcement.

        A network failure has an unknown outcome. Explicit 4xx refusals are
        returned separately so callers cannot claim the assertion was accepted.
        """
        if source not in {"operator", "guardrail"} or value not in {"normal", "quarantined", "under-review"}:
            raise ValueError("Unsupported governance operation")
        token = self.config.get(source + "Token")
        if token is None or self.disabled or self.closed:
            return {"acknowledged": None, "error": "status-control-unavailable"}
        try:
            await asyncio.wait_for(self.queue.join(), timeout=2)
            self._authority_seq += 1
            event = ASPEvent.model_validate({
                "aspVersion": "0.1", "type": "state_change", "agentId": agent_id,
                "eventId": str(uuid.uuid4()), "seq": self._authority_seq,
                "ts": datetime.now(UTC).isoformat(), "source": source,
                "dimension": "governance", "from": "normal", "to": value,
                "reason": "operator-action" if source == "operator" else "policy-restriction",
                "ext": {"local.ofp-playground": {"operationId": operation_id, "enforcement": enforcement}},
            })
            response = await self.client.post("/v1/events", json=event.payload(),
                                              headers={"Authorization": "Bearer " + token})
            if response.status_code == 202:
                return {"acknowledged": True, "cursor": response.json().get("cursor")}
            if 400 <= response.status_code < 500 and response.status_code != 429:
                return {"acknowledged": False, "error": "status-control-rejected"}
        except (httpx.HTTPError, OSError, ValueError, TimeoutError):
            pass
        return {"acknowledged": None, "error": "status-control-unavailable"}

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.worker:
            if not self.disabled:
                try:
                    await asyncio.wait_for(self.queue.join(), timeout=2)
                except TimeoutError:
                    self.problem = "status-service-unavailable"
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.client.aclose()


class AgentStatusPublisher:
    def __init__(self, connection: StatusConnection, agent_id: str, token: str):
        self.connection = connection
        self.agent_id = agent_id
        self.token = token
        self.seq = -1
        self.progress = 0
        self.states: dict[tuple[str, str], str] = {}
        self.activity = "idle"
        self.current_task: str | None = None

    def emit(self, event_type: str, **fields: Any) -> None:
        self.seq += 1
        self.connection.submit({
            "aspVersion": "0.1", "type": event_type, "agentId": self.agent_id,
            "eventId": str(uuid.uuid4()), "seq": self.seq,
            "ts": datetime.now(UTC).isoformat(), **fields,
        }, self.token)

    def transition(self, dimension: str, value: str, *, reason: str | None = None,
                   task_id: str | None = None, conversation_id: str | None = None,
                   ext: dict | None = None) -> None:
        scope = task_id or conversation_id or ""
        previous = self.states.get((dimension, scope), {
            "lifecycle": "online", "health": "healthy", "task": "submitted",
            "floor": "not-joined", "governance": "normal",
        }[dimension])
        if previous == value:
            return
        fields: dict[str, Any] = {"from": previous, "to": value}
        if dimension == "floor":
            fields["conversationId"] = conversation_id
        else:
            fields["dimension"] = dimension
        if task_id:
            fields["taskId"] = task_id
        if reason:
            fields["reason"] = reason
        if ext:
            fields["ext"] = ext
        self.emit("floor_change" if dimension == "floor" else "state_change", **fields)
        self.states[(dimension, scope)] = value

    async def heartbeat(self, interval: float = 2, *, enabled: Callable[[], bool] | None = None) -> None:
        while True:
            await asyncio.sleep(interval)
            if enabled is None or enabled():
                self.emit("heartbeat", progressSeq=self.progress, activity=self.activity)

    def begin_work(self, activity: str = "model-call") -> str:
        task_id = str(uuid.uuid4())
        self.current_task = task_id
        self.activity = activity
        self.transition("lifecycle", "busy", reason="work-started")
        self.transition("task", "working", task_id=task_id, reason="work-started")
        return task_id

    def finish_work(self, task_id: str, state: str, reason: str) -> None:
        self.transition("task", state, task_id=task_id, reason=reason)
        if state == "completed":
            self.progress += 1
            self.transition("health", "healthy", reason="work-completed")
        self.transition("lifecycle", "idle")
        self.current_task = None
        self.activity = "idle"


async def connect_local_service(*, data_dir: Path = DEFAULT_DATA_DIR, port: int = 8787,
                                auto_start: bool = True) -> StatusConnection:
    """Start/reuse a process independent of the runner. Tokens stay off command lines."""
    config = await asyncio.to_thread(load_local_credentials, data_dir)
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=1, trust_env=False)
    authorization = {"Authorization": "Bearer " + config["observerToken"]}
    process = None
    try:
        try:
            response = await client.get("/v1/agents", headers=authorization)
            response.raise_for_status()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if not auto_start:
                raise
            kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                      "stderr": subprocess.DEVNULL, "close_fds": True}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            process = await asyncio.to_thread(
                _spawn_background,
                [sys.executable, "-m", "ofp_playground.status.server", "--port", str(port),
                 "--data-dir", str(data_dir)], kwargs)
            for _ in range(40):
                await asyncio.sleep(0.1)
                try:
                    response = await client.get("/v1/agents", headers=authorization)
                    response.raise_for_status()
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if process.poll() is not None:
                        raise RuntimeError("Independent ASP service could not start") from None
            else:
                raise RuntimeError("Independent ASP service did not become ready")
        connection = StatusConnection(client, config)
        connection.start()
        return connection
    except BaseException:
        await client.aclose()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
        raise
