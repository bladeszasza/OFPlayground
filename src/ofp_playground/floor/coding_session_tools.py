"""Coding session tool definitions.

Two kinds of tools live here:

1. **Orchestrator tool** — ``create_coding_session`` lets an orchestrator
   spin up a multi-agent round-robin coding session (analogous to
   ``create_breakout_session``).

2. **File-system tools** — ``list_workspace``, ``read_file``, ``write_file``,
   ``edit_file``, ``update_todo`` — injected into each coding-session agent
   so they can collaboratively build a project in a shared sandbox directory.

Security: all file operations are sandboxed — paths are resolved relative to
the sandbox root and any attempt to escape (``..``, absolute paths, symlinks
pointing outside) is rejected.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ofp_playground.config.settings import Settings

from ofp_playground.floor.policy import FloorPolicy

# ── Orchestrator-facing tool: create_coding_session ─────────────────────────

_CODING_POLICIES = [
    p.value for p in FloorPolicy if p != FloorPolicy.SHOWRUNNER_DRIVEN
]


def build_coding_session_tool(settings: Settings) -> list[dict]:
    """Return Anthropic-format tool definition for ``create_coding_session``.

    Intended to be appended to the orchestrator's tool list alongside
    breakout and spawn tools.
    """
    has_anthropic = bool(settings.get_anthropic_key())
    has_openai = bool(settings.get_openai_key())
    has_google = bool(settings.get_google_key())
    has_hf = bool(settings.get_huggingface_key())

    providers = [
        p for p, ok in [
            ("anthropic", has_anthropic),
            ("openai", has_openai),
            ("google", has_google),
            ("hf", has_hf),
        ]
        if ok
    ]
    if not providers:
        return []

    return [
        {
            "name": "create_coding_session",
            "description": (
                "Launch a collaborative coding session — a round-robin sub-floor "
                "where 1+ coding agents take turns reading, writing, and editing "
                "files in a shared local workspace to build a project together. "
                "Each agent can see all files created by previous agents and "
                "continue the work. The session runs independently; when it "
                "completes (max_rounds reached or an agent signals "
                "[CODING_COMPLETE]) a project summary with all created files is "
                "returned to you. Use for: multi-file code generation, iterative "
                "development with review/fix cycles, collaborative Three.js or "
                "web projects. IMPORTANT: Coding sessions cannot nest."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "maxLength": 500,
                        "description": (
                            "Short objective for the coding session. Keep this to "
                            "1-3 sentences and under 500 characters. Do NOT paste "
                            "full phase outputs or long specs here — put detailed "
                            "context in artifact_refs and context_files so coding "
                            "agents can read the mirrored markdown files directly."
                        ),
                    },
                    "artifact_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional artifact slugs, phase numbers, or aliases for "
                            "accepted phase outputs that coding agents must read "
                            "from mirrored files in phases/. Example: ['geom-builder', "
                            "'game-architect']."
                        ),
                    },
                    "context_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional sandbox-relative file paths that coding agents "
                            "must read before editing. Use this for mirrored memory "
                            "notes or existing workspace files such as "
                            "memory/browser-sandbox-delivery.md or textures_data.js."
                        ),
                    },
                    "todo_items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional initial TODO items visible to all coding-session "
                            "agents from their first turn. If max_rounds is omitted, "
                            "the session auto-derives it as todo_items x agents "
                            "(clamped to 2-50)."
                        ),
                    },
                    "policy": {
                        "type": "string",
                        "enum": _CODING_POLICIES,
                        "description": (
                            "Floor policy. round_robin recommended for coding sessions."
                        ),
                    },
                    "max_rounds": {
                        "type": "integer",
                        "description": (
                            "Maximum number of agent turns before the session "
                            "auto-stops. Default 16. Range 2-50. If omitted and "
                            "todo_items are provided, rounds auto-derive from "
                            "todo_items x agents."
                        ),
                    },
                    "agents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "Display name for this coding agent",
                                },
                                "system": {
                                    "type": "string",
                                    "description": (
                                        "System prompt / persona (e.g. "
                                        "'@development/threejs-developer')"
                                    ),
                                },
                                "provider": {
                                    "type": "string",
                                    "enum": providers,
                                    "description": "LLM provider to use",
                                },
                                "model": {
                                    "type": "string",
                                    "description": "Optional model override",
                                },
                                "timeout": {
                                    "type": "number",
                                    "description": (
                                        "Optional per-call timeout in seconds for this "
                                        "coding-session agent. Defaults to 1200 seconds."
                                    ),
                                },
                            },
                            "required": ["name", "system", "provider"],
                        },
                        "minItems": 1,
                        "description": (
                            "List of 1+ agents to participate in the coding session. "
                            "Each agent gets file-system tools (read, write, edit, "
                            "list) for the shared workspace."
                        ),
                    },
                },
                "required": ["topic", "policy", "agents"],
            },
        },
    ]


def tool_use_to_coding_session_directive(args: dict) -> str:
    """Convert a ``create_coding_session`` tool call to a directive string.

    Format::

        [CODING_SESSION policy=<p> max_rounds=<n> topic=<t>]
        [CODING_CONTEXT artifact=<slug-or-phase>]
        [CODING_CONTEXT file=<sandbox-relative-path>]
        [CODING_AGENT -provider <p> -name <n> -system <s> [-model <m>] [-timeout <seconds>]]
        ...
    """
    topic = str(args.get("topic", "") or "Coding session")
    topic = " ".join(topic.replace("[", "(").replace("]", ")").split())
    policy = args.get("policy", "round_robin")
    agents = args.get("agents", [])
    todo_items = [
        " ".join(str(item).replace("[", "(").replace("]", ")").split())
        for item in args.get("todo_items", [])
        if str(item).strip()
    ]
    artifact_refs = [
        str(ref).strip()
        for ref in args.get("artifact_refs", [])
        if str(ref).strip()
    ]
    context_files = [
        str(path).strip()
        for path in args.get("context_files", [])
        if str(path).strip()
    ]
    if args.get("max_rounds") is None and todo_items and agents:
        max_rounds = min(max(len(todo_items) * len(agents), 2), 50)
    else:
        max_rounds = args.get("max_rounds", 16)

    lines = [f"[CODING_SESSION policy={policy} max_rounds={max_rounds} topic={topic}]"]
    lines.extend(f"[CODING_CONTEXT artifact={ref}]" for ref in artifact_refs)
    lines.extend(f"[CODING_CONTEXT file={path}]" for path in context_files)
    lines.extend(f"[CODING_TODO {item}]" for item in todo_items)
    for agent in agents:
        name = agent.get("name", "Coder")
        system = agent.get("system", "")
        provider = agent.get("provider", "openai")
        model_part = f" -model {agent['model']}" if agent.get("model") else ""
        timeout_part = f" -timeout {agent['timeout']}" if agent.get("timeout") is not None else ""
        lines.append(
            f"[CODING_AGENT -provider {provider} -name {name} -system {system}{model_part}{timeout_part}]"
        )
    return "\n".join(lines)


# ── Agent-facing file-system tools ──────────────────────────────────────────

def build_file_tools() -> list[dict]:
    """Return Anthropic-format tool definitions for sandbox file I/O.

    These are injected into each agent participating in a coding session.
    """
    return [
        {
            "name": "list_workspace",
            "description": (
                "List files and directories in the shared project workspace. "
                "Returns a tree-style listing with file sizes. Use this first "
                "to see what exists before reading or writing."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path within the workspace to list. "
                            "Omit or use '.' for the workspace root."
                        ),
                    },
                },
            },
        },
        {
            "name": "read_file",
            "description": (
                "Read the full contents of a file from the shared workspace. "
                "Use list_workspace first to discover available files."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the file to read.",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": (
                "Create or overwrite a file in the shared workspace. Parent "
                "directories are created automatically. Use this to create new "
                "files or completely replace existing ones."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative path for the file (e.g. 'js/main.js', "
                            "'index.html', 'css/style.css')."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "edit_file",
            "description": (
                "Edit an existing file by replacing a specific text segment. "
                "The old_text must match exactly (including whitespace). Only "
                "the first occurrence is replaced. Use read_file first to see "
                "the current contents."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the file to edit.",
                    },
                    "old_text": {
                        "type": "string",
                        "description": (
                            "Exact text to find and replace (must match precisely, "
                            "including whitespace and newlines)."
                        ),
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
        {
            "name": "update_todo",
            "description": (
                "Manage the shared project TODO list. Mark items as done "
                "and/or add new items. The TODO list is visible to all agents "
                "at the start of each turn."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "done_items": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Indices (0-based) of TODO items to mark as done."
                        ),
                    },
                    "add_items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New TODO items to add to the list.",
                    },
                },
            },
        },
    ]


# ── Sandbox path validation ────────────────────────────────────────────────

def _safe_resolve(sandbox_dir: Path, rel_path: str) -> Path:
    """Resolve *rel_path* inside *sandbox_dir*, rejecting escapes.

    Raises ``ValueError`` on:
    - absolute paths
    - ``..`` components that would escape the sandbox
    - symlinks whose target is outside the sandbox
    """
    if os.path.isabs(rel_path):
        raise ValueError(f"Absolute paths are not allowed: {rel_path}")

    # Normalise and reject explicit parent traversal
    normed = os.path.normpath(rel_path)
    if normed.startswith("..") or "/.." in normed or normed == "..":
        raise ValueError(f"Path traversal not allowed: {rel_path}")

    resolved = (sandbox_dir / normed).resolve()
    sandbox_resolved = sandbox_dir.resolve()

    # Final containment check (catches tricky symlink chains)
    if not str(resolved).startswith(str(sandbox_resolved)):
        raise ValueError(f"Path escapes sandbox: {rel_path}")

    return resolved


# ── Tool executor ───────────────────────────────────────────────────────────

class TodoList:
    """Simple TODO list shared across all agents in a coding session."""

    def __init__(self, initial_items: list[str] | None = None):
        self._items: list[dict] = []
        if initial_items:
            for item in initial_items:
                self._items.append({"text": item, "done": False})

    def mark_done(self, indices: list[int]) -> None:
        for idx in indices:
            if 0 <= idx < len(self._items):
                self._items[idx]["done"] = True

    def add_items(self, items: list[str]) -> None:
        for item in items:
            self._items.append({"text": item, "done": False})

    def render(self) -> str:
        if not self._items:
            return "(no TODO items yet — use update_todo to add tasks)"
        lines = []
        for i, item in enumerate(self._items):
            mark = "x" if item["done"] else " "
            lines.append(f"  [{mark}] {i}. {item['text']}")
        done = sum(1 for it in self._items if it["done"])
        lines.append(f"\n  Progress: {done}/{len(self._items)} done")
        return "\n".join(lines)


def execute_coding_tool(
    name: str,
    args: dict,
    sandbox_dir: Path,
    todo: TodoList,
) -> str:
    """Execute a file-system or TODO tool and return a text result.

    All file operations are sandboxed to *sandbox_dir*.
    """
    try:
        if name == "list_workspace":
            return _exec_list_workspace(sandbox_dir, args.get("path", "."))

        if name == "read_file":
            path_str = args.get("path")
            if not path_str:
                return "Error: 'path' is required."
            return _exec_read_file(sandbox_dir, path_str)

        if name == "write_file":
            path_str = args.get("path")
            content = args.get("content", "")
            if not path_str:
                return "Error: 'path' is required."
            return _exec_write_file(sandbox_dir, path_str, content)

        if name == "edit_file":
            path_str = args.get("path")
            old_text = args.get("old_text", "")
            new_text = args.get("new_text", "")
            if not path_str:
                return "Error: 'path' is required."
            return _exec_edit_file(sandbox_dir, path_str, old_text, new_text)

        if name == "update_todo":
            done_indices = args.get("done_items", [])
            add_items = args.get("add_items", [])
            if done_indices:
                todo.mark_done(done_indices)
            if add_items:
                todo.add_items(add_items)
            return f"TODO updated.\n{todo.render()}"

        return f"Error: unknown tool '{name}'."

    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Operation failed at an application error boundary")
        return f"Error: {type(e).__name__}: {e}"


_FILE_TOOLS = {"list_workspace", "read_file", "write_file", "edit_file", "update_todo"}


def is_file_tool(name: str) -> bool:
    """Return True if *name* is one of the coding-session file tools."""
    return name in _FILE_TOOLS


# ── Individual tool implementations ─────────────────────────────────────────

def _exec_list_workspace(sandbox_dir: Path, rel_path: str) -> str:
    target = _safe_resolve(sandbox_dir, rel_path or ".")
    if not target.exists():
        return f"Directory not found: {rel_path}"
    if not target.is_dir():
        return f"Not a directory: {rel_path}"

    lines = [f"Workspace: {rel_path or '.'}"]
    _tree(target, sandbox_dir, lines, prefix="")
    if len(lines) == 1:
        lines.append("  (empty)")
    return "\n".join(lines)


def _tree(directory: Path, root: Path, lines: list[str], prefix: str, depth: int = 0) -> None:
    """Recursively build a tree listing."""
    if depth > 8:
        lines.append(f"{prefix}  ... (depth limit)")
        return
    entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    for entry in entries:
        rel = entry.relative_to(root.resolve())
        if entry.is_dir():
            lines.append(f"{prefix}  {rel}/")
            _tree(entry, root, lines, prefix + "  ", depth + 1)
        else:
            size = entry.stat().st_size
            lines.append(f"{prefix}  {rel}  ({_human_size(size)})")


def _human_size(nbytes: int) -> str:
    size = float(nbytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _exec_read_file(sandbox_dir: Path, rel_path: str) -> str:
    target = _safe_resolve(sandbox_dir, rel_path)
    if not target.exists():
        return f"File not found: {rel_path}"
    if target.is_dir():
        return f"'{rel_path}' is a directory, not a file. Use list_workspace instead."
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Cannot read '{rel_path}': binary file."
    if not content:
        return f"File '{rel_path}' is empty."
    return content


def _exec_write_file(sandbox_dir: Path, rel_path: str, content: str) -> str:
    target = _safe_resolve(sandbox_dir, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    target.write_text(content, encoding="utf-8")
    action = "Updated" if existed else "Created"
    return f"{action}: {rel_path} ({len(content)} chars)"


def _exec_edit_file(sandbox_dir: Path, rel_path: str, old_text: str, new_text: str) -> str:
    target = _safe_resolve(sandbox_dir, rel_path)
    if not target.exists():
        return f"File not found: {rel_path}"
    content = target.read_text(encoding="utf-8")
    count = content.count(old_text)
    if count == 0:
        # Show a snippet to help the agent fix the match
        preview = content[:500]
        return f"old_text not found in '{rel_path}'. File starts with:\n{preview}"
    if count > 1:
        return (
            f"old_text found {count} times in '{rel_path}'. "
            f"Please use a more specific match (include surrounding context)."
        )
    new_content = content.replace(old_text, new_text, 1)
    target.write_text(new_content, encoding="utf-8")
    return f"Edited: {rel_path} (replaced {len(old_text)} chars with {len(new_text)} chars)"
