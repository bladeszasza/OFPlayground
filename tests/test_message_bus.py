"""Tests for the message bus."""
import asyncio
import pytest
from openfloor import Envelope, Sender, Conversation

from ofp_playground.bus.message_bus import MessageBus, FLOOR_MANAGER_URI


def make_envelope(sender_uri: str, conv_id: str = "conv:test") -> Envelope:
    return Envelope(
        sender=Sender(speakerUri=sender_uri, serviceUrl="local://test"),
        conversation=Conversation(id=conv_id),
        events=[],
    )


@pytest.mark.asyncio
async def test_register_and_send():
    bus = MessageBus()
    queue = asyncio.Queue()
    fm_queue = asyncio.Queue()

    await bus.register("tag:test:agent-1", queue)
    await bus.register(FLOOR_MANAGER_URI, fm_queue)

    envelope = make_envelope("tag:test:sender")
    await bus.send(envelope)

    # agent-1 should receive it (broadcast)
    assert not queue.empty()
    # floor manager should receive it
    assert not fm_queue.empty()


@pytest.mark.asyncio
async def test_sender_does_not_receive_own_message():
    bus = MessageBus()
    sender_queue = asyncio.Queue()
    other_queue = asyncio.Queue()
    fm_queue = asyncio.Queue()

    await bus.register("tag:test:sender", sender_queue)
    await bus.register("tag:test:other", other_queue)
    await bus.register(FLOOR_MANAGER_URI, fm_queue)

    envelope = make_envelope("tag:test:sender")
    await bus.send(envelope)

    assert sender_queue.empty()  # Sender doesn't get own messages
    assert not other_queue.empty()


@pytest.mark.asyncio
async def test_unregister():
    bus = MessageBus()
    queue = asyncio.Queue()

    await bus.register("tag:test:agent", queue)
    await bus.unregister("tag:test:agent")

    assert "tag:test:agent" not in bus.registered_agents
