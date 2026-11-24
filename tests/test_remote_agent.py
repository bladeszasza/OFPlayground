"""Tests for RemoteOFPAgent — pure logic: cascade prevention, payload construction."""
from __future__ import annotations

import pytest

from ofp_playground.agents.remote import KNOWN_REMOTE_AGENTS, RemoteOFPAgent, REMOTE_URI_TEMPLATE
from ofp_playground.bus.message_bus import MessageBus, FLOOR_MANAGER_URI


def _make_agent(name="ArXiv", url="https://example.com/ofp"):
    return RemoteOFPAgent(
        service_url=url,
        name=name,
        bus=MessageBus(),
        conversation_id="conv:test",
        timeout=10.0,
    )


# ---------------------------------------------------------------------------
# KNOWN_REMOTE_AGENTS constant
# ---------------------------------------------------------------------------

class TestKnownRemoteAgents:
    def test_polly_exists(self):
        assert "polly" in KNOWN_REMOTE_AGENTS

    def test_arxiv_exists(self):
        assert "arxiv" in KNOWN_REMOTE_AGENTS

    def test_wikipedia_exists(self):
        assert "wikipedia" in KNOWN_REMOTE_AGENTS

    def test_each_has_three_tuple(self):
        for slug, value in KNOWN_REMOTE_AGENTS.items():
            assert len(value) == 3, f"{slug} should be a 3-tuple"

    def test_slugs_are_lowercase(self):
        for slug in KNOWN_REMOTE_AGENTS:
            assert slug == slug.lower()


# ---------------------------------------------------------------------------
# _make_base_payload
# ---------------------------------------------------------------------------

class TestMakeBasePayload:
    def test_has_open_floor_key(self):
        agent = _make_agent()
        payload = agent._make_base_payload("tag:test:sender")
        assert "openFloor" in payload

    def test_schema_version(self):
        agent = _make_agent()
        payload = agent._make_base_payload("tag:test:sender")
        assert payload["openFloor"]["schema"]["version"] == "1.1.0"

    def test_conversation_id_set(self):
        agent = _make_agent()
        payload = agent._make_base_payload("tag:test:sender")
        assert payload["openFloor"]["conversation"]["id"] == "conv:test"

    def test_sender_uri_in_payload(self):
        agent = _make_agent()
        payload = agent._make_base_payload("tag:test:custom-sender")
        assert payload["openFloor"]["sender"]["speakerUri"] == "tag:test:custom-sender"

    def test_events_list_empty(self):
        agent = _make_agent()
        payload = agent._make_base_payload("tag:test:sender")
        assert payload["openFloor"]["events"] == []


# ---------------------------------------------------------------------------
# _should_respond — cascade prevention
# ---------------------------------------------------------------------------

class TestShouldRespond:
    def test_remote_to_remote_blocked(self):
        agent = _make_agent("ArXiv")
        respond, _ = agent._should_respond("tag:test:remote-wikipedia", "hello")
        assert respond is False

    def test_remote_sender_uri_blocked(self):
        agent = _make_agent("ArXiv")
        respond, _ = agent._should_respond("tag:ofp-playground.local,2025:remote-polly", "text")
        assert respond is False

    def test_floor_manager_without_directive_blocked(self):
        agent = _make_agent("ArXiv")
        respond, _ = agent._should_respond(FLOOR_MANAGER_URI, "Some narrative context update.")
        assert respond is False

    def test_floor_manager_with_matching_directive_allowed(self):
        agent = _make_agent("ArXiv")
        text = "[DIRECTIVE for ArXiv]: Find papers about black holes."
        respond, task = agent._should_respond(FLOOR_MANAGER_URI, text)
        assert respond is True
        assert "black holes" in task

    def test_floor_manager_with_different_agent_directive_blocked(self):
        agent = _make_agent("ArXiv")
        text = "[DIRECTIVE for Wikipedia]: Look up Mars."
        respond, _ = agent._should_respond(FLOOR_MANAGER_URI, text)
        assert respond is False

    def test_floor_manager_directive_case_insensitive(self):
        agent = _make_agent("ArXiv")
        text = "[DIRECTIVE for arxiv]: Find cosmic inflation papers."
        respond, task = agent._should_respond(FLOOR_MANAGER_URI, text)
        assert respond is True

    def test_human_utterance_always_allowed(self):
        agent = _make_agent("ArXiv")
        respond, task = agent._should_respond("tag:ofp-playground.local,2025:human-user", "Hello!")
        assert respond is True
        assert task == "Hello!"

    def test_llm_utterance_always_allowed(self):
        agent = _make_agent("ArXiv")
        respond, task = agent._should_respond("tag:ofp-playground.local,2025:llm-alice", "Tell me more.")
        assert respond is True
        assert task == "Tell me more."

    def test_task_text_is_full_message_for_local_sender(self):
        agent = _make_agent("ArXiv")
        _, task = agent._should_respond("tag:test:llm-bob", "What is quantum entanglement?")
        assert task == "What is quantum entanglement?"


