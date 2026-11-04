"""Human agent: bridges terminal stdin/stdout with OFP envelopes."""
from __future__ import annotations

import asyncio
import codecs
import io
import logging
import os
import stat
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus

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
        renderer: TerminalRenderer | None = None,
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
        self._command_handlers: dict[str, Callable[[str], Awaitable[None]]] = {}
        self._input_callback: Callable[[], None] | None = None
        self._stop_event = asyncio.Event()
        self._stdin_buffer = ""
        self._stdin_eof = False
        self._stdin_decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")(errors="replace")

    def register_command(self, command: str, handler) -> None:
        self._command_handlers[command] = handler

    async def _handle_incoming(self, envelope) -> None:
        """Process incoming envelope from the bus."""
        sender_uri = self._get_sender_uri(envelope)

        for event in (envelope.events or []):
            event_type = getattr(event, "eventType", type(event).__name__)

            if event_type == "utterance":
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

    async def _read_input(self) -> str | None:
        """Read terminal input without leaving a blocked executor thread on stop."""
        loop = asyncio.get_event_loop()
        try:
            if not sys.stdin.isatty():
                return await self._read_redirected_input()
            elif sys.platform == "win32":
                import msvcrt

                chars: list[str] = []
                while True:
                    if not msvcrt.kbhit():
                        await asyncio.sleep(0.03)
                        continue
                    char = msvcrt.getwch()
                    if char == "\x03":
                        raise asyncio.CancelledError
                    if char == "\x1a":  # Ctrl+Z / EOF
                        return None
                    if char in ("\r", "\n"):
                        sys.stdout.write("\n")
                        return "".join(chars)
                    if char == "\b":
                        if chars:
                            chars.pop()
                            sys.stdout.write("\b \b")
                    elif char in ("\x00", "\xe0"):
                        msvcrt.getwch()  # ignore the second byte of an extended key
                    else:
                        chars.append(char)
                        sys.stdout.write(char)
                    sys.stdout.flush()
            else:
                ready = loop.create_future()
                fd = sys.stdin.fileno()

                def readable(ready=ready):
                    if not ready.done():
                        ready.set_result(sys.stdin.readline())

                loop.add_reader(fd, readable)
                try:
                    line = await ready
                finally:
                    loop.remove_reader(fd)
                return line.rstrip("\n") if line else None
        except (EOFError, KeyboardInterrupt):
            return None

    async def _read_redirected_input(self) -> str | None:
        """Read pipe chunks without blocking the event loop on a partial line."""
        if isinstance(sys.stdin, io.StringIO):
            line = sys.stdin.readline()
            return line.rstrip("\r\n") if line else None
        fd = sys.stdin.fileno()
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
        while "\n" not in self._stdin_buffer and not self._stdin_eof:
            count = 4096
            if not regular and sys.platform == "win32":
                import ctypes
                import msvcrt
                from ctypes import wintypes

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                peek = kernel.PeekNamedPipe
                peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                 wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
                peek.restype = wintypes.BOOL
                available = wintypes.DWORD()
                if not peek(msvcrt.get_osfhandle(fd), None, 0, None, ctypes.byref(available), None):
                    error = ctypes.get_last_error()
                    if error in (109, 232):  # broken / closed pipe
                        self._stdin_eof = True
                        break
                    raise OSError(error, "Cannot read redirected stdin")
                if not available.value:
                    await asyncio.sleep(0.03)
                    continue
                count = min(count, available.value)
            elif not regular:
                loop = asyncio.get_running_loop()
                ready = loop.create_future()

                def readable(ready=ready):
                    if not ready.done():
                        ready.set_result(None)

                loop.add_reader(fd, readable)
                try:
                    await ready
                finally:
                    loop.remove_reader(fd)
            chunk = os.read(fd, count)
            self._stdin_eof = not chunk
            self._stdin_buffer += self._stdin_decoder.decode(chunk, final=self._stdin_eof)
            if len(self._stdin_buffer) > 1024 * 1024:
                raise ValueError("Input line exceeds 1 MiB")
            await asyncio.sleep(0)
        if not self._stdin_buffer and self._stdin_eof:
            return None
        line, separator, rest = self._stdin_buffer.partition("\n")
        self._stdin_buffer = rest if separator else ""
        return line.rstrip("\r")

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
            done, _ = await asyncio.wait(
                [input_task, receive_task], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        finally:
            self._running = False
            for task in (input_task, receive_task):
                task.cancel()
            await asyncio.gather(input_task, receive_task, return_exceptions=True)

    async def _input_loop(self) -> None:
        """Read user input and send as OFP envelopes."""
        while self._running and not self._stop_event.is_set():
            if self._renderer:
                self._renderer.show_input_prompt(self._name)

            task_id = self._status.begin_work("awaiting-input") if self._status else None
            if self._status and task_id:
                self._status.transition("task", "input-required", task_id=task_id, reason="input-needed")
                self._status.transition("lifecycle", "idle")
            try:
                text = await self._read_input()
            except asyncio.CancelledError:
                if self._status and task_id:
                    self._status.finish_work(task_id, "canceled", "canceled")
                raise
            if self._status and task_id:
                self._status.finish_work(task_id, "completed" if text is not None else "canceled",
                                         "work-completed" if text is not None else "canceled")
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
            if self._input_callback:
                self._input_callback()
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
            except TimeoutError:
                continue
            except Exception:
                logger.exception('Human agent receive error: see traceback')
