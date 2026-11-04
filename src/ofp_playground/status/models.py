"""Validated, bounded state-only events for the pinned ASP 0.1 profile."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

STATES = {
    "lifecycle": frozenset(["registering", "initializing", "online", "idle", "busy", "at-capacity", "paused", "draining", "stopping", "offline", "unreachable", "crashed"]),
    "health": frozenset(["healthy", "degraded", "rate-limited", "quota-exhausted", "dependency-down", "auth-error", "overloaded", "unhealthy", "stalled"]),
    "task": frozenset(["submitted", "queued", "working", "input-required", "auth-required", "completed", "failed", "canceled", "rejected", "unknown", "awaiting-approval", "delegated", "retrying", "timed-out"]),
    "floor": frozenset(["not-joined", "invited", "joined", "requesting-floor", "has-floor", "yielded", "floor-revoked", "left"]),
    "governance": frozenset(["normal", "sandboxed", "quarantined", "policy-violation", "under-review"]),
}
TERMINAL = frozenset(["completed", "failed", "canceled", "rejected", "timed-out"])
REASONS = frozenset(["registered", "initializing", "ready", "work-started", "work-completed", "work-failed", "call-timeout", "rate-limit", "quota-exhausted", "dependency-failure", "auth-failure", "retry-backoff", "retry-exhausted", "input-needed", "approval-needed", "delegated", "canceled", "removed", "session-stopped", "operator-action", "policy-restriction", "heartbeat-timeout", "no-progress", "progress-resumed", "contact-restored", "process-exit", "floor-change", "shutdown", "task-deadline", "showcase"])
ACTIVITIES = frozenset(["idle", "model-call", "tool-call", "retry-backoff", "awaiting-input", "awaiting-approval", "delegated", "queued", "draining", "stopped", "showcase"])
METRICS = frozenset(["latencyMs", "queueDepth", "activeTasks", "maxConcurrentTasks", "tokensIn", "tokensOut", "costUsd", "errorRate"])
EXT_KEYS = frozenset(["speakerUri", "nodeId", "parentConversationId", "sessionId", "instrumentation", "enforcement", "operationId"])
ROLES = Literal["self", "supervisor", "guardrail", "operator", "peer", "server"]


class StatusError(Exception):
    """An intentional ingest refusal with a content-free public reason."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class Credential:
    principal_id: str
    role: ROLES
    agent_ids: frozenset[str] = frozenset()
    observe: bool = False
    ingest: bool = True

    def permits(self, agent_id: str) -> bool:
        return agent_id in self.agent_ids or (self.role != "self" and "*" in self.agent_ids)


class ASPEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    aspVersion: Literal["0.1"]
    type: Literal["register", "heartbeat", "state_change", "floor_change", "deregister"]
    agentId: str = Field(min_length=1, max_length=256)
    eventId: str = Field(min_length=1, max_length=128)
    seq: int = Field(ge=0, le=2**63 - 1, strict=True)
    ts: datetime
    source: ROLES = "self"
    reason: str | None = None
    name: str | None = Field(default=None, max_length=100)
    version: str | None = Field(default=None, max_length=40)
    protocols: list[str] | None = Field(default=None, max_length=12)
    capabilities: list[str] | None = Field(default=None, max_length=24)
    heartbeatIntervalSec: int | None = Field(default=None, ge=1, le=300, strict=True)
    stallAfterSec: int | None = Field(default=None, ge=1, le=86400, strict=True)
    lifecycle: str | None = None
    health: str | None = None
    progressSeq: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)
    activity: str | None = None
    metrics: dict[str, Annotated[float, Field(strict=True)]] | None = None
    dimension: Literal["lifecycle", "health", "task", "governance"] | None = None
    from_state: str | None = Field(default=None, alias="from")
    to: str | None = None
    taskId: str | None = Field(default=None, min_length=1, max_length=256)
    contextId: str | None = Field(default=None, min_length=1, max_length=256)
    maxDurationSec: int | None = Field(default=None, ge=1, le=604800, strict=True)
    conversationId: str | None = Field(default=None, min_length=1, max_length=256)
    role: str | None = Field(default=None, max_length=40)
    ext: dict[str, dict[str, str]] | None = None

    @model_validator(mode="after")
    def validate_profile(self) -> ASPEvent:
        if self.ts.tzinfo is None or self.ts.utcoffset() is None:
            raise ValueError("ts requires a timezone")
        common = {"aspVersion", "type", "agentId", "eventId", "seq", "ts", "source", "reason", "ext"}
        fields = {
            "register": {"name", "version", "protocols", "capabilities", "heartbeatIntervalSec", "stallAfterSec"},
            "heartbeat": {"lifecycle", "health", "progressSeq", "activity", "metrics"},
            "state_change": {"dimension", "from_state", "to", "taskId", "contextId", "maxDurationSec"},
            "floor_change": {"conversationId", "from_state", "to", "role"},
            "deregister": set(),
        }
        if self.model_fields_set - common - fields[self.type]:
            raise ValueError("field is not allowed for this event type")
        if self.type == "register" and self.heartbeatIntervalSec is None:
            raise ValueError("registration requires heartbeatIntervalSec")
        if self.type in ("state_change", "floor_change"):
            transition_dimension = "floor" if self.type == "floor_change" else self.dimension
            if transition_dimension is None or self.from_state is None or self.to is None:
                raise ValueError("transition requires dimension, from and to")
            dimension: str = transition_dimension
            if not self.known_or_custom(dimension, self.to) or not self.known_or_custom(dimension, self.from_state):
                raise ValueError("unknown state must use the x- prefix")
            if dimension == "task" and not self.taskId:
                raise ValueError("task transition requires taskId")
            if dimension != "task" and (self.taskId or self.contextId or self.maxDurationSec):
                raise ValueError("task fields require task dimension")
            if dimension == "floor" and not self.conversationId:
                raise ValueError("floor transition requires conversationId")
            if self.maxDurationSec and self.to != "working":
                raise ValueError("maxDurationSec is only declared when working begins")
        for dimension, value in (("lifecycle", self.lifecycle), ("health", self.health)):
            if value is not None and not self.known_or_custom(dimension, value):
                raise ValueError("unknown state must use the x- prefix")
        if self.reason is not None and self.reason not in REASONS:
            raise ValueError("reason must be an allowlisted code")
        if self.activity is not None and self.activity not in ACTIVITIES:
            raise ValueError("activity must be an allowlisted code")
        if self.metrics is not None and (self.metrics.keys() - METRICS or any(v < 0 for v in self.metrics.values())):
            raise ValueError("unsupported metric or negative value")
        if self.protocols and any(not re.fullmatch(r"[A-Za-z0-9._/-]{1,40}", p) for p in self.protocols):
            raise ValueError("protocols must be bounded identifiers")
        if self.capabilities and any(not re.fullmatch(r"[a-z0-9-]{1,60}", p) for p in self.capabilities):
            raise ValueError("capabilities must be bounded codes")
        if self.ext is not None:
            if self.ext.keys() - {"local.ofp-playground"}:
                raise ValueError("extension namespace is not supported by this profile")
            for values in self.ext.values():
                if values.keys() - EXT_KEYS or any(not re.fullmatch(r"[A-Za-z0-9:.,_/@+-]{1,256}", v) for v in values.values()):
                    raise ValueError("extensions contain unsupported fields or values")
        if any(any(ord(character) < 32 or ord(character) == 127 for character in str(value))
               for value in (self.agentId, self.eventId, self.name, self.role, self.taskId, self.contextId,
                             self.conversationId, self.version)):
            raise ValueError("identifiers and labels cannot contain control characters")
        return self

    @staticmethod
    def known_or_custom(dimension: str, value: str) -> bool:
        return value in STATES[dimension] or bool(re.fullmatch(r"x-[a-z0-9-]{1,60}", value))

    def payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)
