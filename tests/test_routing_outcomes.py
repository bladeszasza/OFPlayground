"""Trace facts must distinguish accepted queues from missing or overflowing routes."""
import asyncio

from openfloor import Conversation, DialogEvent, Envelope, Sender, TextFeature, Token, UtteranceEvent
import pytest

from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.trace.collector import EventCollector


@pytest.mark.parametrize("private", [False, True])
async def test_queue_acceptance_and_drops_are_explicit(private):
    bus = MessageBus()
    collector = EventCollector("test")
    bus.set_collector(collector)
    full = asyncio.Queue(maxsize=1)
    full.put_nowait("already occupied")
    await bus.register("full", full)
    await bus.register("ready", asyncio.Queue())
    envelope = Envelope(
        sender=Sender(speakerUri="sender", serviceUrl="local://sender"),
        conversation=Conversation(id="test"),
        events=[UtteranceEvent(dialogEvent=DialogEvent(
            id="one", speakerUri="sender",
            features={"text": TextFeature(tokens=[Token(value="hello")])},
        ))],
    )
    if private:
        await bus.send_private(envelope, "full")
    else:
        await bus.send(envelope)
    outcomes = collector.events[0].delivery_outcomes
    assert outcomes["full"] == "dropped-queue-full"
    assert "unregistered" in outcomes.values()  # floor was not registered
    if not private:
        assert outcomes["ready"] == "queued"
    assert "processed" not in outcomes.values()
