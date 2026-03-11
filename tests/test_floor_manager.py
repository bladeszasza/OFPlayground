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
