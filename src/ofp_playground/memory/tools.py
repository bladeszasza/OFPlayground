"""Memory tool definitions for showrunner_driven orchestrators and worker agents.

Tool definitions follow the same model-agnostic pattern as spawn_tools.py:
- build_memory_tools() returns Anthropic-format canonical definitions
- The existing to_hf_tools(), to_openai_tools(), to_google_tools() converters
  from spawn_tools.py are format-generic and work on any Anthropic-format tool
  definition — no duplication needed here.
- execute_memory_tool() runs the operation on a MemoryStore and returns a
  plain-text result string that can be injected back into the LLM's context.
"""
from __future__ import annotations

import uuid
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ofp_playground.memory.store import MemoryStore


_CATEGORY_ENUM = [
    "decisions",
    "agent_profiles",
    "tasks",
    "lessons",
    "preferences",
    "goals",
]


def build_memory_tools() -> list[dict]:
    """Return Anthropic-format tool definitions for memory operations.

    Use to_hf_tools() / to_openai_tools() / to_google_tools() from
    spawn_tools.py to convert these for other provider APIs.
    """
    return [
        {
            "name": "store_memory",
            "description": (
                "Store a piece of information in the session memory. "
                "Use this to record key decisions, agent performance notes, task status, "
                "lessons learned, user preferences, or goal refinements. "
                "If a (category, key) pair already exists it will be updated in-place."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": _CATEGORY_ENUM,
                        "description": (
                            "Memory category — "
                            "decisions (key choices made), "
                            "agent_profiles (agent strengths/weaknesses/reliability), "
                            "tasks (task status and progress), "
                            "lessons (mistakes to avoid repeating), "
                            "preferences (style or topic preferences), "
                            "goals (mission objectives and refinements)"
                        ),
                    },
                    "key": {
                        "type": "string",
                        "description": (
                            "Short identifying label for this entry "
                            "(e.g. 'chapter_1_status', 'Writer_reliability', 'style_preference')"
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "The information to store. Be concise but complete.",
                    },
                },
                "required": ["category", "key", "content"],
            },
        },
        {
            "name": "recall_memory",
            "description": (
                "Retrieve stored memories by category and/or key. "
                "The full memory summary is already auto-injected into your system prompt "
                "each turn — use recall_memory only when you need to search for something "
                "specific that may not appear in the summary. "
                "Omit both parameters to retrieve all memories."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": _CATEGORY_ENUM,
                        "description": "Filter by category (optional)",
                    },
                    "key": {
                        "type": "string",
                        "description": "Filter by key substring, case-insensitive (optional)",
                    },
                },
                "required": [],
            },
        },
    ]


def execute_memory_tool(
    tool_name: str,
    args: dict,
    memory_store: "MemoryStore",
    author: str = "agent",
) -> str:
    """Execute a memory tool call and return a plain-text result.

    The result is suitable for injecting back into the LLM's conversation
    context (e.g. as a tool-result message or appended to pending context).

    Args:
        tool_name: "store_memory" or "recall_memory"
        args: Tool argument dict from the LLM response
        memory_store: The MemoryStore instance to operate on
        author: Name of the calling agent (tracked for provenance)

    Returns:
        Human-readable result string.
    """
    if tool_name == "store_memory":
        category = args.get("category", "decisions")
        key = args.get("key", "")
        content = args.get("content", "")
        if not key or not content:
            return "store_memory: missing required 'key' or 'content' argument."
        entry_id = memory_store.store(category, key, content, author=author)
        preview = content[:80] + ("..." if len(content) > 80 else "")
        return f"Stored [{category}] {key!r}: {preview} (id={entry_id})"

    if tool_name == "recall_memory":
        category = args.get("category")
        key = args.get("key")
        entries = memory_store.recall(category=category, key=key)
        if not entries:
            filter_desc = ""
            if category:
                filter_desc += f" category={category}"
            if key:
                filter_desc += f" key~='{key}'"
            return f"No memory entries found{filter_desc or ' (memory is empty)'}."
        lines = [f"[{e.category.value}] {e.key}: {e.content}" for e in entries]
        return "\n".join(lines)

    return f"Unknown memory tool: {tool_name!r}"


def parse_remember_directives(text: str, memory_store: "MemoryStore", author: str) -> str:
    """Parse and execute [REMEMBER category]: content directives from agent text output.

    This provides a text-directive fallback so worker agents without tool-calling
    support can still write to the memory store.  The [REMEMBER] lines are stripped
    from the returned text so they don't pollute the manuscript.

    Format (both are accepted):
        [REMEMBER category]: content
        [REMEMBER category:key]: content

    Args:
        text: Raw text output from an agent
        memory_store: Target MemoryStore
        author: Name of the agent that produced the text

    Returns:
        Cleaned text with all [REMEMBER ...] lines removed.
    """
    if "[REMEMBER" not in text.upper():
        return text

    pattern = re.compile(
        r"^\[REMEMBER\s+(\w+)(?::([^\]]+))?\]\s*:\s*(.+)$",
        re.IGNORECASE,
    )
    lines = text.splitlines()
    clean_lines: list[str] = []

    for line in lines:
        m = pattern.match(line.strip())
        if m:
            category_str = m.group(1).lower()
            key = (m.group(2) or "").strip() or f"{author.lower().replace(' ', '_')}_{uuid.uuid4().hex[:6]}"
            content = m.group(3).strip()
            try:
                memory_store.store(category_str, key, content, author=author)
            except (ValueError, KeyError):
                # Silently ignore invalid category strings
                pass
        else:
            clean_lines.append(line)

    return "\n".join(clean_lines).strip()
