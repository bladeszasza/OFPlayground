"""Bridge MessageBus collector events to WebSocket clients."""
from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Resolve result dir once at import time so absolute media paths can be
# converted to browser-accessible /media/<relative> URLs.
_RESULT_DIR = (Path(__file__).resolve().parents[2] / "result").resolve()


def _to_media_url(path_str: str) -> str:
    """Convert an absolute filesystem path to a /media/<relative> URL.

    If the path is already relative or an http URL, return it unchanged.
    """
    if not path_str or not os.path.isabs(path_str):
        return path_str
    try:
        rel = Path(path_str).resolve().relative_to(_RESULT_DIR)
        return f"/media/{rel.as_posix()}"
    except ValueError:
        # Path outside RESULT_DIR — return as-is; browser will fail gracefully
        return path_str

if TYPE_CHECKING:
    from fastapi import WebSocket
    from openfloor import Envelope

    from ofp_playground.trace.collector import EventCollector

logger = logging.getLogger(__name__)


def _extract_agent_name(uri: str, agent_names: dict[str, str]) -> str:
    if uri in agent_names:
        return agent_names[uri]
    return (
        uri.split(":")[-1]
        .replace("llm-", "")
        .replace("human-", "")
        .replace("image-", "")
        .replace("vision-", "")
    )


def _token_value(token: Any) -> str:
    value = getattr(token, "value", "")
    return value if isinstance(value, str) else str(value)


def _envelope_to_dict(envelope: Envelope) -> dict[str, Any]:
    """Best-effort serialization of an OFP envelope to a plain dict."""
    try:
        import json as _json
        raw = dict(envelope)
        # Round-trip through JSON to ensure all nested objects are plain types
        return _json.loads(_json.dumps(raw, default=str))
    except Exception:
        logger.exception("Operation failed at an application error boundary")
        return {}


def _serialize_events(
    envelope: Envelope, agent_names: dict[str, str]
) -> Iterator[dict[str, Any]]:
    """Serialize every supported event, including compound OFP envelopes."""
    sender_uri = envelope.sender.speakerUri if envelope.sender else "unknown"
    sender_name = _extract_agent_name(sender_uri, agent_names)
    envelope_json = _envelope_to_dict(envelope)

    for event in envelope.events or []:
        event_type = getattr(event, "eventType", type(event).__name__)

        if event_type == "utterance":
            dialog_event = getattr(event, "dialogEvent", None)
            features = getattr(dialog_event, "features", {}) or {}
            text = ""
            media: dict[str, str] | None = None

            text_feature = features.get("text") if isinstance(features, dict) else None
            text_tokens = getattr(text_feature, "tokens", None) or []
            if text_tokens:
                text = " ".join(
                    _token_value(token) for token in text_tokens if _token_value(token)
                ).strip()

            for key in ("image", "video", "audio", "3d"):
                feature = features.get(key) if isinstance(features, dict) else None
                tokens = getattr(feature, "tokens", None) or []
                if tokens:
                    url = _to_media_url(_token_value(tokens[0]).strip())
                    if url:
                        media = {"type": key, "url": url}
                        break

            yield {
                "type": "utterance",
                "sender": sender_name,
                "sender_uri": sender_uri,
                "text": text,
                "media": media,
                "envelope_json": envelope_json,
            }

        if event_type == "grantFloor":
            target = getattr(event, "to", None)
            target_uri = getattr(target, "speakerUri", "") if target else ""
            target_name = _extract_agent_name(target_uri, agent_names) if target_uri else ""
            yield {"type": "floor_grant", "to": target_name, "to_uri": target_uri, "envelope_json": envelope_json}

        if event_type == "revokeFloor":
            target = getattr(event, "to", None)
            target_uri = getattr(target, "speakerUri", "") if target else ""
            target_name = _extract_agent_name(target_uri, agent_names) if target_uri else ""
            yield {"type": "floor_revoke", "from": target_name, "from_uri": target_uri, "envelope_json": envelope_json}

        if event_type == "requestFloor":
            yield {"type": "floor_request", "from": sender_name, "from_uri": sender_uri, "envelope_json": envelope_json}

        if event_type in ("publishManifest", "publishManifests"):
            parameters = getattr(event, "parameters", None) or {}
            if isinstance(parameters, dict):
                raw_manifests = parameters.get("servicingManifests", [])
            else:
                raw_manifests = getattr(parameters, "servicingManifests", None) or []
            manifests: list[dict] = []
            for m in raw_manifests:
                if isinstance(m, dict):
                    manifests.append(m)
                elif hasattr(m, "to_dict"):
                    manifests.append(m.to_dict())
                else:
                    try:
                        manifests.append(dict(m))
                    except Exception:
                        logger.exception("Operation failed at an application error boundary")
            yield {
                "type": "manifest_published",
                "agent": sender_name,
                "agent_uri": sender_uri,
                "manifests": manifests,
                "envelope_json": envelope_json,
            }

        if event_type == "yieldFloor":
            yield {"type": "floor_yield", "from": sender_name, "from_uri": sender_uri, "envelope_json": envelope_json}

        # Suppress noisy protocol events — return None to skip them
        if event_type not in {"utterance"}:
            continue

    return


def _serialize_envelope(
    envelope: Envelope, agent_names: dict[str, str]
) -> dict[str, Any] | None:
    """Compatibility helper for callers interested in the first visible event."""
    return next(_serialize_events(envelope, agent_names), None)


