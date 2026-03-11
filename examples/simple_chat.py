"""Simple example: 1 human + 1 Anthropic agent."""
import asyncio
import os
import sys

sys.path.insert(0, "src")

from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.floor.manager import FloorManager
from ofp_playground.floor.policy import FloorPolicy
from ofp_playground.agents.human import HumanAgent
from ofp_playground.agents.llm.anthropic import AnthropicAgent
from ofp_playground.renderer.terminal import TerminalRenderer
from rich.console import Console


async def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Set ANTHROPIC_API_KEY environment variable")
        return

    renderer = TerminalRenderer(Console())
    bus = MessageBus()
    floor = FloorManager(bus, policy=FloorPolicy.SEQUENTIAL, renderer=renderer)

    renderer.show_header(floor.conversation_id, "sequential", 2)

    human = HumanAgent(
        name="User",
        bus=bus,
        conversation_id=floor.conversation_id,
        renderer=renderer,
        floor_policy="sequential",
    )
    floor.register_agent(human.speaker_uri, human.name)

    claude = AnthropicAgent(
        name="Claude",
        synopsis="A helpful AI assistant",
        bus=bus,
        conversation_id=floor.conversation_id,
        api_key=api_key,
        relevance_filter=False,  # Always respond in simple chat
    )
    floor.register_agent(claude.speaker_uri, claude.name)

    renderer.show_system_event("Starting conversation with Claude...")

    await asyncio.gather(
        floor.run(),
        human.run(),
        claude.run(),
        return_exceptions=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
