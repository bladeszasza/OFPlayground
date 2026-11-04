"""A pipe without a newline must remain cancellable on Windows and POSIX."""
import asyncio
import io
import os
import sys

from ofp_playground.agents.human import HumanAgent
from ofp_playground.bus.message_bus import MessageBus


async def test_partial_pipe_input_does_not_block_cancellation(monkeypatch):
    reader, writer = os.pipe()
    stream = io.TextIOWrapper(os.fdopen(reader, "rb"), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stream)
    agent = HumanAgent("Human", MessageBus(), "test")
    task = asyncio.create_task(agent._read_input())
    try:
        os.write(writer, b"partial")
        await asyncio.sleep(0.08)
        assert not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        os.write(writer, b" line\nnext\n")
        assert await asyncio.wait_for(agent._read_input(), 1) == "partial line"
        assert await asyncio.wait_for(agent._read_input(), 1) == "next"
        os.close(writer)
        writer = None
        assert await asyncio.wait_for(agent._read_input(), 1) is None
    finally:
        if writer is not None:
            os.close(writer)
        stream.close()