# ---------------------------------------------------------------------------
# Speaker URI construction
# ---------------------------------------------------------------------------

class TestUri:
    def test_uri_follows_template(self):
        agent = _make_agent("ArXiv")
        assert "remote-arxiv" in agent.speaker_uri

    def test_spaces_replaced_in_uri(self):
        agent = _make_agent("My Agent")
        assert "remote-my-agent" in agent.speaker_uri


# ---------------------------------------------------------------------------
# OFP 1.1.0 envelopes sent to the remote (offline; HTTP is stubbed)
# ---------------------------------------------------------------------------

class _Recorder:
    """Stub the HTTP POST and the local bus for one RemoteOFPAgent."""

    def __init__(self, agent, replies):
        self.posted: list[dict] = []
        self.local: list = []
        self._replies = list(replies)

        async def post(payload):
            self.posted.append(payload)
            return self._replies.pop(0) if self._replies else []

        async def send(envelope):
            self.local.append(envelope)

        agent._post_envelope = post
        agent.send_envelope = send

    def local_types(self) -> list[str]:
        return [e.eventType for env in self.local for e in env.events]


VERITY_HELLO = [{
    "eventType": "utterance",
    "to": {"speakerUri": FLOOR_MANAGER_URI, "private": True},
    "parameters": {"dialogEvent": {"features": {"text": {"tokens": [{"value": "Ready to assist."}]}}}},
}]
MANIFEST = [{
    "eventType": "publishManifests",
    "parameters": {"servicingManifests": [{"identification": {"conversationalName": "Verity"}}]},
}]


class TestOFP110Envelopes:
    async def test_invite_puts_to_at_event_level_with_schema_110(self):
        agent = _make_agent("Verity", url="https://remote.example/ofp")
        rec = _Recorder(agent, [VERITY_HELLO, MANIFEST])
        await agent._handle_invite()
        invite = rec.posted[0]["openFloor"]
        assert invite["schema"] == {"version": "1.1.0"}
        event = invite["events"][0]
        assert event["eventType"] == "invite"
        assert event["to"] == {"speakerUri": agent.speaker_uri, "serviceUrl": "https://remote.example/ofp"}
        assert "parameters" not in event

    async def test_manifest_fetched_with_get_manifests_when_invite_reply_has_none(self):
        agent = _make_agent("Verity")
        rec = _Recorder(agent, [VERITY_HELLO, MANIFEST])
        await agent._handle_invite()
        get = rec.posted[1]["openFloor"]["events"][0]
        assert get["eventType"] == "getManifests"
        assert get["parameters"] == {"recommendScope": "internal"}
        assert rec.local_types() == ["acceptInvite", "publishManifests"]

    async def test_legacy_manifest_in_invite_reply_is_still_relayed(self):
        agent = _make_agent("Old")
        rec = _Recorder(agent, [MANIFEST])
        await agent._handle_invite()
        assert len(rec.posted) == 1  # no getManifests round trip needed
        assert rec.local_types() == ["acceptInvite", "publishManifests"]

    async def test_decline_is_relayed_and_stops_the_proxy(self):
        agent = _make_agent("Busy")
        agent._running = True
        rec = _Recorder(agent, [[{"eventType": "declineInvite", "reason": "@unavailable"}]])
        await agent._handle_invite()
        assert rec.local_types() == ["declineInvite"]
        assert rec.local[0].events[0].reason == "@unavailable"
        assert agent._running is False

    async def test_unreachable_remote_still_accepts(self):
        agent = _make_agent("Offline")
        rec = _Recorder(agent, [None, None])
        await agent._handle_invite()
        assert rec.local_types() == ["acceptInvite"]

    async def test_bye_in_answer_relays_bye_after_the_answer(self):
        agent = _make_agent("Brief")
        agent._running = True
        agent._pending_text = "hi"
        agent._pending_sender_uri = "tag:ofp-playground.local,2025:human-user"
        rec = _Recorder(agent, [VERITY_HELLO + [{"eventType": "bye"}]])
        await agent._handle_grant_floor()
        assert rec.local_types() == ["utterance", "yieldFloor", "bye"]
        assert agent._running is False

    async def test_uninvite_is_forwarded_to_the_remote(self):
        from openfloor import Conversation, Envelope, Sender, To, UninviteEvent

        agent = _make_agent("Gone", url="https://remote.example/ofp")
        agent._running = True
        rec = _Recorder(agent, [[]])
        await agent._dispatch(Envelope(
            sender=Sender(speakerUri=FLOOR_MANAGER_URI),
            conversation=Conversation(id="conv:test"),
            events=[UninviteEvent(to=To(speakerUri=agent.speaker_uri), reason="@timedOut")],
        ))
        event = rec.posted[0]["openFloor"]["events"][0]
        assert event["eventType"] == "uninvite"
        assert event["reason"] == "@timedOut"
        assert event["to"]["serviceUrl"] == "https://remote.example/ofp"
        assert agent._running is False
