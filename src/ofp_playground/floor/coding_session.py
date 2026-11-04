"""Coding session — a collaborative multi-agent coding sub-floor.

A coding session is a self-contained round-robin conversation where multiple
LLM agents collaboratively build a project by reading, writing, and editing
files in a shared local sandbox directory.

Derived from the breakout session backbone but specialised for code:

- Agents get file-system tools (``list_workspace``, ``read_file``,
  ``write_file``, ``edit_file``, ``update_todo``) injected into every LLM
  call via a tool-calling proxy loop.
- The shared sandbox directory is visible to all agents — each agent can
  see and edit files created by previous agents.
- A workspace snapshot (file listing + TODO state) is injected into each
  agent's context at the start of their turn.
- Higher round limit (2–50, default 16) and longer timeout (1800 s).
- Results include the full file manifest + project structure.

Only one level of nesting is allowed — coding sessions cannot be nested.

Lifecycle:
    1. Orchestrator calls ``create_coding_session`` tool.
    2. FloorManager creates agents, wires file tools, starts session.
    3. Agents take turns: read workspace → write/edit files → yield.
    4. Session ends at max_rounds or ``[CODING_COMPLETE]``.
    5. Project manifest + files returned to orchestrator.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from ofp_playground.status.runtime import SessionStatusRuntime
    from ofp_playground.trace import EventCollector

from openfloor import (
    Conversation,
    DialogEvent,
    Envelope,
    GrantFloorEvent,
    Sender,
    TextFeature,
    Token,
    UtteranceEvent,
)

from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.floor.breakout import _await_child_floor
from ofp_playground.floor.coding_session_tools import (
    TodoList,
    build_file_tools,
    execute_coding_tool,
    is_file_tool,
)
from ofp_playground.floor.history import ConversationHistory
from ofp_playground.floor.policy import FloorController, FloorPolicy
from ofp_playground.models.artifact import Utterance

logger = logging.getLogger(__name__)

CODING_FM_URI = "tag:ofp-playground.local,2025:coding-session-manager"
DEFAULT_CODING_AGENT_TIMEOUT_SECONDS = 1200.0

_CODE_BLOCK_RE = re.compile(
    r"```(?:(\w+))?[ \t]*(?://\s*(?:file(?:name)?|path)\s*:\s*(\S+))?\n(.*?)```",
    re.DOTALL,
)

_EXT_MAP = {
    "python": ".py", "py": ".py",
    "javascript": ".js", "js": ".js",
    "typescript": ".ts", "ts": ".ts",
    "html": ".html",
    "css": ".css",
    "json": ".json",
    "yaml": ".yaml", "yml": ".yaml",
    "xml": ".xml",
    "sql": ".sql",
    "shell": ".sh", "bash": ".sh", "sh": ".sh",
    "markdown": ".md", "md": ".md",
    "jsx": ".jsx", "tsx": ".tsx",
    "glsl": ".glsl",
}


class CodingSessionResult(NamedTuple):
    """Return value from ``run_coding_session``."""
    history: list          # list[Utterance]
    topic: str
    agent_names: list      # list[str]
    round_count: int
    files: list            # list[Path] — all files in sandbox
    sandbox_dir: Path


def _render_required_context_files(required_context_files: tuple[str, ...]) -> str:
    """Render the sandbox-relative files that coding agents must read first."""
    if not required_context_files:
        return ""

    lines = [
        "=== REQUIRED CONTEXT FILES ===",
        "Read these sandbox-relative files with list_workspace/read_file before editing code:",
    ]
    lines.extend(f"- {path}" for path in required_context_files)
    lines.append("=== END REQUIRED CONTEXT FILES ===")
    return "\n".join(lines)


class CodingSessionManager:
    """Floor manager for a collaborative coding session.

    Differences from BreakoutFloorManager:
    - Agents get file-system tools injected into every LLM call.
    - A workspace snapshot is prepended to the topic context each turn.
    - Higher round limit (2–50) and longer per-queue timeout.
    - Tracks a shared TodoList visible to all agents.
    """

    def __init__(
        self,
        bus: MessageBus,
        policy: FloorPolicy,
        parent_conversation_id: str,
        sandbox_dir: Path,
        required_context_files: tuple[str, ...] = (),
        initial_todo_items: tuple[str, ...] = (),
        max_rounds: int = 16,
    ):
        self._bus = bus
        self._conversation_id = f"coding:{uuid.uuid4()}"
        self._parent_conversation_id = parent_conversation_id
        self._policy = FloorController(policy)
        self._history = ConversationHistory()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._agents: dict[str, str] = {}   # speakerUri -> name
        self._running = False
        self._max_rounds = max_rounds
        self._utterance_count = 0
        self._sandbox_dir = sandbox_dir
        self._required_context_files = tuple(
            dict.fromkeys(
                path.strip()
                for path in required_context_files
                if path and path.strip()
            )
        )
        self._todo = TodoList(list(dict.fromkeys(
            item.strip()
            for item in initial_todo_items
            if item and item.strip()
        )))
        self._contributors: set[str] = set()  # URIs of agents that have spoken
        self._status_callback: Callable[[str, str], None] | None = None
        self._active_holders: set[str] = set()

    def _report_floor(self, speaker_uri: str, state: str) -> None:
        if self._status_callback:
            self._status_callback(speaker_uri, state)

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def speaker_uri(self) -> str:
        return CODING_FM_URI

    @property
    def sandbox_dir(self) -> Path:
        return self._sandbox_dir

    @property
    def todo(self) -> TodoList:
        return self._todo

    @property
    def required_context_files(self) -> tuple[str, ...]:
        return self._required_context_files

    def register_agent(self, speaker_uri: str, name: str) -> None:
        self._agents[speaker_uri] = name
        self._policy.add_to_rotation(speaker_uri)

    def _make_sender(self) -> Sender:
        return Sender(speakerUri=self.speaker_uri, serviceUrl="local://coding-session")

    def _make_conversation(self) -> Conversation:
        return Conversation(id=self._conversation_id)

    async def _send(self, envelope: Envelope) -> None:
        await self._bus.send(envelope)

    async def _grant_floor(self, speaker_uri: str) -> None:
        self._active_holders.add(speaker_uri)
        from openfloor import To

        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[
                GrantFloorEvent(
                    to=To(speakerUri=speaker_uri),
                    reason="Your turn in coding session",
                )
            ],
        )
        await self._send(envelope)
        self._report_floor(speaker_uri, "has-floor")

    async def run_to_completion(self, topic: str) -> list[Utterance]:
        """Run the coding session and return conversation history."""
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)

        # Seed the topic (includes workspace snapshot)
        snapshot = _build_workspace_snapshot(self._sandbox_dir, self._todo)
        context_note = _render_required_context_files(self._required_context_files)
        topic_body = f"{topic}\n\n{context_note}" if context_note else topic
        augmented_topic = f"{topic_body}\n\n{snapshot}"
        await self._seed_topic(augmented_topic)

        # Grant floor to the first agent in the rotation
        if self._policy._round_robin_order:
            first_uri = self._policy._round_robin_order[0]
            self._policy._current_holder = first_uri
            await self._grant_floor(first_uri)

        try:
            while self._running:
                try:
                    envelope = await asyncio.wait_for(self._queue.get(), timeout=180.0)
                    await self._process_envelope(envelope)
                except TimeoutError:
                    logger.warning("Coding session queue timed out")
                    break
                except Exception:
                    logger.exception('Coding session error: see traceback')
        finally:
            self._running = False
            await self._bus.unregister(self.speaker_uri)

        return self._history.all()

    async def _seed_topic(self, topic: str) -> None:
        de = DialogEvent(
            speakerUri=self.speaker_uri,
            id=str(uuid.uuid4()),
            features={"text": TextFeature(tokens=[Token(value=topic)])},
        )
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[UtteranceEvent(dialogEvent=de)],
        )
        await self._send(envelope)

    async def _process_envelope(self, envelope: Envelope) -> None:
        sender_uri = envelope.sender.speakerUri if envelope.sender else "unknown"

        for event in (envelope.events or []):
            event_type = (
                event.eventType if hasattr(event, "eventType") else type(event).__name__
            )
            if event_type == "utterance":
                await self._handle_utterance(envelope, event)
            elif event_type == "requestFloor":
                await self._handle_request_floor(sender_uri)
            elif event_type == "yieldFloor":
                await self._handle_yield_floor(sender_uri)

    async def _handle_utterance(self, envelope: Envelope, event) -> None:
        sender_uri = envelope.sender.speakerUri if envelope.sender else "unknown"
        sender_name = self._agents.get(sender_uri, sender_uri.split(":")[-1])

        text = ""
        de = getattr(event, "dialogEvent", None)
        if de and de.features:
            text_feat = de.features.get("text")
            if text_feat and text_feat.tokens:
                text = " ".join(t.value for t in text_feat.tokens if t.value)

        if not text:
            return

        utterance = Utterance.from_text(sender_uri, sender_name, text)
        self._history.add(utterance)

        if sender_uri != self.speaker_uri:
            self._utterance_count += 1
            self._contributors.add(sender_uri)

            if "[CODING_COMPLETE]" in text:
                # Only honor if ALL agents have contributed at least once
                if self._contributors >= set(self._agents.keys()):
                    self._running = False
                    return
                logger.info(
                    "Ignoring [CODING_COMPLETE] — not all agents have "
                    "contributed yet (%d/%d)",
                    len(self._contributors), len(self._agents),
                )

            if self._utterance_count >= self._max_rounds:
                self._running = False
                return

            # Turn advancement is handled by _handle_yield_floor when the
            # agent sends its yieldFloor event — do NOT advance here to
            # avoid double-granting.

    async def _handle_request_floor(self, sender_uri: str) -> None:
        self._report_floor(sender_uri, "requesting-floor")
        granted = self._policy.request_floor(sender_uri)
        if granted:
            await self._grant_floor(sender_uri)

    async def _handle_yield_floor(self, sender_uri: str) -> None:
        if sender_uri not in self._active_holders:
            return
        if self._policy.policy != FloorPolicy.FREE_FOR_ALL and self._policy.current_holder != sender_uri:
            return
        self._active_holders.discard(sender_uri)
        self._report_floor(sender_uri, "yielded")
        next_uri = self._policy.yield_floor(sender_uri)
        if next_uri:
            await self._grant_floor(next_uri)


# ── Workspace snapshot ──────────────────────────────────────────────────────

def _build_workspace_snapshot(sandbox_dir: Path, todo: TodoList) -> str:
    """Build a text snapshot of the workspace for agent context injection."""
    lines = ["=== WORKSPACE STATUS ==="]

    # File listing
    lines.append("\nFiles:")
    if sandbox_dir.exists():
        files = sorted(sandbox_dir.rglob("*"))
        files = [f for f in files if f.is_file()]
        if files:
            for f in files:
                rel = f.relative_to(sandbox_dir)
                size = f.stat().st_size
                lines.append(f"  {rel}  ({size} bytes)")
        else:
            lines.append("  (no files yet — start creating the project!)")
    else:
        lines.append("  (workspace not yet created)")

    # TODO list
    lines.append(f"\nTODO:\n{todo.render()}")
    lines.append("=== END WORKSPACE STATUS ===")

    return "\n".join(lines)


# ── Fallback code block extraction ──────────────────────────────────────────

def _extract_and_save_code_blocks(text: str, sandbox_dir: Path, agent_name: str) -> list[Path]:
    """Parse fenced code blocks from agent text and save as files.

    Supports filename hints in the fence header:
        ```js // filename: js/main.js
        ```html // path: index.html

    Falls back to auto-naming if no filename hint is present.
    """
    saved: list[Path] = []
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"\W+", "_", agent_name.lower())[:20]

    for i, m in enumerate(_CODE_BLOCK_RE.finditer(text)):
        lang = (m.group(1) or "").lower().strip()
        filename_hint = m.group(2)
        code = m.group(3).strip()

        if not code:
            continue

        if filename_hint:
            # Use the filename hint as-is (relative to sandbox)
            target = sandbox_dir / filename_hint
        else:
            # Auto-name based on language
            ext = _EXT_MAP.get(lang, ".txt")
            suffix = f"_{i + 1}" if i > 0 else ""
            target = sandbox_dir / f"{ts}_{slug}{suffix}{ext}"

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        saved.append(target)

    return saved


# ── Per-provider tool-calling loops ────────────────────────────────────────

async def _call_anthropic_with_tools(
    agent, system: str, messages: list, tools: list, sandbox_dir: Path, todo: TodoList,
) -> str:
    """Run Anthropic tool-calling loop: generate → execute tools → repeat."""
    import asyncio

    loop = asyncio.get_event_loop()
    client = agent._get_client()
    max_tool_rounds = 25  # safety limit

    for _ in range(max_tool_rounds):
        kwargs = {
            "model": agent._model,
            "max_tokens": max(agent._max_tokens, 16384),
            "system": system,
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "auto"},
        }

        response = await loop.run_in_executor(None, partial(client.messages.create, **kwargs))

        final_text = ""
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                final_text = block.text.strip()
            elif block.type == "tool_use" and is_file_tool(block.name):
                tool_calls.append(block)

        if not tool_calls:
            return final_text

        # Execute tool calls and build result messages
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                if is_file_tool(block.name):
                    result = execute_coding_tool(block.name, block.input, sandbox_dir, todo)
                else:
                    result = f"Unknown tool: {block.name}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})

    return final_text


async def _call_openai_with_tools(
    agent, system: str, history: list, tools: list, sandbox_dir: Path, todo: TodoList,
) -> str:
    """Run OpenAI Responses API tool-calling loop."""
    import asyncio

    loop = asyncio.get_event_loop()
    client = agent._get_client()
    max_tool_rounds = 25

    input_items = list(history)

    for _ in range(max_tool_rounds):
        kwargs = {
            "model": agent._model,
            "instructions": system,
            "input": input_items,
            "max_output_tokens": max(agent._max_tokens, 16384),
            "tools": tools,
            "tool_choice": "auto",
        }

        response = await loop.run_in_executor(None, partial(client.responses.create, **kwargs))

        final_text = ""
        tool_calls = []

        for item in response.output:
            if item.type == "function_call" and is_file_tool(item.name):
                tool_calls.append(item)
            elif item.type == "message":
                for content_block in item.content:
                    if hasattr(content_block, "text") and content_block.text:
                        final_text = content_block.text.strip()

        if not tool_calls:
            return final_text

        # Execute tool calls and feed results back
        for item in tool_calls:
            args = json.loads(item.arguments)
            result = execute_coding_tool(item.name, args, sandbox_dir, todo)
            input_items.append({
                "type": "function_call",
                "call_id": item.call_id,
                "name": item.name,
                "arguments": item.arguments,
            })
            input_items.append({
                "type": "function_call_output",
                "call_id": item.call_id,
                "output": result,
            })

    return final_text


async def _call_google_with_tools(
    agent, system: str, history: list, tools: list, sandbox_dir: Path, todo: TodoList,
) -> str:
    """Run Google Gemini tool-calling loop."""
    import asyncio

    from google import genai
    from google.genai import types

    loop = asyncio.get_event_loop()
    max_tool_rounds = 25

    contents = [
        types.Content(
            parts=[types.Part(text=msg["content"])],
            role="model" if msg["role"] == "assistant" else "user",
        )
        for msg in history
    ]

    for _ in range(max_tool_rounds):
        config_kwargs = {
            "system_instruction": system,
            "max_output_tokens": max(agent._max_tokens, 16384),
            "tools": tools,
        }
        config = types.GenerateContentConfig(**config_kwargs)

        def _call(cts, cfg):
            c = genai.Client(api_key=agent._api_key)
            return c.models.generate_content(model=agent._model, contents=cts, config=cfg)

        response = await loop.run_in_executor(None, partial(_call, contents, config))

        final_text = ""
        tool_calls = []

        for candidate in (response.candidates or []):
            for part in (candidate.content.parts or []):
                if hasattr(part, "function_call") and part.function_call:
                    if is_file_tool(part.function_call.name):
                        tool_calls.append(part.function_call)
                elif hasattr(part, "text") and part.text:
                    final_text = part.text.strip()

        if not tool_calls:
            return final_text

        # Build function response parts
        # First, add the model's response to contents
        model_parts: list[types.Part] = []
        for candidate in (response.candidates or []):
            model_parts.extend(candidate.content.parts or [])
        contents.append(types.Content(parts=model_parts, role="model"))

        # Then add tool results
        func_responses = []
        for fc in tool_calls:
            args = dict(fc.args)
            result = execute_coding_tool(fc.name, args, sandbox_dir, todo)
            func_responses.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=fc.name,
                        response={"result": result},
                    )
                )
            )
        contents.append(types.Content(parts=func_responses, role="user"))

    return final_text


# ── Agent turn runner with tool injection ──────────────────────────────────

def _detect_provider(agent) -> str:
    """Detect which LLM provider an agent uses based on its class."""
    cls_name = type(agent).__name__.lower()
    if "anthropic" in cls_name or "claude" in cls_name:
        return "anthropic"
    if "openai" in cls_name or "gpt" in cls_name:
        return "openai"
    if "google" in cls_name or "gemini" in cls_name:
        return "google"
    return "unknown"


async def _coding_agent_generate(
    agent,
    sandbox_dir: Path,
    todo: TodoList,
    workspace_snapshot: str,
    required_context_files: tuple[str, ...],
) -> str | None:
    """Generate a response with file tools injected.

    This replaces the agent's normal ``_generate_response()`` for the
    duration of the coding session, adding:
    - File-system tools to the LLM call
    - Workspace snapshot in the system prompt
    - Multi-round tool-calling loop
    """
    provider = _detect_provider(agent)
    file_tools = build_file_tools()
    required_context_note = _render_required_context_files(required_context_files)
    required_context_rule = (
        "- REQUIRED CONTEXT FILES are listed above. Read them before making code changes.\n"
        if required_context_files
        else ""
    )
    review_step = (
        "1. Review the workspace: list files, then read EVERY required context file listed above before editing code. "
        "After that, read any additional relevant phase docs in phases/ and notes in memory/*.md.\n"
        if required_context_files
        else "1. Review the workspace: list files, then read relevant phase docs in phases/ and notes in memory/*.md before editing code.\n"
    )

    # Build augmented system prompt
    base_system = agent._build_system_prompt([])
    context_prefix = f"{base_system}\n\n{workspace_snapshot}\n\n"
    if required_context_note:
        context_prefix += f"{required_context_note}\n\n"

    augmented_system = context_prefix + (
        "=== COLLABORATIVE CODING SESSION ===\n"
        "You are part of a coding TEAM. Multiple developers are taking turns "
        "building this project together in a shared workspace.\n\n"
        "HOW THIS WORKS:\n"
        "- You and your teammates take turns in round-robin order.\n"
        "- Everyone reads and writes files in the SAME workspace.\n"
        "- Check WORKSPACE STATUS above to see what files exist and what's been done.\n"
        f"{required_context_rule}"
        "- Read mirrored phase briefs from phases/ and relevant guidance from memory/*.md when present.\n"
        "- Read the conversation history to understand your teammates' progress.\n\n"
        "YOUR TURN:\n"
        f"{review_step}"
        "2. Pick ONE specific task or area to work on — do NOT rewrite everything.\n"
        "3. Use the file tools (write_file, edit_file, read_file) to implement your part.\n"
        "4. End with a brief summary: what you did, what files you changed, "
        "and what remains for your teammates.\n\n"
        "RULES:\n"
        "- Build ON TOP of existing files — don't overwrite teammates' work.\n"
        "- If a file exists, use edit_file to modify specific sections, "
        "or write new files that integrate with existing code.\n"
        "- Focus on quality over quantity — one well-implemented feature per turn.\n"
        "- If the task must run directly from file://, avoid local ES module graphs, "
        "importmaps, and bare specifiers like from 'three'; prefer index.html + a "
        "single local main.js (or inline remote-only module code).\n"
        "- For browser sandboxes, keep the renderer canvas inside the viewport "
        "(append it to a fixed app root or use position:fixed) so DOM overlays do not "
        "push the 3D view off-screen.\n"
        "- Only include [CODING_COMPLETE] when the project is polished and "
        "ALL planned features are implemented after multiple rounds of collaboration.\n"
        "=== END SESSION INSTRUCTIONS ==="
    )

    messages = list(agent._conversation_history[-30:])
    if not messages:
        messages = [{"role": "user", "content": "Begin coding."}]

    try:
        if provider == "anthropic":
            return await _call_anthropic_with_tools(
                agent, augmented_system, messages, file_tools,
                sandbox_dir, todo,
            )

        if provider == "openai":
            from ofp_playground.agents.llm.spawn_tools import to_openai_tools

            openai_tools = to_openai_tools(file_tools)
            return await _call_openai_with_tools(
                agent, augmented_system, messages, openai_tools,
                sandbox_dir, todo,
            )

        if provider == "google":
            from ofp_playground.agents.llm.spawn_tools import to_google_tools

            google_tools = to_google_tools(file_tools)
            return await _call_google_with_tools(
                agent, augmented_system, messages, google_tools,
                sandbox_dir, todo,
            )

        # Unknown provider: fall back to normal generation (no tools)
        logger.warning("Unknown provider '%s' for coding session, no tools", provider)
        return await agent._generate_response([])

    except Exception:
        logger.exception('[%s] Coding session generation error: see traceback', agent._name)
        raise


# ── Main entry point ────────────────────────────────────────────────────────

async def _run_coding_agent(
    agent,
    sandbox_dir: Path,
    todo: TodoList,
    required_context_files: tuple[str, ...],
) -> None:
    """Run an agent's event loop for a coding session.

    Unlike the standard agent loop, this intercepts ``grantFloor`` events
    and replaces ``_generate_response`` with the tool-augmented version.
    Utterance events are added to context directly — we bypass the normal
    ``_dispatch`` to avoid triggering ``request_floor()`` which would
    interfere with the session manager's round-robin control.
    """
    try:
        while True:
            try:
                envelope = await asyncio.wait_for(agent._queue.get(), timeout=2.0)
            except TimeoutError:
                continue

            sender_uri = (
                envelope.sender.speakerUri if envelope.sender else "unknown"
            )

            # Classify events in this envelope
            is_grant = False
            for event in (envelope.events or []):
                event_type = (
                    event.eventType
                    if hasattr(event, "eventType")
                    else type(event).__name__
                )
                if event_type == "grantFloor":
                    is_grant = True
                    break

            if is_grant:
                await _handle_coding_grant(
                    agent,
                    sandbox_dir,
                    todo,
                    required_context_files,
                )
            elif sender_uri != agent.speaker_uri:
                # Utterance from a peer — add to context so this agent
                # can see what teammates did, but do NOT call _dispatch()
                # (which would trigger request_floor and break round-robin).
                text = agent._extract_text_from_envelope(envelope)
                if text:
                    sender_name = (
                        agent._name_registry.get(sender_uri)
                        or sender_uri.split(":")[-1]
                    )
                    agent._append_to_context(sender_name, text, is_self=False)

    except asyncio.CancelledError:
        return


async def _handle_coding_grant(
    agent,
    sandbox_dir: Path,
    todo: TodoList,
    required_context_files: tuple[str, ...],
) -> None:
    """Handle a floor grant in a coding session — generate with tools."""
    agent._has_floor = True
    try:
        snapshot = _build_workspace_snapshot(sandbox_dir, todo)
        response_text = await agent._call_with_retry(
            lambda: _coding_agent_generate(
                agent,
                sandbox_dir,
                todo,
                snapshot,
                required_context_files,
            )
        )

        if response_text:
            # Fallback: extract code blocks from text response and save them
            _extract_and_save_code_blocks(response_text, sandbox_dir, agent._name)

            agent._append_to_context(agent._name, response_text, is_self=True)
            envelope = agent._make_utterance_envelope(response_text)
            await agent.send_envelope(envelope)
            agent._consecutive_errors = 0
    except TimeoutError:
        agent._consecutive_errors += 1
        logger.warning("[%s] coding response timed out", agent._name)
    except Exception:
        agent._consecutive_errors += 1
        logger.exception('[%s] coding error: see traceback', agent._name)
    finally:
        agent._has_floor = False
        await agent.yield_floor()


async def run_coding_session(
    topic: str,
    agents: list,
    policy: FloorPolicy,
    parent_conversation_id: str,
    sandbox_dir: Path,
    max_rounds: int = 16,
    required_context_files: list[str] | None = None,
    initial_todo_items: list[str] | None = None,
    parent_renderer=None,
    trace_collector: EventCollector | None = None,
    status_runtime: SessionStatusRuntime | None = None,
) -> CodingSessionResult:
    """Run a full coding session and return a CodingSessionResult.

    This is the main entry point called by the FloorManager when the
    orchestrator requests a coding session via tool calling.

    Args:
        topic: Project description / requirements for the coding session.
        agents: List of fresh agent instances (BaseLLMAgent subclasses).
        policy: Floor policy (``ROUND_ROBIN`` recommended).
        parent_conversation_id: ID of the parent conversation.
        sandbox_dir: Shared workspace directory for file I/O.
        max_rounds: Maximum agent turns before auto-stop (2–50).
        required_context_files: Sandbox-relative files that agents must read first.
        initial_todo_items: Initial shared TODO items visible from the first turn.
        parent_renderer: Optional renderer for status messages.
        trace_collector: Optional event collector for tracing.

    Returns:
        CodingSessionResult with history, files, and sandbox path.
    """
    if len(agents) < 1:
        return CodingSessionResult(
            history=[],
            topic=topic,
            agent_names=[],
            round_count=0,
            files=[],
            sandbox_dir=sandbox_dir,
        )

    sandbox_dir.mkdir(parents=True, exist_ok=True)

    # Create isolated bus and session manager
    coding_bus = MessageBus()
    session_mgr = CodingSessionManager(
        bus=coding_bus,
        policy=policy,
        parent_conversation_id=parent_conversation_id,
        sandbox_dir=sandbox_dir,
        required_context_files=tuple(required_context_files or ()),
        initial_todo_items=tuple(initial_todo_items or ()),
        max_rounds=max_rounds,
    )

    if trace_collector is not None:
        coding_bus.set_collector(
            trace_collector,
            breakout_id=session_mgr.conversation_id,
            scope_id=session_mgr.conversation_id,
            scope_kind="coding",
            parent_conversation_id=parent_conversation_id,
        )

    if parent_renderer:
        agent_labels = ", ".join(a.name for a in agents)
        parent_renderer.show_system_event(
            f"[CodingSession] Starting: {topic[:60]} | policy={policy.value} | "
            f"agents=[{agent_labels}] | max_rounds={max_rounds} | "
            f"context_files={len(session_mgr.required_context_files)} | sandbox={sandbox_dir}"
        )

    # Wire agents to the coding bus
    for agent in agents:
        agent._bus = coding_bus
        agent._conversation_id = session_mgr.conversation_id
        agent._queue = asyncio.Queue()
        agent._conversation_history = []
        agent._pending_context = []
        agent._has_floor = False
        agent._pending_floor_request = False
        if getattr(agent, "_timeout", None) is None:
            agent._timeout = DEFAULT_CODING_AGENT_TIMEOUT_SECONDS
        session_mgr.register_agent(agent.speaker_uri, agent.name)
        await coding_bus.register(agent.speaker_uri, agent._queue)

    # Give agents name registry
    name_registry = dict(session_mgr._agents)
    for agent in agents:
        if hasattr(agent, "set_name_registry"):
            agent.set_name_registry(name_registry)

    # Start agent loops with tool injection
    child_status = status_runtime.child(session_mgr.conversation_id) if status_runtime else None
    if child_status:
        session_mgr._status_callback = child_status.floor_transition
        for agent in agents:
            child_status.track(agent)
    agent_tasks = [
        asyncio.create_task(
            child_status.run_agent(agent, partial(_run_coding_agent, agent, session_mgr.sandbox_dir,
                                                  session_mgr.todo, session_mgr.required_context_files))
            if child_status else _run_coding_agent(agent, session_mgr.sandbox_dir,
                                                   session_mgr.todo, session_mgr.required_context_files)
        )
        for agent in agents
    ]

    try:
        history = await _await_child_floor(
            session_mgr.run_to_completion(topic),
            agent_tasks, timeout=1800.0,  # 30-minute hard timeout
        )
    except TimeoutError:
        logger.warning("Coding session hard timeout after 1800s")
        history = session_mgr._history.all()
    finally:
        for t in agent_tasks:
            t.cancel()
        await asyncio.gather(*agent_tasks, return_exceptions=True)
        for agent in agents:
            await coding_bus.unregister(agent.speaker_uri)

    # Collect all files in sandbox
    files = sorted(sandbox_dir.rglob("*"))
    files = [f for f in files if f.is_file()]

    agent_names = [a.name for a in agents]
    round_count = sum(
        1 for u in history
        if "coding-session" not in u.speaker_uri and "floor-manager" not in u.speaker_uri
    )

    if parent_renderer:
        parent_renderer.show_system_event(
            f"[CodingSession] Completed: {topic[:60]} — "
            f"{round_count} rounds, {len(files)} files"
        )

    return CodingSessionResult(
        history=history,
        topic=topic,
        agent_names=agent_names,
        round_count=round_count,
        files=files,
        sandbox_dir=sandbox_dir,
    )


# ── Result persistence ──────────────────────────────────────────────────────

def save_coding_session_artifact(result: CodingSessionResult, output_dir: Path) -> Path:
    """Save coding session manifest to a structured Markdown file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w]+", "_", result.topic[:40]).strip("_").lower()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"{timestamp}_{slug}.md"

    lines = [
        f"# Coding Session: {result.topic[:80]}",
        f"Date: {datetime.now().isoformat(timespec='seconds')}",
        f"Agents: {', '.join(result.agent_names)} | Rounds: {result.round_count}",
        f"Sandbox: {result.sandbox_dir}",
        "",
        "## Project Files",
        "",
    ]

    for f in result.files:
        rel = f.relative_to(result.sandbox_dir)
        size = f.stat().st_size
        lines.append(f"- `{rel}` ({size} bytes)")

    lines.extend(["", "## Conversation Log", ""])

    for u in result.history:
        if "coding-session" not in u.speaker_uri and "floor-manager" not in u.speaker_uri:
            text = u.text[:500]
            if len(u.text) > 500:
                text += "..."
            lines.append(f"### {u.speaker_name}")
            lines.append(text)
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def build_coding_session_notification(
    result: CodingSessionResult,
    artifact_path: Path,
    session_num: int,
) -> str:
    """Build a compact notification for the parent orchestrator."""
    agent_str = ", ".join(result.agent_names)
    lines = [
        f"[CODING SESSION COMPLETE]: {result.topic[:80]}",
        f"{len(result.agent_names)} agents | {result.round_count} rounds | {agent_str}",
        f"Artifact: {artifact_path}",
        f"Sandbox: {result.sandbox_dir}",
        "",
        f"Files created ({len(result.files)}):",
    ]

    for f in result.files:
        rel = f.relative_to(result.sandbox_dir)
        size = f.stat().st_size
        lines.append(f"  {rel}  ({size} bytes)")

    return "\n".join(lines)
