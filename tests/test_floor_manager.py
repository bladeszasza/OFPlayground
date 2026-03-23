"""Tests for the floor manager."""
import asyncio
import pytest

from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.floor.manager import FloorManager
from ofp_playground.floor.policy import FloorPolicy


@pytest.mark.asyncio
async def test_floor_manager_init():
    bus = MessageBus()
    fm = FloorManager(bus, policy=FloorPolicy.SEQUENTIAL)
    assert fm.conversation_id.startswith("conv:")
    assert fm.floor_holder is None


@pytest.mark.asyncio
async def test_register_agent():
    bus = MessageBus()
    fm = FloorManager(bus, policy=FloorPolicy.SEQUENTIAL)
    fm.register_agent("tag:test:agent-1", "TestAgent")
    assert "tag:test:agent-1" in fm.active_agents
    assert fm.active_agents["tag:test:agent-1"] == "TestAgent"


# ---------------------------------------------------------------------------
# _resolve_agent_uri_by_name — normalization tests
# ---------------------------------------------------------------------------

def test_resolve_exact_match():
    bus = MessageBus()
    fm = FloorManager(bus)
    fm.register_agent("tag:test:analyst", "Analyst")
    assert fm._resolve_agent_uri_by_name("Analyst") == "tag:test:analyst"


def test_resolve_case_insensitive():
    bus = MessageBus()
    fm = FloorManager(bus)
    fm.register_agent("tag:test:analyst", "Analyst")
    assert fm._resolve_agent_uri_by_name("analyst") == "tag:test:analyst"
    assert fm._resolve_agent_uri_by_name("ANALYST") == "tag:test:analyst"


def test_resolve_underscore_matches_space():
    bus = MessageBus()
    fm = FloorManager(bus)
    fm.register_agent("tag:test:wiki", "Wikipedia Research Specialist")
    assert fm._resolve_agent_uri_by_name("Wikipedia_Research_Specialist") == "tag:test:wiki"


def test_resolve_space_matches_underscore():
    bus = MessageBus()
    fm = FloorManager(bus)
    fm.register_agent("tag:test:wiki", "Wikipedia_Research_Specialist")
    assert fm._resolve_agent_uri_by_name("Wikipedia Research Specialist") == "tag:test:wiki"


def test_resolve_unknown_returns_none():
    bus = MessageBus()
    fm = FloorManager(bus)
    fm.register_agent("tag:test:analyst", "Analyst")
    assert fm._resolve_agent_uri_by_name("UnknownAgent") is None