class SessionBridge:
    """Collector-compatible bridge from MessageBus to WebSocket clients."""

    def __init__(self, retention: int = 5000) -> None:
        self._agent_names: dict[str, str] = {}
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._clients: dict[WebSocket, asyncio.Queue[dict[str, Any]]] = {}
        self._writers: dict[WebSocket, asyncio.Task[None]] = {}
        self._cutoffs: dict[WebSocket, int] = {}
        self._retention = retention
        self._event_log: deque[dict[str, Any]] = deque(maxlen=retention)
        self._seq = 0
        self._inner_collector: EventCollector | None = None
        self._conversation_id = ""

    @property
    def conversation_id(self) -> str:
        if self._inner_collector is not None:
            return self._inner_collector.conversation_id
        return self._conversation_id

    @property
    def event_log(self) -> list[dict[str, Any]]:
        return list(self._event_log)

    def reset(self) -> None:
        """Clear transient state between sessions."""
        self._agent_names = {}
        for client in list(self._clients):
            self.disconnect(client)
        self._event_log.clear()
        self._seq = 0
        self._queue = asyncio.Queue()
        self._inner_collector = None
        self._conversation_id = ""

    def set_inner_collector(self, collector: EventCollector) -> None:
        self._inner_collector = collector
        self._conversation_id = collector.conversation_id

    def register_agent(self, uri: str, name: str) -> None:
        self._agent_names[uri] = name
        if self._inner_collector is not None:
            self._inner_collector.register_agent(uri, name)

    def push_raw(self, event: dict[str, Any]) -> None:
        self._seq += 1
        event = {**event, "cursor": self._seq}
        self._event_log.append(event)
        self._queue.put_nowait(event)

    def record(
        self,
        envelope: Envelope,
        recipients: set[str],
        is_private: bool,
        breakout_id: str | None = None,
        *,
        scope_id: str | None = None,
        scope_kind: str | None = None,
        parent_conversation_id: str | None = None,
        delivery_outcomes: dict[str, str] | None = None,
    ) -> None:
        if self._inner_collector is not None:
            self._inner_collector.record(
                envelope=envelope,
                recipients=recipients,
                is_private=is_private,
                breakout_id=breakout_id,
                scope_id=scope_id,
                scope_kind=scope_kind,
                parent_conversation_id=parent_conversation_id,
                delivery_outcomes=delivery_outcomes,
            )

        for event in _serialize_events(envelope, self._agent_names):
            if delivery_outcomes is not None:
                event["delivery_outcomes"] = dict(delivery_outcomes)
            event["conversation_id"] = (
                envelope.conversation.id if envelope.conversation else self.conversation_id
            )
            event["scope_id"] = scope_id or breakout_id or event["conversation_id"]
            event["scope_kind"] = scope_kind or "main"
            event["parent_conversation_id"] = parent_conversation_id
            self.push_raw(event)

    async def connect(self, websocket: WebSocket, after: int = 0) -> None:
        await websocket.accept()
        # No await between the snapshot and subscription: queued broadcasts at or
        # below this cursor are replay, while later ones are live.
        cutoff = self._seq
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._retention + 256)
        oldest = self._event_log[0]["cursor"] if self._event_log else cutoff + 1
        if after > cutoff or (after and after < oldest - 1):
            queue.put_nowait({"type": "replay_gap", "oldest_cursor": oldest})
            after = 0
        for event in self._event_log:
            if event["cursor"] > after:
                queue.put_nowait({**event, "replay": True})
        self._clients[websocket] = queue

        async def write() -> None:
            try:
                while True:
                    event = await queue.get()
                    await asyncio.wait_for(websocket.send_json(event), timeout=5.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Dropping stale canvas websocket", exc_info=True)
                self._clients.pop(websocket, None)
                await websocket.close(code=1013, reason="Reconnect to resume events")

        writer = asyncio.create_task(write(), name="canvas:writer")
        self._writers[websocket] = writer

        def finished(task: asyncio.Task[None]) -> None:
            if self._writers.get(websocket) is task:
                self._writers.pop(websocket, None)
            self._clients.pop(websocket, None)
            self._cutoffs.pop(websocket, None)
            if not task.cancelled():
                task.exception()  # consume transport-close failures too

        writer.add_done_callback(finished)
        # Each client has its own live cutoff, independent of its replay cursor.
        self._cutoffs[websocket] = cutoff

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.pop(websocket, None)
        self._cutoffs.pop(websocket, None)
        task = self._writers.get(websocket)
        if task is not None:
            task.cancel()

    async def close(self) -> None:
        for websocket in list(self._writers):
            self.disconnect(websocket)
        await asyncio.gather(*self._writers.values(), return_exceptions=True)
        self._writers.clear()

    async def broadcast_loop(self) -> None:
        """Enqueue independently per client so a slow reader cannot block the fleet."""
        try:
            while True:
                event = await self._queue.get()
                for websocket, queue in list(self._clients.items()):
                    if event["cursor"] <= self._cutoffs[websocket]:
                        continue
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        self.disconnect(websocket)
                        # The timed writer/endpoint will close the transport.
                        asyncio.create_task(websocket.close(code=1013, reason="Slow consumer"))
        finally:
            await self.close()
