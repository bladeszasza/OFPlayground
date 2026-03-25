"""Registry-driven spawn tool definitions for all orchestrator types.

Tools are built dynamically from configured API keys — the LLM only sees
spawn options for providers that are actually available.

Provider-specific format converters allow the same tool definitions to be
used with Anthropic, OpenAI, Google, and HuggingFace APIs.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ofp_playground.config.settings import Settings


def build_spawn_tools(settings: "Settings") -> list[dict]:
    """Return Anthropic-format tool definitions for available agent types.

    This is the canonical format; use to_*_tools() helpers to convert
    for other providers.
    """
    has_anthropic = bool(settings.get_anthropic_key())
    has_openai    = bool(settings.get_openai_key())
    has_google    = bool(settings.get_google_key())
    has_hf        = bool(settings.get_huggingface_key())

    tools: list[dict] = []

    # spawn_text_agent — all text providers
    text_providers = [
        p for p, ok in [
            ("anthropic", has_anthropic),
            ("openai", has_openai),
            ("google", has_google),
            ("hf", has_hf),
        ] if ok
    ]
    if text_providers:
        tools.append({
            "name": "spawn_text_agent",
            "description": (
                "Spawn a text generation agent (writer, analyst, editor, narrator, etc.). "
                "The agent joins immediately and receives initial_task as its first assignment."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Display name for this agent (e.g. Writer, Analyst)",
                    },
                    "system": {
                        "type": "string",
                        "description": "System prompt defining this agent's role and writing style",
                    },
                    "initial_task": {
                        "type": "string",
                        "description": "Concrete first task to assign the moment the agent joins",
                    },
                    "provider": {
                        "type": "string",
                        "enum": text_providers,
                        "description": "LLM provider to use",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model override (omit to use provider default)",
                    },
                },
                "required": ["name", "system", "initial_task", "provider"],
            },
        })

    # spawn_image_agent — openai, google, hf
    image_providers = [
        p for p, ok in [
            ("openai", has_openai),
            ("google", has_google),
            ("hf", has_hf),
        ] if ok
    ]
    if image_providers:
        tools.append({
            "name": "spawn_image_agent",
            "description": (
                "Spawn a text-to-image generation agent. "
                "The agent produces image files from text prompts."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Display name (e.g. Painter, Illustrator)",
                    },
                    "style": {
                        "type": "string",
                        "description": "Art style / system prompt (e.g. 'oil painting, warm tones')",
                    },
                    "initial_task": {
                        "type": "string",
                        "description": "First image to generate — a concrete visual scene description",
                    },
                    "provider": {
                        "type": "string",
                        "enum": image_providers,
                        "description": "Image generation provider",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model override",
                    },
                },
                "required": ["name", "style", "initial_task", "provider"],
            },
        })

    # spawn_video_agent — hf only
    if has_hf:
        tools.append({
            "name": "spawn_video_agent",
            "description": "Spawn a text-to-video generation agent (HuggingFace only).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Display name (e.g. Filmmaker)"},
                    "style": {"type": "string", "description": "Visual style / system prompt"},
                    "initial_task": {"type": "string", "description": "First video to generate"},
                    "model": {"type": "string", "description": "Optional model override"},
                },
                "required": ["name", "style", "initial_task"],
            },
        })

    # spawn_music_agent — google only
    if has_google:
        tools.append({
            "name": "spawn_music_agent",
            "description": "Spawn a text-to-music generation agent (Google Lyria).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Display name (e.g. Composer)"},
                    "style": {"type": "string", "description": "Musical style / system prompt"},
                    "initial_task": {"type": "string", "description": "First piece of music to compose"},
                    "model": {"type": "string", "description": "Optional model override"},
                },
                "required": ["name", "style", "initial_task"],
            },
        })

    return tools


# ── Provider-specific format converters ──────────────────────────────────────

def to_hf_tools(spawn_tools: list[dict]) -> list[dict]:
    """Convert to HuggingFace/OpenAI chat completions tool format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in spawn_tools
    ]


def to_openai_tools(spawn_tools: list[dict]) -> list[dict]:
    """Convert to OpenAI Responses API tool format."""
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }
        for t in spawn_tools
    ]


def _json_schema_to_google_schema(schema: dict):
    """Recursively convert a JSON Schema dict to a google.genai types.Schema."""
    from google.genai import types

    type_map = {
        "object": types.Type.OBJECT,
        "string": types.Type.STRING,
        "number": types.Type.NUMBER,
        "integer": types.Type.INTEGER,
        "boolean": types.Type.BOOLEAN,
        "array": types.Type.ARRAY,
    }
    kwargs: dict = {}
    if "type" in schema:
        kwargs["type"] = type_map.get(schema["type"], types.Type.STRING)
    if "description" in schema:
        kwargs["description"] = schema["description"]
    if "enum" in schema:
        kwargs["enum"] = schema["enum"]
    if "properties" in schema:
        kwargs["properties"] = {
            k: _json_schema_to_google_schema(v)
            for k, v in schema["properties"].items()
        }
    if "required" in schema:
        kwargs["required"] = schema["required"]
    return types.Schema(**kwargs)


def to_google_tools(spawn_tools: list[dict]) -> list:
    """Convert to Google genai Tool format (list of types.Tool)."""
    from google.genai import types

    fdecls = [
        types.FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters=_json_schema_to_google_schema(t["input_schema"]),
        )
        for t in spawn_tools
    ]
    return [types.Tool(function_declarations=fdecls)]


# ── Directive converter ───────────────────────────────────────────────────────

def tool_use_to_directives(tool_name: str, args: dict) -> str:
    """Convert a tool call to '[SPAWN...]\n[ASSIGN...]' directive text.

    The result is fed back into the FloorManager's existing text-parsing pipeline,
    so no changes to FloorManager are needed.
    """
    name = args.get("name")
    if not name:
        return ""
    model_part = f" -model {args['model']}" if args.get("model") else ""
    initial_task = args.get("initial_task", "")

    if tool_name == "spawn_text_agent":
        provider = args.get("provider", "hf")
        system = args.get("system", "")
        spawn = f"[SPAWN -provider {provider} -name {name} -system {system}{model_part}]"
    elif tool_name == "spawn_image_agent":
        provider = args.get("provider", "hf")
        style = args.get("style", "")
        spawn = f"[SPAWN -provider {provider} -type text-to-image -name {name} -system {style}{model_part}]"
    elif tool_name == "spawn_video_agent":
        style = args.get("style", "")
        spawn = f"[SPAWN -provider hf -type text-to-video -name {name} -system {style}{model_part}]"
    elif tool_name == "spawn_music_agent":
        style = args.get("style", "")
        spawn = f"[SPAWN -provider google -type text-to-music -name {name} -system {style}{model_part}]"
    else:
        return ""

    assign = f"[ASSIGN {name}]: {initial_task}"
    return f"{spawn}\n{assign}"
