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


# ---------------------------------------------------------------------------
# _OrchestratorBase helpers — _resolve_name_in_registry, _spawn_or_assign
# ---------------------------------------------------------------------------

class _MockOrchestratorBase:
    """Minimal stub that enables testing _OrchestratorBase methods without a real LLM."""

    def __init__(self, name_registry: dict):
        self._name_registry = name_registry
        self._name = "Director"
        self._mission = "test"
        self._manifest_registry = {}
        self._memory_store = None
        self.speaker_uri = "tag:test:orchestrator"

    # Pull in the real implementations from the mixin
    from ofp_playground.agents.llm.showrunner import _OrchestratorBase
    _resolve_name_in_registry = _OrchestratorBase._resolve_name_in_registry
    _spawn_or_assign = _OrchestratorBase._spawn_or_assign


def test_resolve_name_exact():
    o = _MockOrchestratorBase({"tag:test:analyst": "Analyst"})
    assert o._resolve_name_in_registry("Analyst") == "tag:test:analyst"


def test_resolve_name_case_insensitive():
    o = _MockOrchestratorBase({"tag:test:analyst": "Analyst"})
    assert o._resolve_name_in_registry("ANALYST") == "tag:test:analyst"
    assert o._resolve_name_in_registry("analyst") == "tag:test:analyst"


def test_resolve_name_underscore_to_space():
    o = _MockOrchestratorBase({"tag:test:wiki": "Wikipedia Research Specialist"})
    assert o._resolve_name_in_registry("Wikipedia_Research_Specialist") == "tag:test:wiki"


def test_resolve_name_space_to_underscore():
    o = _MockOrchestratorBase({"tag:test:wiki": "Wikipedia_Research_Specialist"})
    assert o._resolve_name_in_registry("Wikipedia Research Specialist") == "tag:test:wiki"


def test_resolve_name_not_found():
    o = _MockOrchestratorBase({"tag:test:analyst": "Analyst"})
    assert o._resolve_name_in_registry("Unknown") is None


def test_spawn_or_assign_new_agent():
    """When agent is NOT in registry, full [SPAWN]+[ASSIGN] directive is returned."""
    o = _MockOrchestratorBase({})
    result = o._spawn_or_assign("spawn_text_agent", {
        "name": "Writer",
        "system": "You write stories",
        "initial_task": "Write chapter 1",
        "provider": "hf",
    })
    assert "[SPAWN" in result
    assert "[ASSIGN Writer]: Write chapter 1" in result


def test_spawn_or_assign_existing_agent_skips_spawn():
    """When agent IS in registry, only [ASSIGN] is returned — no [SPAWN]."""
    o = _MockOrchestratorBase({"tag:test:analyst": "Analyst"})
    result = o._spawn_or_assign("spawn_text_agent", {
        "name": "Analyst",
        "system": "You analyse",
        "initial_task": "Analyse the data",
        "provider": "openai",
    })
    assert "[SPAWN" not in result
    assert result == "[ASSIGN Analyst]: Analyse the data"


def test_spawn_or_assign_existing_underscore_name():
    """Underscore→space normalization works in spawn redirect too."""
    o = _MockOrchestratorBase({"tag:test:wiki": "Wikipedia Research Specialist"})
    result = o._spawn_or_assign("spawn_text_agent", {
        "name": "Wikipedia_Research_Specialist",
        "system": "Research",
        "initial_task": "Summarise findings",
        "provider": "anthropic",
    })
    assert "[SPAWN" not in result
    assert "[ASSIGN Wikipedia_Research_Specialist]: Summarise findings" == result


def test_spawn_or_assign_no_task_returns_empty_for_existing():
    """If initial_task is missing and agent exists, returns empty string (no directive)."""
    o = _MockOrchestratorBase({"tag:test:analyst": "Analyst"})
    result = o._spawn_or_assign("spawn_text_agent", {
        "name": "Analyst",
        "system": "You analyse",
        "initial_task": "",
        "provider": "openai",
    })
    assert result == ""
