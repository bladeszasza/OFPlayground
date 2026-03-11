"""Tests for agents."""
import asyncio
import pytest

from ofp_playground.agents.base import BasePlaygroundAgent
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
