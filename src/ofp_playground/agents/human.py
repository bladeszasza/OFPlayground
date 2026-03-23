"""Human agent: bridges terminal stdin/stdout with OFP envelopes."""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional, TYPE_CHECKING

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus, FLOOR_MANAGER_URI

if TYPE_CHECKING:
    from ofp_playground.renderer.terminal import TerminalRenderer

logger = logging.getLogger(__name__)

HUMAN_URI_TEMPLATE = "tag:ofp-playground.local,2025:human-{name}"


class HumanAgent(BasePlaygroundAgent):
    """Human participant in the conversation.

    Reads input from stdin, wraps in OFP envelopes, displays received messages.
    Supports / commands for session management.
    """

    def __init__(
        self,
        name: str,
        bus: MessageBus,
        conversation_id: str,
        renderer: Optional["TerminalRenderer"] = None,
        floor_policy: str = "sequential",
    ):
        speaker_uri = HUMAN_URI_TEMPLATE.format(name=name.lower().replace(" ", "-"))
        super().__init__(
            speaker_uri=speaker_uri,
            name=name,
            service_url=f"local://human-{name.lower()}",
            bus=bus,
            conversation_id=conversation_id,
        )
        self._renderer = renderer
        self._floor_policy = floor_policy
        self._has_floor = floor_policy == "free_for_all"
        self._command_handlers: dict[str, callable] = {}
        self._stop_event = asyncio.Event()

    def register_command(self, command: str, handler) -> None:
        self._command_handlers[command] = handler

    async def _handle_incoming(self, envelope) -> None:
        """Process incoming envelope from the bus."""
        sender_uri = self._get_sender_uri(envelope)

        for event in (envelope.events or []):
            event_type = getattr(event, "eventType", type(event).__name__)

            if event_type == "utterance":
                text = self._extract_text_from_envelope(envelope)
                if text and self._renderer:
                    # Don't display our own messages again
                    if sender_uri != self.speaker_uri:
                        pass  # Renderer handles this via floor manager broadcast
                if (
                    sender_uri != self.speaker_uri
                    and self._floor_policy == "sequential"
                    and not self._has_floor
                ):
                    # In sequential mode, ask for the next turn after others speak.
                    await self.request_floor("waiting for next turn")

            elif event_type == "grantFloor":
                self._has_floor = True
                if self._renderer:
                    self._renderer.show_system_event(f"Floor granted to you, {self._name}")

            elif event_type == "revokeFloor":
                self._has_floor = False
                if self._renderer:
                    self._renderer.show_system_event("Your floor has been revoked")

    async def _read_input(self) -> Optional[str]:
        """Read a line from stdin asynchronously."""
        loop = asyncio.get_event_loop()
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            return line.rstrip("\n") if line else None
        except (EOFError, KeyboardInterrupt):
            return None

    async def _handle_command(self, command: str) -> bool:
        """Handle a /command. Returns True if should continue, False to quit."""
        parts = command[1:].strip().split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        args = parts[1] if len(parts) > 1 else ""

        if cmd in ("quit", "exit", "q"):
            self._stop_event.set()
            return False
        elif cmd == "history":
            # Handled by CLI/renderer
            if cmd in self._command_handlers:
                await self._command_handlers[cmd](args)
        elif cmd in self._command_handlers:
            await self._command_handlers[cmd](args)
        else:
            if self._renderer:
                self._renderer.show_system_event(
                    f"Unknown command: /{cmd}. Type /help for commands."
                )
        return True

    async def run(self) -> None:
        """Main human agent loop."""
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)

        # In sequential mode, request floor first
        if self._floor_policy == "sequential":
            await self.request_floor("Starting conversation")
        elif self._floor_policy == "free_for_all":
            self._has_floor = True

        # Run input reader and message receiver concurrently
        input_task = asyncio.create_task(self._input_loop())
        receive_task = asyncio.create_task(self._receive_loop())

        try:
            await asyncio.gather(input_task, receive_task, return_exceptions=True)
        finally:
            self._running = False

    async def _input_loop(self) -> None:
        """Read user input and send as OFP envelopes."""
        while self._running and not self._stop_event.is_set():
            if self._renderer:
                self._renderer.show_input_prompt(self._name)

            text = await self._read_input()
            if text is None:
                self._stop_event.set()
                break

            text = text.strip()
            if not text:
                continue

            if text.startswith("/"):
                should_continue = await self._handle_command(text)
                if not should_continue:
                    break
                continue

            # Send utterance
            envelope = self._make_utterance_envelope(text)
            await self.send_envelope(envelope)

            # In sequential and round_robin mode, yield after speaking
            if self._floor_policy in ("sequential", "round_robin") and self._has_floor:
                self._has_floor = False
                await self.yield_floor()

    async def _receive_loop(self) -> None:
        """Process incoming messages from the bus."""
        while self._running and not self._stop_event.is_set():
            try:
                envelope = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                await self._handle_incoming(envelope)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("Human agent receive error: %s", e, exc_info=True)
