"""Opt-in single provider request through the real OFP/ASP session path.

Default invocation only explains the limits. No model is selected silently.
"""
from __future__ import annotations

import argparse
import asyncio
import os


async def run(model: str) -> None:
    from openai import OpenAI

    from ofp_playground.agents.llm.openai import OpenAIAgent
    from ofp_playground.agents.registry import AgentRegistry
    from ofp_playground.bus.message_bus import MessageBus
    from ofp_playground.floor.manager import FloorManager
    from ofp_playground.session import SessionService

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("Set OPENAI_API_KEY for this opt-in example")
    bus = MessageBus()
    floor = FloorManager(bus)
    session = SessionService(floor, bus, AgentRegistry())
    await session.enable_status()
    await bus.register(floor.speaker_uri, floor.queue)

    class OneRequest(OpenAIAgent):
        async def run(self) -> None:
            await bus.register(self.speaker_uri, self.queue)
            await self.request_floor()
            while True:
                envelope = await self.queue.get()
                if any(event.eventType == "grantFloor" for event in envelope.events):
                    result = await self._call_with_retry(lambda: self._generate_response([]))
                    if result:
                        await self.send_envelope(self._make_utterance_envelope(result))
                    await self.yield_floor()
                    return

    with OpenAI(api_key=key, timeout=15.0, max_retries=0) as client:
        agent = OneRequest(name="LiveExample", synopsis="Suggest one practical way to inspect multi-agent status in one sentence.",
                           bus=bus, conversation_id=floor.conversation_id,
                           api_key=key, model=model, relevance_filter=False)
        agent._client = client
        agent._max_tokens = 128
        agent._max_retries = 0
        agent._timeout = 20
        floor.register_agent(agent.speaker_uri, agent.name)
        session.registry.register(agent)
        session.registry.start(agent)
        try:
            await asyncio.wait_for(session.run(), timeout=30)
        finally:
            await session.close()
    print("Single-request example ended. Inspect ofp-playground status --agent LiveExample.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-paid", action="store_true", help="Explicitly allow this one billable request")
    parser.add_argument("--model", help="Exact account-supported Responses model; no default")
    args = parser.parse_args()
    if not args.run_paid:
        print("No request made. Opt in with --run-paid --model MODEL. Limits: one application request, no SDK/application retries, 128 output tokens, 15s transport timeout, 30s session wait. This is not a dollar budget; a submitted remote job may continue after cancellation.")
        return
    if not args.model:
        parser.error("--model is required with --run-paid")
    try:
        asyncio.run(run(args.model))
    except (ValueError, TimeoutError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
