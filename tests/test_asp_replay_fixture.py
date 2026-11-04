"""Replay a captured provider-free journal using recorded identity and receive time."""
import json
from pathlib import Path

from ofp_playground.status.models import ASPEvent, Credential
from ofp_playground.status.store import StatusStore


def test_captured_showcase_replays_with_authority_ordering_and_dedup():
    records = [json.loads(line) for line in (Path(__file__).parent / "fixtures" / "asp-showcase.ndjson").read_text().splitlines()]
    now = [records[0]["receivedAt"]]
    store = StatusStore(clock=lambda: now[0])
    try:
        for record in records:
            event = ASPEvent.model_validate(record["event"])
            now[0] = record["receivedAt"]
            credential = Credential(record["reporterId"], event.source, frozenset({event.agentId}))
            store.ingest(event, credential, internal=event.source == "server")
        assert store.cursor == len(records)
        for record in records:
            event = ASPEvent.model_validate(record["event"])
            credential = Credential(record["reporterId"], event.source, frozenset({event.agentId}))
            store.ingest(event, credential, internal=event.source == "server")
        assert store.cursor == len(records)
        assert any(r["event"].get("to") == "unreachable" and r["event"]["source"] == "server" for r in records)
        assert any(r["event"].get("to") == "stalled" and r["event"]["source"] == "server" for r in records)
        assert any(a["governance"]["value"] == "quarantined" and a["governance"]["source"] == "guardrail" for a in store.agents.values())
        assert all(a["lifecycle"]["value"] == "offline" for a in store.agents.values())
    finally:
        store.close()
