"""Behavioral ASP profile gates against a deterministic clock and durable journal."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from pydantic import ValidationError

from ofp_playground.status.models import ASPEvent, Credential, StatusError
from ofp_playground.status.store import StatusStore


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


SELF = Credential("agent-process", "self", frozenset({"a"}))
PEER = Credential("observer", "peer", frozenset({"a"}), observe=True)
GUARD = Credential("guard", "guardrail", frozenset({"a"}))
OPERATOR = Credential("operator", "operator", frozenset({"*"}), observe=True)


def event(seq=0, event_type="heartbeat", source="self", **fields):
    return ASPEvent.model_validate({
        "aspVersion": "0.1", "type": event_type, "agentId": "a",
        "eventId": str(uuid.uuid4()), "seq": seq, "ts": "2026-11-01T10:00:00Z",
        "source": source, **fields,
    })


def change(seq, dimension, to, source="self", **fields):
    starts = {"task": "submitted", "health": "healthy", "lifecycle": "online", "governance": "normal"}
    return event(seq, "state_change", source, dimension=dimension, to=to,
                 **{"from": starts[dimension]}, **fields)


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    status = StatusStore(tmp_path / "status.sqlite", clock=clock)
    status.ingest(event(0, "register", heartbeatIntervalSec=2, stallAfterSec=3), SELF)
    yield status, clock
    status.close()


def test_registration_and_credential_bound_authority(tmp_path):
    status = StatusStore(tmp_path / "db")
    with pytest.raises(StatusError, match="register before"):
        status.ingest(event(1), SELF)
    with pytest.raises(StatusError, match="scope"):
        status.ingest(event(0, "register", source="operator", heartbeatIntervalSec=2), SELF)
    with pytest.raises(StatusError, match="read-only"):
        status.ingest(event(0), Credential("reader", "self", frozenset({"a"}), ingest=False))
    status.close()


def test_old_equal_duplicate_events_do_not_overwrite_or_refresh_liveness(store):
    status, clock = store
    newer = event(10, lifecycle="busy", progressSeq=2)
    status.ingest(newer, SELF)
    clock.advance(4)
    assert status.ingest(newer, SELF)["duplicate"]
    assert not status.ingest(event(9, lifecycle="idle", progressSeq=100), SELF)["applied"]
    assert not status.ingest(event(10, lifecycle="idle", progressSeq=100), SELF)["applied"]
    assert status.agents["a"]["lifecycle"]["value"] == "busy"
    assert status.agents["a"]["lastSelfEventAt"] == 1000
    assert len(status.history()) == 4
    assert [r["disposition"] for r in status.history()][-2:] == ["out-of-order"] * 2


def test_independent_reports_never_refresh_self_liveness_and_disagreements_remain(store):
    status, clock = store
    status.ingest(change(1, "task", "working", taskId="t"), SELF)
    status.ingest(event(2, "floor_change", conversationId="c", **{"from": "joined", "to": "has-floor"}), SELF)
    clock.advance(4)
    status.ingest(change(500, "health", "degraded", "peer", reason="call-timeout"), PEER)
    assert status.agents["a"]["health"]["value"] == "healthy"
    assert status.agents["a"]["observations"]["observer"]["value"] == "degraded"
    clock.advance(1)
    status.tick()
    agent = status.agents["a"]
    assert agent["lifecycle"]["value"] == "unreachable"
    assert agent["lifecycle"]["source"] == "server"
    assert agent["tasks"]["t"]["stale"] and agent["floors"]["c"]["stale"]
    status.ingest(event(3, progressSeq=2), SELF)
    agent = status.agents["a"]
    assert agent["lifecycle"]["value"] == "online"
    assert not agent["tasks"]["t"]["stale"]


def test_retry_transitions_cannot_reset_progress_stall_clock(store):
    status, clock = store
    status.ingest(change(1, "task", "working", taskId="t"), SELF)
    clock.advance(1.5)
    status.ingest(event(2, progressSeq=0), SELF)
    status.ingest(change(3, "task", "retrying", taskId="t"), SELF)
    clock.advance(1.5)
    status.ingest(event(4, progressSeq=0), SELF)
    status.ingest(change(5, "task", "working", taskId="t"), SELF)
    status.tick()
    assert status.agents["a"]["health"]["value"] == "stalled"
    assert status.agents["a"]["health"]["reason"] == "no-progress"


def test_known_supervisor_crash_retains_stale_scopes_until_contact_returns(store):
    status, clock = store
    status.ingest(change(1, "task", "working", taskId="t"), SELF)
    status.ingest(event(2, "floor_change", conversationId="c", **{"from": "joined", "to": "has-floor"}), SELF)
    supervisor = Credential("parent", "supervisor", frozenset({"a"}))
    status.ingest(change(0, "lifecycle", "crashed", "supervisor", reason="process-exit"), supervisor)
    clock.advance(6)
    status.tick()
    agent = status.agents["a"]
    assert agent["lifecycle"]["value"] == "crashed"
    assert agent["tasks"]["t"]["stale"] and agent["floors"]["c"]["stale"]
    status.ingest(event(3, progressSeq=1), SELF)
    assert status.agents["a"]["lifecycle"]["value"] == "online"
    assert not status.agents["a"]["tasks"]["t"]["stale"]


@pytest.mark.parametrize("fields", [{"seq": 2**64}, {"taskId": "task\x00id"}, {"conversationId": "c\tID"}])
def test_bounded_sequences_and_control_free_identifiers(fields):
    with pytest.raises(ValidationError):
        ASPEvent.model_validate({"aspVersion": "0.1", "type": "state_change", "agentId": "a",
                                "eventId": "e", "seq": 1, "ts": "2026-11-01T10:00:00Z",
                                "dimension": "task", "from": "submitted", "to": "working",
                                "taskId": "t", **fields})


@pytest.mark.parametrize("wait", ["queued", "input-required", "auth-required", "awaiting-approval", "delegated"])
def test_designed_waits_are_not_stalls(store, wait):
    status, clock = store
    status.ingest(change(1, "task", wait, taskId="t"), SELF)
    for seq in range(2, 8):
        clock.advance(1)
        status.ingest(event(seq, progressSeq=0), SELF)
        status.tick()
    assert status.agents["a"]["health"]["value"] == "healthy"


@pytest.mark.parametrize("work", ["working", "retrying"])
def test_progress_not_heartbeat_controls_stall_and_recovery(store, work):
    status, clock = store
    status.ingest(change(1, "task", work, taskId="t"), SELF)
    status.ingest(event(2, progressSeq=0, health="rate-limited"), SELF)
    for seq in range(3, 7):
        clock.advance(1)
        status.ingest(event(seq, progressSeq=0, health="healthy"), SELF)
        status.tick()
    assert status.agents["a"]["health"]["value"] == "stalled"
    assert status.agents["a"]["lifecycle"]["value"] != "unreachable"
    status.ingest(event(7, progressSeq=1, health="degraded"), SELF)
    assert status.agents["a"]["health"]["value"] == "degraded"


def test_governance_cannot_be_self_cleared_and_principals_have_separate_sequences(store):
    status, _ = store
    status.ingest(change(1, "governance", "quarantined", "guardrail"), GUARD)
    with pytest.raises(StatusError, match="higher authority"):
        status.ingest(change(50, "governance", "normal"), SELF)
    status.ingest(change(0, "governance", "under-review", "operator"), OPERATOR)
    with pytest.raises(StatusError, match="higher authority"):
        status.ingest(change(2, "governance", "normal", "guardrail"), GUARD)
    status.ingest(change(1, "governance", "normal", "operator"), OPERATOR)
    assert status.agents["a"]["governance"]["value"] == "normal"
    other = Credential("second-operator", "operator", frozenset({"a"}))
    assert status.ingest(change(0, "governance", "quarantined", "operator"), other)["applied"]


@pytest.mark.parametrize("state", ["unreachable", "stalled", "crashed"])
def test_agent_cannot_assign_server_or_supervisor_states(store, state):
    status, _ = store
    dim = "health" if state == "stalled" else "lifecycle"
    with pytest.raises(StatusError):
        status.ingest(change(1, dim, state), SELF)


def test_task_deadlines_and_terminal_states_are_scoped(store):
    status, clock = store
    status.ingest(change(1, "task", "working", taskId="t1", maxDurationSec=2), SELF)
    status.ingest(change(2, "task", "completed", taskId="t2"), SELF)
    clock.advance(2)
    status.tick()
    assert status.agents["a"]["tasks"]["t1"]["value"] == "timed-out"
    assert status.agents["a"]["tasks"]["t2"]["value"] == "completed"
    with pytest.raises(StatusError, match="terminal"):
        status.ingest(change(3, "task", "working", taskId="t2"), SELF)
    assert status.ingest(change(4, "task", "working", taskId="t3"), SELF)["applied"]


def test_restart_retains_watermarks_snapshots_and_dedup_after_retention(tmp_path):
    path = tmp_path / "durable.sqlite"
    status = StatusStore(path, retention=2)
    register = event(0, "register", heartbeatIntervalSec=2)
    status.ingest(register, SELF)
    status.ingest(event(1, lifecycle="busy"), SELF)
    status.ingest(event(2, lifecycle="idle"), SELF)
    status.close()
    status = StatusStore(path, retention=2)
    assert status.cursor == 3
    assert status.agents["a"]["lifecycle"]["value"] == "idle"
    assert status.ingest(register, SELF)["duplicate"]
    assert not status.ingest(event(1, lifecycle="busy"), SELF)["applied"]
    assert status.agents["a"]["lifecycle"]["value"] == "idle"
    status.close()


def test_subscription_cutoff_gap_and_slow_observer_isolation(tmp_path):
    status = StatusStore(tmp_path / "db", retention=2, subscriber_size=1)
    status.ingest(event(0, "register", heartbeatIntervalSec=2), SELF)
    fast, initial = status.subscribe(1)
    slow, _ = status.subscribe(1)
    assert initial == []
    status.ingest(event(1), SELF)
    assert fast.get_nowait()["cursor"] == 2
    status.ingest(event(2), SELF)
    assert fast.get_nowait()["cursor"] == 3
    assert slow.get_nowait()["kind"] == "disconnect"
    assert slow not in status.subscribers
    resumed, replay = status.subscribe(2)
    assert [r["cursor"] for r in replay] == [3]
    status.ingest(event(3), SELF)
    assert resumed.get_nowait()["cursor"] == 4
    gap, reset = status.subscribe(1)
    assert reset[0]["kind"] == "gap" and reset[1]["kind"] == "snapshot"
    status.unsubscribe(gap)
    status.close()


def test_deregister_remains_offline_until_explicit_reregistration(store):
    status, clock = store
    status.ingest(event(1, "deregister"), SELF)
    clock.advance(100)
    status.tick()
    assert status.agents["a"]["lifecycle"]["value"] == "offline"
    with pytest.raises(StatusError, match="register again"):
        status.ingest(event(2, progressSeq=0), SELF)
    status.ingest(event(3, "register", heartbeatIntervalSec=2), SELF)
    assert status.agents["a"]["lifecycle"]["value"] == "online"


@pytest.mark.parametrize("fields", [
    {"seq": True}, {"prompt": "secret"}, {"reason": "my API key is xyz"},
    {"activity": "raw user prompt"}, {"metrics": {"prompt": 1}},
    {"ext": {"example.com": {"prompt": "secret"}}},
    {"ts": "2026-11-01T10:00:00"}, {"heartbeatIntervalSec": 301},
])
def test_profile_rejects_unbounded_or_content_bearing_fields(fields):
    with pytest.raises(ValidationError):
        event(**fields)


def test_unknown_namespaced_states_remain_visible(store):
    status, _ = store
    status.ingest(change(1, "health", "x-test-state"), SELF)
    assert status.agents["a"]["health"]["value"] == "x-test-state"
    assert not status.agents["a"]["health"]["known"]
