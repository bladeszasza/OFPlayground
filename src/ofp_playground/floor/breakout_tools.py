"""Breakout session tool definitions for orchestrator agents.

Follows the same model-agnostic pattern as spawn_tools.py:
- build_breakout_tools() returns Anthropic-format canonical definitions
- Existing to_hf_tools/to_openai_tools/to_google_tools converters work as-is
- tool_use_to_breakout_directive() converts a tool call into a text
  directive that the FloorManager parses and executes

The orchestrator calls ``create_breakout_session`` to spin up a
temporary sub-floor.  Only available providers appear as agent options.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ofp_playground.config.settings import Settings

from ofp_playground.floor.policy import FloorPolicy


# Policies available for breakout sessions (not showrunner_driven —
# that would require an orchestrator inside the breakout, which is
# one-level-only).
_BREAKOUT_POLICIES = [
    p.value for p in FloorPolicy if p != FloorPolicy.SHOWRUNNER_DRIVEN
]


def build_breakout_tools(settings: "Settings") -> list[dict]:
    """Return Anthropic-format tool definitions for breakout sessions.

    Use to_hf_tools() / to_openai_tools() / to_google_tools() from
    spawn_tools.py to convert these for other provider APIs.
    """
    has_anthropic = bool(settings.get_anthropic_key())
    has_openai    = bool(settings.get_openai_key())
    has_google    = bool(settings.get_google_key())
    has_hf        = bool(settings.get_huggingface_key())

    text_providers = [
        p for p, ok in [
            ("anthropic", has_anthropic),
            ("openai", has_openai),
            ("google", has_google),
            ("hf", has_hf),
        ] if ok
    ]

    if not text_providers:
        return []

    # Build the policy descriptions for the tool schema
    policy_descriptions = []
    for p in FloorPolicy:
        if p == FloorPolicy.SHOWRUNNER_DRIVEN:
            continue
        policy_descriptions.append(f"{p.value}: {p.description}")

    return [
        {
            "name": "create_breakout_session",
            "description": (
                "Create a breakout session — a temporary sub-floor where 2+ agents "
                "discuss a specific topic under their own floor policy. The session "
                "runs independently; when it completes (max_rounds reached or agents "
                "signal [BREAKOUT_COMPLETE]) a summary is returned to you. "
                "Use breakouts for: focused deliberation on a sub-problem, "
                "brainstorming without polluting the main manuscript, peer review "
                "between specialists, or any side-discussion that benefits from "
                "multiple perspectives. "
                "IMPORTANT: Breakouts cannot nest — they run one level deep only."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": (
                            "The discussion topic or question for the breakout. "
                            "Be specific — this is seeded as the opening message."
                        ),
                    },
                    "policy": {
                        "type": "string",
                        "enum": _BREAKOUT_POLICIES,
                        "description": (
                            "Floor policy for the breakout session. "
                            + " | ".join(policy_descriptions)
                        ),
                    },
                    "max_rounds": {
                        "type": "integer",
                        "description": (
                            "Maximum number of utterances before the session "
                            "auto-stops. Default 6. Range 2-20."
                        ),
                    },
                    "agents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "Display name for this agent",
                                },
                                "system": {
                                    "type": "string",
                                    "description": "System prompt defining agent's perspective",
                                },
                                "provider": {
                                    "type": "string",
                                    "enum": text_providers,
                                    "description": "LLM provider to use",
                                },
                                "model": {
                                    "type": "string",
                                    "description": "Optional model override",
                                },
                            },
                            "required": ["name", "system", "provider"],
                        },
                        "minItems": 2,
                        "description": (
                            "List of 2+ agents to participate.  Each agent is "
                            "spawned fresh for the breakout.  You may also "
                            "reference existing agents by name (set provider "
                            "to match their current provider)."
                        ),
                    },
                },
                "required": ["topic", "policy", "agents"],
            },
        },
    ]


def tool_use_to_breakout_directive(args: dict) -> str:
    """Convert a create_breakout_session tool call to a directive string.

    The FloorManager parses this and executes the breakout.

    Format:
        [BREAKOUT policy=<policy> max_rounds=<n> topic=<topic>]
        [BREAKOUT_AGENT -provider <p> -name <n> -system <s> [-model <m>]]
        ...
    """
    topic = args.get("topic", "")
    policy = args.get("policy", "round_robin")
    max_rounds = args.get("max_rounds", 6)
    agents = args.get("agents", [])

    lines = [f"[BREAKOUT policy={policy} max_rounds={max_rounds} topic={topic}]"]
    for agent in agents:
        name = agent.get("name", "Agent")
        system = agent.get("system", "")
        provider = agent.get("provider", "hf")
        model_part = f" -model {agent['model']}" if agent.get("model") else ""
        lines.append(f"[BREAKOUT_AGENT -provider {provider} -name {name} -system {system}{model_part}]")

    return "\n".join(lines)
