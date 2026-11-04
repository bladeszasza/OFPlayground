"""Artifact tools for orchestrator and worker agents.

Provides ``read_artifact`` and ``list_artifacts`` tool definitions so agents
can retrieve specific phase outputs on demand rather than having the full
manuscript injected into every prompt.

Tool definitions follow the same model-agnostic Anthropic-format pattern as
``memory/tools.py`` and ``agents/llm/spawn_tools.py``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ofp_playground.memory.artifact_store import ArtifactStore


def build_artifact_tools() -> list[dict]:
    """Return Anthropic-format tool definitions for artifact operations.

    Use ``to_openai_tools()`` / ``to_google_tools()`` from spawn_tools.py
    to convert for other provider APIs.
    """
    return [
        {
            "name": "read_artifact",
            "description": (
                "Retrieve the full content of a specific phase artifact by name or number. "
                "Use this to access prior phase outputs (e.g. geometry code, asset manifests, "
                "texture specs) without needing the full text in your conversation context. "
                "The PHASE ARTIFACTS index in your system prompt lists all available artifacts."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Artifact identifier: phase number (e.g. '3'), slug (e.g. 'geometry-code'), "
                            "filename (e.g. '03_geometry-code.md'), or substring match on slug/agent name."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "list_artifacts",
            "description": (
                "List all completed phase artifacts with their phase number, slug, agent, "
                "and summary.  Same as the PHASE ARTIFACTS index in the system prompt — "
                "use this tool only if you need an updated list after new phases complete."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    ]


def execute_artifact_tool(
    tool_name: str,
    args: dict,
    artifact_store: ArtifactStore,
) -> str:
    """Execute an artifact tool call and return a plain-text result.

    Args:
        tool_name: ``"read_artifact"`` or ``"list_artifacts"``
        args: Tool argument dict from the LLM response
        artifact_store: The ArtifactStore instance to query

    Returns:
        Tool result string suitable for injection into conversation context.
    """
    if tool_name == "read_artifact":
        query = args.get("query", "")
        if not query:
            return "read_artifact: missing required 'query' argument."
        content = artifact_store.read(query)
        if content is None:
            available = [a.slug for a in artifact_store.all_artifacts()]
            return (
                f"No artifact found matching '{query}'. "
                f"Available: {available or '(none yet)'}"
            )
        art = artifact_store._resolve(query)
        header = f"--- Phase {art.phase_num}: {art.slug} ({art.agent_name}) ---\n" if art else ""
        return header + content

    if tool_name == "list_artifacts":
        index = artifact_store.get_index()
        return index if index else "No phase artifacts saved yet."

    return f"Unknown artifact tool: {tool_name!r}"
