"""Tests for agents."""
import asyncio
import pytest
from openfloor import Conversation, DialogEvent, Envelope, Sender, TextFeature, Token, UtteranceEvent

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.agents.human import HumanAgent
from ofp_playground.bus.message_bus import MessageBus


class ConcreteAgent(BasePlaygroundAgent):
    async def run(self):
        self._running = True


def test_agent_init():
    bus = MessageBus()
    agent = ConcreteAgent(
        speaker_uri="tag:test:agent",
        name="TestAgent",
        service_url="local://test",
        bus=bus,
        conversation_id="conv:test",
    )
    assert agent.speaker_uri == "tag:test:agent"
    assert agent.name == "TestAgent"


def test_make_utterance_envelope():
    bus = MessageBus()
    agent = ConcreteAgent(
        speaker_uri="tag:test:agent",
        name="TestAgent",
        service_url="local://test",
        bus=bus,
        conversation_id="conv:test",
    )
    envelope = agent._make_utterance_envelope("Hello, world!")
    assert envelope.sender.speakerUri == "tag:test:agent"
    assert len(envelope.events) == 1


@pytest.mark.asyncio
async def test_human_requests_floor_after_other_utterance_in_sequential_mode():
    bus = MessageBus()
    human = HumanAgent(
        name="User",
        bus=bus,
        conversation_id="conv:test",
        renderer=None,
        floor_policy="sequential",
    )

    called_reasons = []

    async def fake_request_floor(reason: str = "") -> None:
        called_reasons.append(reason)

    human.request_floor = fake_request_floor  # type: ignore[method-assign]
    human._has_floor = False

    envelope = Envelope(
        sender=Sender(speakerUri="tag:test:assistant", serviceUrl="local://assistant"),
        conversation=Conversation(id="conv:test"),
        events=[
            UtteranceEvent(
                dialogEvent=DialogEvent(
                    id="1",
                    speakerUri="tag:test:assistant",
                    features={"text": TextFeature(tokens=[Token(value="hello")])},
                )
            )
        ],
    )

    await human._handle_incoming(envelope)

    assert called_reasons == ["waiting for next turn"]


@pytest.mark.asyncio
async def test_human_does_not_immediately_rerequest_after_speaking():
    bus = MessageBus()
    human = HumanAgent(
        name="User",
        bus=bus,
        conversation_id="conv:test",
        renderer=None,
        floor_policy="sequential",
    )
    human._running = True
    human._has_floor = True

    async def fake_read_input():
        if not hasattr(fake_read_input, "count"):
            fake_read_input.count = 0  # type: ignore[attr-defined]
        fake_read_input.count += 1  # type: ignore[attr-defined]
        return "hello" if fake_read_input.count == 1 else None  # type: ignore[attr-defined]

    sent = []
    yielded = []
    requested = []

    async def fake_send_envelope(_envelope):
        sent.append(True)

    async def fake_yield_floor(reason: str = "@complete"):
        yielded.append(reason)

    async def fake_request_floor(reason: str = ""):
        requested.append(reason)

    human._read_input = fake_read_input  # type: ignore[method-assign]
    human.send_envelope = fake_send_envelope  # type: ignore[method-assign]
    human.yield_floor = fake_yield_floor  # type: ignore[method-assign]
    human.request_floor = fake_request_floor  # type: ignore[method-assign]

    await human._input_loop()

    assert sent == [True]
    assert yielded == ["@complete"]
    assert requested == []
