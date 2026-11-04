"""CLI entry point for OFP Playground."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from dataclasses import asdict
from pathlib import Path

import click
import httpx
from rich.console import Console

from ofp_playground import __version__
from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.agents.human import HumanAgent
from ofp_playground.agents.registry import AgentRegistry
from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.config.settings import Settings
from ofp_playground.floor.manager import FloorManager
from ofp_playground.floor.policy import FloorPolicy
from ofp_playground.renderer.terminal import TerminalRenderer

logger = logging.getLogger(__name__)

# Noisy third-party loggers to silence even in verbose mode
_QUIET_LOGGERS = [
    "httpcore", "httpx", "urllib3", "asyncio",
    "anthropic._base_client", "openai._base_client",
    "google_genai", "google.auth",
]


def _configure_logging(verbose: bool) -> None:
    """Configure logging: ofp_playground at DEBUG when verbose, noisy libs always at WARNING."""
    logging.basicConfig(level=logging.WARNING)
    if verbose:
        logging.getLogger("ofp_playground").setLevel(logging.DEBUG)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

console = Console()


def _load_dotenv() -> None:
    """Load .env from cwd or project root (if present) without requiring python-dotenv."""
    for candidate in (Path.cwd() / ".env", Path(__file__).parent.parent.parent / ".env"):
        if candidate.exists():
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = value
            break


def _parse_policy(policy_str: str) -> FloorPolicy:
    try:
        return FloorPolicy(policy_str)
    except ValueError:
        valid = ", ".join(p.value for p in FloorPolicy)
        raise click.BadParameter(f"Invalid policy. Choose from: {valid}")


def _parse_agent_spec(spec: str) -> tuple[str, str, str, str | None, int | None, float | None, int | None]:
    """Parse agent spec in two supported formats:

    Colon format:  type:name[:description[:model]]
    Flag format:   -provider TYPE -name NAME [-system DESCRIPTION] [-model MODEL]
                   [-max-tokens N] [-timeout SECONDS] [-max-retries N]

    Examples:
        hf:Astronomer:You are a skeptical astronomer.:MiniMaxAI/MiniMax-M2.7
        -provider hf -name Astronomer -system You are a skeptical astronomer. -model MiniMaxAI/MiniMax-M2.7
        -provider hf -name FastTask -timeout 30 -max-retries 2

    Returns: (agent_type, name, description, model_override, max_tokens_override, timeout, max_retries)
    """
    import re

    spec = spec.strip()

    if spec.startswith("-"):
        # Flag-based format: find each -flag and collect its value up to the next -flag.
        # Ignore flag-like tokens embedded inside directive-style square-bracket blocks
        # so orchestrator mission text can include examples like [BREAKOUT_AGENT -provider ...].
        flag_re = re.compile(r"(?<!\w)-(provider|name|system|model|type|max-tokens|timeout|max-retries)\s+", re.IGNORECASE)
        bracket_spans: list[tuple[int, int]] = []
        bracket_depth = 0
        span_start: int | None = None
        for index, char in enumerate(spec):
            if char == "[":
                if bracket_depth == 0:
                    span_start = index
                bracket_depth += 1
            elif char == "]" and bracket_depth:
                bracket_depth -= 1
                if bracket_depth == 0 and span_start is not None:
                    bracket_spans.append((span_start, index + 1))
                    span_start = None

        def _inside_brackets(position: int) -> bool:
            return any(start <= position < end for start, end in bracket_spans)

        def _looks_like_flag_model_id(value: str) -> bool:
            if not value or len(value) > 256 or any(c.isspace() for c in value):
                return False
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$", value):
                return False
            return any(c.isdigit() for c in value) or any(c in value for c in "./-:")

        def _is_real_flag_match(match: re.Match[str]) -> bool:
            key = match.group(1).lower()
            if key != "model":
                return True
            rest = spec[match.end():].lstrip()
            first_token = rest.split(None, 1)[0] if rest else ""
            return _looks_like_flag_model_id(first_token)

        matches = [
            match for match in flag_re.finditer(spec)
            if not _inside_brackets(match.start()) and _is_real_flag_match(match)
        ]
        if not matches:
            raise click.BadParameter(f"Invalid flag-based agent spec: {spec}")

        flags: dict[str, str] = {}
        for i, m in enumerate(matches):
            key = m.group(1).lower()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(spec)
            flags[key] = spec[start:end].strip()

        provider = flags.get("provider", "").lower()
        # Normalize HF task type: "Text-to-Image" → "text-to-image"
        modality = flags.get("type", "text-generation").lower().replace(" ", "-")
        agent_type = f"{provider}:{modality}" if modality != "text-generation" else provider
        name = flags.get("name", "")
        description = flags.get("system", f"I am {name}, an AI assistant.")
        model_override = flags.get("model") or None
        max_tokens_raw = flags.get("max-tokens")
        max_tokens_override = int(max_tokens_raw) if max_tokens_raw and max_tokens_raw.isdigit() else None
        timeout_raw = flags.get("timeout")
        timeout_override = float(timeout_raw) if timeout_raw else None
        max_retries_raw = flags.get("max-retries")
        max_retries_override = int(max_retries_raw) if max_retries_raw and max_retries_raw.isdigit() else None

        if not provider:
            raise click.BadParameter(f"Missing -provider in agent spec: {spec}")
        if not name:
            raise click.BadParameter(f"Missing -name in agent spec: {spec}")
        description = _resolve_agent_slug(description)
        return agent_type, name, description, model_override, max_tokens_override, timeout_override, max_retries_override

    # Colon-separated format.
    # Supports both:
    #   type:name[:description[:model]]              e.g. openai:Artbot:painter
    #   type:subtype:name[:description[:model]]      e.g. openai:text-to-image:Artbot:painter
    TASK_SUBTYPES = {
        "text-to-image", "image-to-text", "text-to-video", "text-generation", "text-to-music",
        "image-text-to-text", "image-classification", "object-detection",
        "image-segmentation", "token-classification", "text-classification",
        "summarization", "showrunner", "orchestrator",
        "code-generation",
    }
    def _looks_like_model_id(s: str) -> bool:
        """Return True only if s looks like a model identifier (no whitespace, ≤256 chars)."""
        return bool(s) and len(s) <= 256 and not any(c in s for c in (" ", "\n", "\r", "\t"))

    def _split_rest(rest: str) -> tuple[str, str | None]:
        """Extract optional model from the END of a rest string.

        The model (if present) is the last colon-delimited token that looks like
        a model identifier (no whitespace, ≤256 chars).  Everything before it is
        the description.  This handles descriptions that themselves contain colons
        (e.g. multi-line system prompts with 'DESIGN: ...' inside).
        """
        idx = rest.rfind(":")
        if idx >= 0 and _looks_like_model_id(rest[idx + 1:]):
            return rest[:idx], rest[idx + 1:]
        return rest, None

    # Use minimal splits so that multi-colon descriptions are preserved intact.
    # Detect type:subtype:name:... format (subtype in TASK_SUBTYPES)
    head = spec.split(":", 3)  # at most 4 parts: [type, subtype-or-name, name-or-rest, rest]
    if len(head) < 2:
        raise click.BadParameter(
            f"Invalid agent spec: '{spec}'. "
            f"Use 'type:name[:description[:model]]' or "
            f"'type:subtype:name[:description[:model]]' or "
            f"'-provider TYPE -name NAME [-system DESC] [-model MODEL]'"
        )

    demo_mode = head[0].lower() == "demo" and head[1].lower() in {"normal", "rate-limit", "input-wait", "silent", "stalled"}
    if len(head) >= 3 and (head[1].lower() in TASK_SUBTYPES or demo_mode):
        # type:subtype:name[:rest]
        agent_type = f"{head[0]}:{head[1]}".lower()
        name = head[2]
        rest = head[3] if len(head) > 3 else ""
        if rest:
            description, model_override = _split_rest(rest)
        else:
            description = f"I am {name}, an AI assistant."
            model_override = None
    else:
        # type:name[:rest]
        agent_type = head[0].lower()
        name = head[1]
        rest = ":".join(head[2:]) if len(head) > 2 else ""
        if rest:
            description, model_override = _split_rest(rest)
        else:
            description = f"I am {name}, an AI assistant."
            model_override = None
    description = _resolve_agent_slug(description)
    return agent_type, name, description, model_override, None, None, None


def _build_canvas_config(
    policy: str | None,
    agents: tuple[str, ...],
    topic: str | None,
    no_human: bool,
    max_turns: int | None,
    human_name: str,
    remotes: tuple[str, ...] = (),
    show_floor_events: bool = False,
) -> dict:
    """Build a StartRequest-shaped dict from CLI flags for canvas pre-population.

    Carries every `start` option the canvas understands: policy, topic, turn
    limit, human, floor-event visibility, agents (with model / timeout /
    retries / max tokens) and remote agents.
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    floor_id = "floor-main"
    conv_id = "conv-main"

    nodes.append({
        "id": floor_id,
        "type": "FloorNode",
        "data": {
            "policy": (policy or "SEQUENTIAL").upper(),
            "topic": topic or "",
            "maxTurns": max_turns,
            "noHuman": no_human,
            "humanName": human_name,
            "showFloorEvents": show_floor_events,
        },
    })
    nodes.append({"id": conv_id, "type": "ConversationNode", "data": {}})
    edges.append({"source": floor_id, "target": conv_id})

    for spec in agents:
        agent_type, name, description, model_override, max_tokens, timeout, retries = (
            _parse_agent_spec(spec)
        )
        parts = agent_type.split(":", 1)
        provider = parts[0]
        subtype = parts[1] if len(parts) > 1 else ""
        agent_id = f"agent-{name}"
        nodes.append({
            "id": agent_id,
            "type": "AgentNode",
            "data": {
                "provider": provider,
                "name": name,
                "model": model_override or "",
                "systemPrompt": description,
                "slug": "",
                "agentType": subtype,
                "timeout": timeout,
                "maxRetries": retries,
                "maxTokens": max_tokens,
            },
        })
        edges.append({"source": floor_id, "target": agent_id})

    for target in remotes:
        display_name, _url = _resolve_remote(target)
        agent_id = f"remote-{display_name}"
        nodes.append({
            "id": agent_id,
            "type": "AgentNode",
            "data": {
                "provider": "remote",
                "name": display_name,
                "model": "",
                "systemPrompt": "",
                "slug": "",
                "agentType": "",
                "remoteTarget": target,
            },
        })
        edges.append({"source": floor_id, "target": agent_id})

    if not no_human:
        human_id = "human-main"
        nodes.append({
            "id": human_id,
            "type": "HumanNode",
            "data": {"humanName": human_name},
        })
        edges.append({"source": floor_id, "target": human_id})

    return {"nodes": nodes, "edges": edges}


def _resolve_agent_slug(description: str) -> str:
    """If description starts with '@', resolve it to the matching SOUL.md content.

    Examples::

        @creative/brand-designer   → reads agents/creative/brand-designer/SOUL.md
        @marketing/copywriter      → reads agents/marketing/copywriter/SOUL.md
    """
    if not description or not description.startswith("@"):
        return description
    import re

    try:
        from ofp_playground.agents.library import resolve_slug
        return resolve_slug(description)
    except ValueError as exc:
        # Some orchestrators append extra instructions directly after an
        # ``@category/agent`` slug (for example ``@development/threejs-developer.
        # Write files now``). Resolve the leading slug first, then append the
        # trailing instructions to the resolved SOUL content.
        match = re.match(r"^(@[\w-]+/[\w-]+)(.*)$", description, re.DOTALL)
        if match:
            slug_ref, suffix = match.groups()
            suffix = suffix.lstrip(" .:\n\t")
            try:
                from ofp_playground.agents.library import resolve_slug
                resolved = resolve_slug(slug_ref)
            except ValueError:
                raise click.BadParameter(str(exc)) from exc
            if suffix:
                return f"{resolved}\n\nAdditional instructions:\n{suffix}"
            return resolved
        raise click.BadParameter(str(exc)) from exc


def _resolve_remote(slug_or_url: str) -> tuple[str, str]:
    """Return (display_name, service_url) for a known agent slug or a raw URL."""
    from ofp_playground.agents.remote import KNOWN_REMOTE_AGENTS
    entry = KNOWN_REMOTE_AGENTS.get(slug_or_url.lower())
    if entry:
        return entry[0], entry[1]
    # Treat as a raw URL; derive a short display name from the host
    name = f"Remote-{slug_or_url.split('//')[-1].split('/')[0][:16]}"
    return name, slug_or_url


async def _connect_remote_agent(
    slug_or_url: str,
    floor: FloorManager,
    bus: MessageBus,
    registry: AgentRegistry,
    renderer: TerminalRenderer,
) -> BasePlaygroundAgent:
    """Resolve slug-or-URL, create RemoteOFPAgent, register and start it."""
    from ofp_playground.agents.remote import RemoteOFPAgent
    display_name, url = _resolve_remote(slug_or_url)
    if any(n.casefold() == display_name.casefold() for n in floor.active_agents.values()):
        raise ValueError(f"Agent {display_name!r} already exists")
    remote = RemoteOFPAgent(
        service_url=url,
        name=display_name,
        bus=bus,
        conversation_id=floor.conversation_id,
    )
    floor.register_agent(remote.speaker_uri, remote.name)
    registry.register(remote)
    # Pre-register queue so the invite (sent below) is delivered before run() starts.
    await bus.register(remote.speaker_uri, remote.queue)
    renderer.show_system_event(f"Connected {display_name} → {url}")
    # Send OFP invite — triggers manifest exchange and floor request inside the agent.
    await floor.invite_agent(remote.speaker_uri, url)
    registry.start(remote)
    return remote


async def _seed_topic(topic: str, floor: FloorManager, bus: MessageBus) -> None:
    """Inject a topic message from the floor manager (see ``session.seed_topic``)."""
    from ofp_playground.session import seed_topic

    await seed_topic(topic, floor, bus)


async def _run_session(
    policy: FloorPolicy,
    agent_specs: tuple[str, ...],
    remote_urls: tuple[str, ...],
    settings: Settings,
    verbose: bool,
    no_human: bool = False,
    topic: str | None = None,
    max_turns: int | None = None,
    human_name: str = "User",
    show_floor_events: bool = False,
) -> None:
    """Run the main conversation session."""
    if no_human and not agent_specs and not remote_urls:
        raise ValueError("Autonomous CLI sessions need at least one --agent or --remote")
    renderer = TerminalRenderer(console, show_floor_events=show_floor_events)
    bus = MessageBus()

    floor = FloorManager(bus, policy=policy, renderer=renderer)

    renderer.show_header(floor.conversation_id, policy.value, 0)

    registry = AgentRegistry()

    session = await _attach_floor_callbacks(floor, bus, registry, settings, renderer)

    tasks = []

    if not no_human:
        renderer.show_system_event("Type /help for commands, /quit to exit")
        human = HumanAgent(
            name=human_name,
            bus=bus,
            conversation_id=floor.conversation_id,
            renderer=renderer,
            floor_policy=policy.value,
        )
        floor.register_agent(human.speaker_uri, human.name)
        registry.register(human)
        human._input_callback = session.notify_input

        async def handle_agents(_args: str):
            renderer.show_agents_table(floor.active_agents, floor.floor_holder)

        async def handle_help(_args: str):
            renderer.show_help()
            renderer.show_system_event(
                "/add SPEC · /status [json] · /watch [seconds] · /remove NAME [graceful|force] · "
                "/quarantine NAME · /review NAME · /clear NAME · /personas [search] · /models"
            )

        async def handle_status(args: str):
            if session.status is None:
                renderer.show_system_event(session.status_problem or "ASP unavailable; status is unknown")
                return
            try:
                snapshot = await session.status.connection.snapshot()
            except (httpx.HTTPError, OSError, RuntimeError, ValueError):
                renderer.show_system_event("ASP observation lost; previous status must be treated as stale")
                return
            ids = {agent.instance_id for agent in session.status.agents.values()}
            snapshot["agents"] = [agent for agent in snapshot.get("agents", []) if agent["agentId"] in ids]
            if args.strip() == "json":
                click.echo(json.dumps(snapshot, ensure_ascii=True))
            else:
                _print_status(snapshot)

        async def handle_watch(args: str):
            seconds = min(60.0, max(1.0, float(args) if args.strip().replace(".", "", 1).isdigit() else 10.0))
            renderer.show_system_event(f"Watching ASP for {seconds:g}s. Ctrl+C stops the session.")
            deadline = asyncio.get_running_loop().time() + seconds
            while asyncio.get_running_loop().time() < deadline:
                await handle_status("")
                await asyncio.sleep(2)

        async def handle_remove(args: str):
            parts = args.strip().rsplit(" ", 1)
            mode = parts[-1] if len(parts) == 2 and parts[-1] in {"force", "graceful"} else "graceful"
            name = parts[0] if len(parts) == 2 and parts[-1] in {"force", "graceful"} else args.strip()
            agent = registry.by_name(name)
            if agent is None:
                renderer.show_system_event(f"Agent '{name}' not found. Use /agents to list participants.")
                return
            from ofp_playground.renderer.status_text import operation_text
            result = await session.remove(agent.speaker_uri, mode=mode)
            renderer.show_system_event(operation_text(result))

        async def governance(args: str, value: str):
            agent = registry.by_name(args.strip())
            if agent is None:
                agent = next((item for item in session.participants.values()
                              if item.name.casefold() == args.strip().casefold()), None)
            if agent is None:
                renderer.show_system_event("Agent not found. Use /agents to list participants.")
                return
            from ofp_playground.renderer.status_text import operation_text
            result = await session.set_governance(agent.speaker_uri, value)
            renderer.show_system_event(operation_text(result))

        async def handle_quarantine(args: str):
            await governance(args, "quarantined")

        async def handle_review(args: str):
            await governance(args, "under-review")

        async def handle_clear(args: str):
            await governance(args, "normal")

        async def handle_personas(args: str):
            _print_personas(args.strip(), False, False)

        async def handle_models(_args: str):
            _print_models(settings, False)

        async def handle_history(args: str):
            n = int(args.strip()) if args.strip().isdigit() else 10
            for e in floor.history.recent(n):
                media = e.primary_media
                renderer.show_utterance(
                    e.speaker_uri, e.speaker_name, e.text,
                    media_key=media.feature_key if media else None,
                    media_path=media.value_url if media else None,
                )

        async def handle_floor(_args: str):
            renderer.show_system_event(await session.floor_command("/floor") or "")

        async def handle_grant(args: str):
            # Moderator control (MODERATED chair, or an override in any policy).
            renderer.show_system_event(
                await session.floor_command(f"/grant {args}", human.speaker_uri) or ""
            )

        async def handle_pass(_args: str):
            renderer.show_system_event(
                await session.floor_command("/pass", human.speaker_uri) or ""
            )

        async def handle_connect(args: str):
            target = args.strip().split()[0] if args.strip() else ""
            if not target:
                renderer.show_system_event("Usage: /connect <slug-or-url>  (e.g. /connect polly)")
                return
            try:
                await _connect_remote_agent(target, floor, bus, registry, renderer)
            except Exception as e:
                logger.exception("Operation failed at an application error boundary")
                renderer.show_system_event(f"Failed to connect remote agent: {e}")

        async def handle_spawn(args: str):
            parts = args.split(maxsplit=3)
            if len(parts) < 2:
                renderer.show_system_event(
                    "Usage: /spawn <type> <name> [description] [model]\n"
                    "       /spawn remote <slug-or-url>  (e.g. /spawn remote polly)"
                )
                return
            agent_type = parts[0].lower()
            if agent_type == "remote":
                slug_or_url = parts[1]
                try:
                    await _connect_remote_agent(slug_or_url, floor, bus, registry, renderer)
                except Exception as e:
                    logger.exception("Operation failed at an application error boundary")
                    renderer.show_system_event(f"Failed to connect remote agent: {e}")
                return
            name = parts[1]
            description = parts[2] if len(parts) > 2 else f"I am {name}, an AI assistant."
            model_ov = parts[3] if len(parts) > 3 else None
            try:
                await _spawn_llm_agent(agent_type, name, description, floor, bus, registry, renderer, settings, model_ov)
            except (ValueError, click.ClickException) as exc:
                renderer.show_system_event(f"Agent was not added: {exc}")

        async def handle_add(args: str):
            try:
                at, name, desc, model, tokens, timeout, retries = _parse_agent_spec(args)
                await _spawn_llm_agent(at, name, desc, floor, bus, registry, renderer,
                                       settings, model, tokens, timeout, retries)
            except (ValueError, click.ClickException) as exc:
                renderer.show_system_event(f"Agent was not added: {exc}")

        async def handle_kick(args: str):
            name = args.strip()
            agent = registry.by_name(name)
            if agent:
                await session.remove(agent.speaker_uri)
                renderer.show_system_event(f"Removed {name} from the conversation")
            else:
                renderer.show_system_event(f"Agent '{name}' not found")

        human.register_command("agents", handle_agents)
        human.register_command("help", handle_help)
        human.register_command("history", handle_history)
        human.register_command("floor", handle_floor)
        human.register_command("grant", handle_grant)
        human.register_command("pass", handle_pass)
        human.register_command("connect", handle_connect)
        human.register_command("spawn", handle_spawn)
        human.register_command("add", handle_add)
        human.register_command("kick", handle_kick)
        human.register_command("remove", handle_remove)
        human.register_command("status", handle_status)
        human.register_command("watch", handle_watch)
        human.register_command("quarantine", handle_quarantine)
        human.register_command("review", handle_review)
        human.register_command("clear", handle_clear)
        human.register_command("personas", handle_personas)
        human.register_command("models", handle_models)
        registry.start(human)
    else:
        renderer.show_system_event("Running in autonomous mode — press Ctrl+C to stop")

    # Spawn pre-configured agents
    for spec in agent_specs:
        try:
            agent_type, name, description, model_ov, max_tokens_ov, timeout_ov, max_retries_ov = _parse_agent_spec(spec)
            await _spawn_llm_agent(agent_type, name, description, floor, bus, registry, renderer, settings, model_ov, max_tokens_ov, timeout_ov, max_retries_ov)
        except Exception as e:
            renderer.show_system_event(f"Failed to spawn agent: {e}")
            await session.close()
            raise ValueError(f"Could not start agent: {e}") from e

    # Connect remote agents
    for slug_or_url in remote_urls:
        try:
            await _connect_remote_agent(slug_or_url, floor, bus, registry, renderer)
        except Exception as e:
            renderer.show_system_event(f"Failed to connect to {slug_or_url}: {e}")
            await session.close()
            raise ValueError(f"Could not connect remote agent: {e}") from e

    # All participant run tasks are owned by the registry and joined at shutdown.

    # Same start-up and turn limit as the web UI and canvas (session.kickoff).
    floor.max_turns = max_turns
    from ofp_playground.session import kickoff
    tasks.append(kickoff(
        floor, bus, topic=topic, human=None if no_human else human,
        notify=renderer.show_system_event, seed=_seed_topic,
    ))

    try:
        await session.run(*tasks)
    finally:
        await session.close()
        renderer.show_system_event("Conversation ended. Goodbye!")


def _create_breakout_agent(spec_str: str, bus: MessageBus, conversation_id: str, settings: Settings):
    """Create a text-only LLM agent for a breakout session from a spec string.

    Spec format: -provider <p> -name <n> -system <s> [-model <m>] [-timeout <s>] [-max-retries <n>]

    Only text-generation agents are created — no image/video/orchestrator.
    The agent is NOT registered on any floor; the caller wires it.
    """
    import re

    provider_m = re.search(r"-provider\s+(\S+)", spec_str)
    name_m = re.search(r"-name\s+(\S+)", spec_str)
    system_m = re.search(r"-system\s+(.+?)(?:\s+-(?:model|max-tokens|timeout|max-retries)\s|\s*$)", spec_str)
    model_m = re.search(r"-model\s+(\S+)", spec_str)
    max_tokens_m = re.search(r"-max-tokens\s+(\d+)", spec_str)
    timeout_m = re.search(r"-timeout\s+(\S+)", spec_str)
    max_retries_m = re.search(r"-max-retries\s+(\d+)", spec_str)

    provider = provider_m.group(1) if provider_m else "hf"
    from ofp_playground.agents.llm.base import BaseLLMAgent
    agent: BaseLLMAgent | None
    name = name_m.group(1) if name_m else "BreakoutAgent"
    system = system_m.group(1).strip() if system_m else f"You are {name}, an AI assistant."
    model_ov = model_m.group(1) if model_m else None
    max_tokens_ov = int(max_tokens_m.group(1)) if max_tokens_m else None
    timeout_ov = float(timeout_m.group(1)) if timeout_m else None
    max_retries_ov = int(max_retries_m.group(1)) if max_retries_m else None

    agent = None
    if provider in ("anthropic", "claude"):
        api_key = settings.get_anthropic_key()
        if not api_key:
            raise ValueError(f"No Anthropic API key for breakout agent {name}")
        from ofp_playground.agents.llm.anthropic import AnthropicAgent
        agent = AnthropicAgent(
            name=name, synopsis=system, bus=bus, conversation_id=conversation_id,
            api_key=api_key, model=model_ov or settings.defaults.llm_model_anthropic,
        )

    elif provider in ("openai", "gpt"):
        api_key = settings.get_openai_key()
        if not api_key:
            raise ValueError(f"No OpenAI API key for breakout agent {name}")
        from ofp_playground.agents.llm.openai import OpenAIAgent
        agent = OpenAIAgent(
            name=name, synopsis=system, bus=bus, conversation_id=conversation_id,
            api_key=api_key, model=model_ov or settings.defaults.llm_model_openai,
        )

    elif provider in ("google", "gemini"):
        api_key = settings.get_google_key()
        if not api_key:
            raise ValueError(f"No Google API key for breakout agent {name}")
        from ofp_playground.agents.llm.google import GoogleAgent
        agent = GoogleAgent(
            name=name, synopsis=system, bus=bus, conversation_id=conversation_id,
            api_key=api_key, model=model_ov or settings.defaults.llm_model_google,
        )

    elif provider in ("hf", "huggingface"):
        api_key = settings.get_huggingface_key()
        if not api_key:
            raise ValueError(f"No HuggingFace API key for breakout agent {name}")
        from ofp_playground.agents.llm.huggingface import HuggingFaceAgent
        agent = HuggingFaceAgent(
            name=name, synopsis=system, bus=bus, conversation_id=conversation_id,
            api_key=api_key, model=model_ov or settings.defaults.llm_model_huggingface,
        )

    else:
        raise ValueError(f"Unknown provider '{provider}' for breakout agent {name}")

    if agent is not None and max_tokens_ov and hasattr(agent, "_max_tokens"):
        agent._max_tokens = max_tokens_ov
    if agent is not None and timeout_ov is not None and hasattr(agent, "_timeout"):
        agent._timeout = timeout_ov
    if agent is not None and max_retries_ov is not None and hasattr(agent, "_max_retries"):
        agent._max_retries = max_retries_ov
    return agent


async def _spawn_llm_agent(
    agent_type: str,
    name: str,
    description: str,
    floor: FloorManager,
    bus: MessageBus,
    registry: AgentRegistry,
    renderer: TerminalRenderer,
    settings: Settings,
    model_override: str | None = None,
    max_tokens_override: int | None = None,
    timeout_override: float | None = None,
    max_retries_override: int | None = None,
    web_renderer=None,
) -> BasePlaygroundAgent:
    """Spawn and register an LLM agent."""
    name = name.strip()
    if not name:
        raise ValueError("Agent name must not be empty")
    if any(n.casefold() == name.casefold() for n in floor.active_agents.values()):
        raise ValueError(f"Agent {name!r} already exists")
    description = _resolve_agent_slug(description)
    agent: BasePlaygroundAgent | None = None

    if agent_type == "demo" or agent_type.startswith("demo:"):
        from ofp_playground.agents.demo import DemoAgent

        agent = DemoAgent(name=name, synopsis=description, bus=bus, conversation_id=floor.conversation_id,
                          mode=agent_type.split(":", 1)[1] if ":" in agent_type else "normal")
    elif agent_type in ("anthropic", "claude") or agent_type.startswith(("anthropic:", "claude:")):
        api_key = settings.get_anthropic_key()
        if not api_key:
            raise ValueError("Configure the Anthropic API key before adding an agent")

        task = agent_type.split(":", 1)[1] if ":" in agent_type else "text-generation"

        if task == "text-generation":
            from ofp_playground.agents.llm.anthropic import AnthropicAgent
            agent = AnthropicAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.llm_model_anthropic,
                relevance_filter=settings.defaults.relevance_filter,
            )
        elif task == "image-to-text":
            from ofp_playground.agents.llm.anthropic_vision import AnthropicVisionAgent
            agent = AnthropicVisionAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.vision_model_anthropic,
            )
        elif task == "orchestrator":
            from ofp_playground.agents.llm.showrunner import AnthropicOrchestratorAgent
            agent = AnthropicOrchestratorAgent(
                name=name,
                mission=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.llm_model_anthropic,
                stop_callback=floor.stop,
                settings=settings,
            )
            floor.register_orchestrator(agent.speaker_uri)
        elif task == "code-generation":
            from ofp_playground.agents.llm.anthropic_coding import AnthropicCodingAgent
            agent = AnthropicCodingAgent(
                name=name, synopsis=description, bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or None,
            )
        else:
            renderer.show_system_event(
                f"Unknown Anthropic task: {task}. Use anthropic for text-generation, image-to-text, orchestrator, or code-generation."
            )
            raise ValueError(f"Unsupported agent type/task: {agent_type}")

    elif agent_type in ("openai", "gpt") or agent_type.startswith(("openai:", "gpt:")):
        api_key = settings.get_openai_key()
        if not api_key:
            raise ValueError("Configure the OpenAI API key before adding an agent")

        task = agent_type.split(":", 1)[1] if ":" in agent_type else "text-generation"

        if task == "text-generation":
            from ofp_playground.agents.llm.openai import OpenAIAgent
            agent = OpenAIAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.llm_model_openai,
                relevance_filter=settings.defaults.relevance_filter,
            )
        elif task == "text-to-image":
            from ofp_playground.agents.llm.openai_image import OpenAIImageAgent
            agent = OpenAIImageAgent(
                name=name,
                style=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.image_model_openai,
            )
        elif task == "image-to-text":
            from ofp_playground.agents.llm.openai_image import OpenAIVisionAgent
            agent = OpenAIVisionAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.vision_model_openai,
            )
        elif task == "text-to-video":
            from ofp_playground.agents.llm.openai_video import SoraVideoAgent
            agent = SoraVideoAgent(
                name=name,
                style=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.video_model_openai,
            )
        elif task == "orchestrator":
            from ofp_playground.agents.llm.showrunner import OpenAIOrchestratorAgent
            agent = OpenAIOrchestratorAgent(
                name=name,
                mission=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.llm_model_openai,
                stop_callback=floor.stop,
                settings=settings,
            )
            floor.register_orchestrator(agent.speaker_uri)
        elif task == "code-generation":
            from ofp_playground.agents.llm.codex import OpenAICodingAgent
            agent = OpenAICodingAgent(
                name=name, synopsis=description, bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or None,
            )
        else:
            renderer.show_system_event(
                f"Unknown OpenAI task: {task}. Use OpenAI for text-generation, text-to-image, text-to-video, image-to-text, orchestrator, or code-generation."
            )
            raise ValueError(f"Unsupported agent type/task: {agent_type}")

    elif agent_type in ("google", "gemini") or agent_type.startswith(("google:", "gemini:")):
        api_key = settings.get_google_key()
        if not api_key:
            raise ValueError("Configure the Google API key before adding an agent")

        task = agent_type.split(":", 1)[1] if ":" in agent_type else "text-generation"

        if task == "text-generation":
            from ofp_playground.agents.llm.google import GoogleAgent
            agent = GoogleAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.llm_model_google,
                relevance_filter=settings.defaults.relevance_filter,
            )
        elif task == "text-to-image":
            from ofp_playground.agents.llm.google_image import GeminiImageAgent
            agent = GeminiImageAgent(
                name=name,
                style=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.image_model_google,
            )
        elif task == "image-to-text":
            from ofp_playground.agents.llm.google_image import GeminiVisionAgent
            agent = GeminiVisionAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.vision_model_google,
            )
        elif task == "text-to-music":
            from ofp_playground.agents.llm.google_music import GeminiMusicAgent
            agent = GeminiMusicAgent(
                name=name,
                style=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.music_model_google,
            )
        elif task == "text-to-video":
            from ofp_playground.agents.llm.google_video import GeminiVideoAgent
            agent = GeminiVideoAgent(
                name=name,
                style=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.video_model_google,
            )
        elif task == "orchestrator":
            from ofp_playground.agents.llm.showrunner import GoogleOrchestratorAgent
            agent = GoogleOrchestratorAgent(
                name=name,
                mission=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.llm_model_google,
                stop_callback=floor.stop,
                settings=settings,
            )
            floor.register_orchestrator(agent.speaker_uri)
        elif task == "code-generation":
            from ofp_playground.agents.llm.google_coding import GoogleCodingAgent
            agent = GoogleCodingAgent(
                name=name, synopsis=description, bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or None,
            )
        else:
            renderer.show_system_event(
                f"Unknown Google task: {task}. Use google for text-generation, text-to-image, image-to-text, text-to-music, text-to-video, orchestrator, or code-generation."
            )
            raise ValueError(f"Unsupported agent type/task: {agent_type}")

    elif agent_type in ("huggingface", "hf") or agent_type.startswith(("huggingface:", "hf:")):
        api_key = settings.get_huggingface_key()
        if not api_key:
            raise ValueError("Configure the HuggingFace API key before adding an agent")

        # Extract task type from compound agent_type string (e.g. "hf:text-to-image")
        task = agent_type.split(":", 1)[1] if ":" in agent_type else "text-generation"

        if task == "text-to-image":
            from ofp_playground.agents.llm.image import DEFAULT_MODEL as DEFAULT_IMAGE_MODEL
            from ofp_playground.agents.llm.image import ImageAgent
            agent = ImageAgent(
                name=name,
                style=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_IMAGE_MODEL,
            )
        elif task == "text-to-video":
            from ofp_playground.agents.llm.video import DEFAULT_MODEL as DEFAULT_VIDEO_MODEL
            from ofp_playground.agents.llm.video import VideoAgent
            agent = VideoAgent(
                name=name,
                style=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_VIDEO_MODEL,
            )
        elif task == "image-text-to-text":
            from ofp_playground.agents.llm.multimodal import (
                DEFAULT_MODEL as DEFAULT_MULTIMODAL_MODEL,
            )
            from ofp_playground.agents.llm.multimodal import MultimodalAgent
            # Support "model@provider" syntax, e.g. "Qwen/Qwen3.5-9B@together"
            raw_model = model_override or DEFAULT_MULTIMODAL_MODEL
            if "@" in raw_model:
                mm_model, mm_provider = raw_model.rsplit("@", 1)
            else:
                mm_model, mm_provider = raw_model, None
            agent = MultimodalAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=mm_model,
                provider=mm_provider,
            )
        elif task == "image-classification":
            from ofp_playground.agents.llm.classifier import (
                DEFAULT_MODEL as DEFAULT_CLASSIFIER_MODEL,
            )
            from ofp_playground.agents.llm.classifier import ImageClassificationAgent
            agent = ImageClassificationAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_CLASSIFIER_MODEL,
            )
        elif task == "object-detection":
            from ofp_playground.agents.llm.detector import DEFAULT_MODEL as DEFAULT_DETECTOR_MODEL
            from ofp_playground.agents.llm.detector import ObjectDetectionAgent
            agent = ObjectDetectionAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_DETECTOR_MODEL,
            )
        elif task == "image-segmentation":
            from ofp_playground.agents.llm.segmenter import DEFAULT_MODEL as DEFAULT_SEGMENTER_MODEL
            from ofp_playground.agents.llm.segmenter import ImageSegmentationAgent
            agent = ImageSegmentationAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_SEGMENTER_MODEL,
            )
        elif task == "image-to-text":
            from ofp_playground.agents.llm.ocr import DEFAULT_MODEL as DEFAULT_OCR_MODEL
            from ofp_playground.agents.llm.ocr import OCRAgent
            agent = OCRAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_OCR_MODEL,
            )
        elif task == "text-classification":
            from ofp_playground.agents.llm.text_classifier import (
                DEFAULT_MODEL as DEFAULT_TEXTCLS_MODEL,
            )
            from ofp_playground.agents.llm.text_classifier import TextClassificationAgent
            agent = TextClassificationAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_TEXTCLS_MODEL,
            )
        elif task == "token-classification":
            from ofp_playground.agents.llm.ner import DEFAULT_MODEL as DEFAULT_NER_MODEL
            from ofp_playground.agents.llm.ner import NERAgent
            agent = NERAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_NER_MODEL,
            )
        elif task == "summarization":
            from ofp_playground.agents.llm.summarizer import (
                DEFAULT_MODEL as DEFAULT_SUMMARIZER_MODEL,
            )
            from ofp_playground.agents.llm.summarizer import SummarizationAgent
            agent = SummarizationAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_SUMMARIZER_MODEL,
            )
        elif task == "showrunner":
            from ofp_playground.agents.llm.director import parse_total_parts
            from ofp_playground.agents.llm.showrunner import ShowRunnerAgent
            total_parts = parse_total_parts(description)
            agent = ShowRunnerAgent(
                name=name,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.llm_model_huggingface,
                stop_callback=floor.stop,
                total_parts=total_parts,
            )
            floor.register_showrunner(agent.speaker_uri)
        elif task == "orchestrator":
            from ofp_playground.agents.llm.showrunner import OrchestratorAgent
            agent = OrchestratorAgent(
                name=name,
                mission=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.llm_model_huggingface,
                stop_callback=floor.stop,
                settings=settings,
            )
            floor.register_orchestrator(agent.speaker_uri)
        elif task == "code-generation":
            renderer.show_system_event(
                "HuggingFace code-generation is not supported. "
                "Use anthropic:code-generation, openai:code-generation, or google:code-generation."
            )
            raise ValueError(f"Unsupported agent type/task: {agent_type}")
        elif task == "text-to-music":
            # There is no HF music agent; this used to fall through to a *text*
            # agent that silently "answered" music assignments with prose.
            raise ValueError(
                "HuggingFace text-to-music is not supported. "
                "Use google:text-to-music (Lyria) for music generation."
            )
        else:
            # Default: text-generation (and any other text-in/text-out tasks)
            from ofp_playground.agents.llm.huggingface import HuggingFaceAgent
            agent = HuggingFaceAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or settings.defaults.llm_model_huggingface,
                relevance_filter=settings.defaults.relevance_filter,
                max_tokens=max_tokens_override,
            )

    elif agent_type in ("director", "director-hf"):
        api_key = settings.get_huggingface_key()
        if not api_key:
            raise ValueError("Configure the HuggingFace API key before adding an agent")
        from ofp_playground.agents.llm.director import DirectorAgent, parse_total_parts
        total_parts = parse_total_parts(description)
        agent = DirectorAgent(
            name=name,
            story_outline=description,
            total_parts=total_parts,
            bus=bus,
            conversation_id=floor.conversation_id,
            api_key=api_key,
            model=model_override or settings.defaults.llm_model_huggingface,
            stop_callback=floor.stop,
        )
        floor.register_director(agent.speaker_uri)

    else:
        renderer.show_system_event(
            f"Unknown agent type: {agent_type}. Use: anthropic, openai, google, hf, remote"
            f" (with -type for HF tasks, e.g. -type Text-to-Image,"
            f" -type Image-Classification, -type Summarization, etc."
            f" Run 'ofp-playground agents' for the full list)"
        )
        raise ValueError(f"Unsupported agent type/task: {agent_type}")

    if agent:
        if agent.speaker_uri in floor.active_agents:
            raise ValueError(f"Agent URI already in use: {agent.speaker_uri}")
        # Apply timeout / retry / token settings from CLI flags
        if timeout_override is not None:
            agent._timeout = timeout_override
        if max_retries_override is not None:
            agent._max_retries = max_retries_override
        if max_tokens_override and hasattr(agent, "_max_tokens"):
            agent._max_tokens = max_tokens_override
        # Wire per-session output directories into media / web agents
        if hasattr(agent, "_output_dir"):
            out = floor.output
            task = agent_type.split(":", 1)[1] if ":" in agent_type else ""
            if task in ("text-to-image",) or "image" in agent_type:
                agent._output_dir = out.images
            elif task in ("text-to-video",):
                agent._output_dir = out.videos
            elif task in ("text-to-music",):
                agent._output_dir = out.music
            elif task == "code-generation":
                agent._output_dir = out.sandbox
            else:
                agent._output_dir = out.root
            agent._output_dir.mkdir(parents=True, exist_ok=True)
        # Give every LLM agent access to the live URI→name registry
        if hasattr(agent, "set_name_registry"):
            agent.set_name_registry(floor._agents)
        # Give orchestrator access to the live manifest registry for capability-aware prompts
        if hasattr(agent, "set_manifest_registry"):
            agent.set_manifest_registry(floor._manifests)
        # Give all agents access to the shared session memory store
        if hasattr(agent, "set_memory_store"):
            agent.set_memory_store(floor._memory_store)
        # Give all agents access to the shared phase artifact store
        if hasattr(agent, "set_artifact_store"):
            agent.set_artifact_store(floor._artifact_store)
        # Policy-specific behaviour (e.g. the FREE_FOR_ALL relevance filter)
        agent.set_floor_policy(floor.policy)
        floor.register_agent(agent.speaker_uri, agent.name)
        registry.register(agent)
        model_name = model_override or getattr(agent, "_model", "default")
        renderer.show_system_event(f"Spawned {name} ({agent_type} / {model_name}) — joining conversation...")
        if web_renderer is not None:
            web_renderer.add_agent(agent.speaker_uri, agent.name)
            web_renderer.add_agent_meta(agent.speaker_uri, f"{agent_type} / {model_name}")
            web_renderer.add_system_event(f"Spawned {name} ({agent_type} / {model_name}) — joining conversation...")
        # Pre-register the agent's queue on the bus so that any messages sent
        # immediately after spawn (e.g. [ASSIGN] directives) are not dropped.
        # agent.run() will call bus.register() again — that's a harmless no-op.
        await bus.register(agent.speaker_uri, agent.queue)
        # Send OFP-compliant InviteEvent so the agent knows it joined the floor.
        await floor.invite_agent(agent.speaker_uri, getattr(agent, "_service_url", "local://agent"))
        registry.start(agent)
        return agent
    raise ValueError(f"Unsupported agent type/task: {agent_type}")


async def _attach_floor_callbacks(
    floor: FloorManager,
    bus: MessageBus,
    registry: AgentRegistry,
    settings: Settings,
    renderer: TerminalRenderer,
    web_renderer=None,
):
    """Attach spawn + breakout callbacks to the floor manager.

    Shared by both the terminal session (_run_session) and the web session
    (_run_web_session) so the agentic backbone behaves identically regardless
    of which renderer is active.

    renderer     — TerminalRenderer used for terminal notifications
    web_renderer — optional GradioRenderer; if set, events also go to the web UI
    """

    from ofp_playground.session import SessionService

    session = SessionService(floor, bus, registry)
    if os.environ.get("OFP_STATUS_DISABLED") != "1":
        await session.enable_status()
        if session.status_problem:
            renderer.show_system_event(session.status_problem)
    # Agents may publish immediately during setup, before floor.run() is scheduled.
    await bus.register(floor.speaker_uri, floor._queue)

    def _notify(msg: str) -> None:
        renderer.show_system_event(msg)
        if web_renderer is not None:
            web_renderer.add_system_event(msg)

    async def _floor_spawn_callback(spec_str: str) -> None:
        try:
            agent_type, name, description, model_ov, max_tokens_ov, timeout_ov, max_retries_ov = _parse_agent_spec(spec_str)
            task_type = agent_type.split(":", 1)[1] if ":" in agent_type else "text-generation"

            # Manifest-based check (rich capability info)
            existing = floor.find_agent_by_manifest(name, task_type)
            if existing:
                uri, manifest = existing
                existing_name = manifest.identification.conversationalName or uri
                caps = [kp for cap in (manifest.capabilities or []) for kp in (cap.keyphrases or [])]
                caps_str = ", ".join(caps) if caps else "unknown"
                _notify(
                    f"[Orchestrator] Skipping spawn of '{name}' — "
                    f"'{existing_name}' already registered (capabilities: [{caps_str}])"
                )
                return

            # Floor-registry fallback: use FloorManager's active agents as source of truth.
            if floor._resolve_agent_uri_by_name(name):
                _notify(f"[Orchestrator] Skipping spawn of '{name}' — agent already registered")
                return

            await _spawn_llm_agent(
                agent_type, name, description, floor, bus, registry, renderer, settings,
                model_ov, max_tokens_ov, timeout_ov, max_retries_ov,
                web_renderer=web_renderer,
            )
        except Exception as e:
            logger.error("spawn_callback failed for spec '%s': %s", spec_str, e)
            raise

    floor._spawn_callback = _floor_spawn_callback

    _breakout_session_count = 0

    async def _floor_breakout_callback(
        topic: str,
        policy: FloorPolicy,
        max_rounds: int,
        agent_specs: list[str],
    ) -> tuple[str, object]:
        from ofp_playground.floor.breakout import (
            build_compact_notification,
            run_breakout_session,
            save_breakout_artifact,
        )

        nonlocal _breakout_session_count

        agents = []
        temp_bus = MessageBus()  # placeholder — run_breakout_session creates its own bus

        for spec_str in agent_specs:
            try:
                agent = _create_breakout_agent(spec_str, temp_bus, "breakout-temp", settings)
                if agent:
                    agents.append(agent)
            except Exception as e:
                logger.exception("Breakout agent creation failed for '%s': see traceback", spec_str)
                _notify(f"Breakout agent failed: {e}")

        if len(agents) < 2:
            msg = f"[Breakout: {topic}] — could not create enough agents ({len(agents)}/2 minimum)."
            return msg, None

        result = await run_breakout_session(
            topic=topic,
            agents=agents,
            policy=policy,
            parent_conversation_id=floor.conversation_id,
            max_rounds=max_rounds,
            parent_renderer=renderer,
            trace_collector=floor.trace_collector,
            status_runtime=session.status,
        )

        artifact_path = save_breakout_artifact(result, floor.output.breakout)
        _notify(f"[Breakout] Artifact: {artifact_path}")

        _breakout_session_count += 1
        if floor._memory_store:
            floor._memory_store.store(
                "tasks",
                f"breakout_{_breakout_session_count}",
                f"'{result.topic}' | {result.round_count} rounds | "
                f"{', '.join(result.agent_names)} → {artifact_path}",
            )

        compact = build_compact_notification(result, artifact_path, _breakout_session_count)
        return compact, artifact_path

    floor._breakout_callback = _floor_breakout_callback

    _coding_session_count = 0

    async def _floor_coding_session_callback(
        topic: str,
        policy: FloorPolicy,
        max_rounds: int,
        agent_specs: list[str],
        required_context_files: list[str],
        initial_todo_items: list[str],
    ) -> tuple[str, object]:
        from ofp_playground.floor.coding_session import (
            build_coding_session_notification,
            run_coding_session,
            save_coding_session_artifact,
        )

        nonlocal _coding_session_count

        agents = []
        temp_bus = MessageBus()

        for spec_str in agent_specs:
            try:
                agent = _create_breakout_agent(spec_str, temp_bus, "coding-temp", settings)
                if agent:
                    agents.append(agent)
            except Exception as e:
                logger.exception("Coding session agent creation failed for '%s': see traceback", spec_str)
                _notify(f"Coding session agent failed: {e}")

        if len(agents) < 1:
            msg = f"[Coding session: {topic[:60]}] — could not create enough agents ({len(agents)}/1 minimum)."
            return msg, None

        # Use the session sandbox directory for shared file workspace
        sandbox_dir = floor.output.sandbox

        result = await run_coding_session(
            topic=topic,
            agents=agents,
            policy=policy,
            parent_conversation_id=floor.conversation_id,
            sandbox_dir=sandbox_dir,
            max_rounds=max_rounds,
            required_context_files=required_context_files,
            initial_todo_items=initial_todo_items,
            parent_renderer=renderer,
            trace_collector=floor.trace_collector,
            status_runtime=session.status,
        )

        artifact_path = save_coding_session_artifact(result, floor.output.breakout)
        _notify(f"[CodingSession] Artifact: {artifact_path} | {len(result.files)} files in {sandbox_dir}")

        _coding_session_count += 1
        if floor._memory_store:
            floor._memory_store.store(
                "tasks",
                f"coding_session_{_coding_session_count}",
                f"'{result.topic[:60]}' | {result.round_count} rounds | "
                f"{', '.join(result.agent_names)} | {len(result.files)} files → {sandbox_dir}",
            )

        compact = build_coding_session_notification(result, artifact_path, _coding_session_count)
        return compact, artifact_path

    floor._coding_session_callback = _floor_coding_session_callback
    return session


@click.group()
@click.version_option(version=__version__)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.pass_context
def main(ctx: click.Context, verbose: bool):
    """OFP Playground — Multi-party OFP conversation tool."""
    # Redirected streams may use a legacy encoding on any platform. Preserve
    # explicit encoding choices, escaping Unicode labels that cannot be encoded.
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty() and hasattr(stream, "reconfigure"):
            if sys.platform == "win32" and not os.environ.get("PYTHONIOENCODING"):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            else:
                stream.reconfigure(errors="backslashreplace")
    _load_dotenv()
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _configure_logging(verbose)


def _print_status(snapshot: dict) -> None:
    from rich.table import Table

    from ofp_playground.renderer.status_text import status_rows

    table = Table(title=f"ASP observations · cursor {snapshot.get('cursor', '?')}", expand=True)
    keys = ("name", "lifecycle", "health", "tasks", "floors", "governance", "source", "seen")
    for key in keys:
        table.add_column(key.title(), overflow="fold")
    for row in status_rows(snapshot):
        table.add_row(*(row[key] for key in keys))
    console.print(table)
    if not snapshot.get("agents"):
        click.echo("No registered ASP observations. Uninstrumented agents remain unknown.")


async def _read_status(*, data_dir: Path, port: int, watch: bool, interval: float,
                       count: int | None, agent: str | None, as_json: bool) -> None:
    import httpx

    from ofp_playground.status.client import connect_local_service

    try:
        connection = await connect_local_service(data_dir=data_dir, port=port, auto_start=False)
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException("ASP service unavailable; status is unknown. Start a session or ofp-status-server first.") from exc
    seen = 0
    try:
        while True:
            try:
                snapshot = await connection.snapshot()
            except (httpx.HTTPError, OSError, RuntimeError) as exc:
                if as_json:
                    click.echo(json.dumps({"available": False, "stale": True, "problem": "status-service-unavailable"}))
                raise click.ClickException("ASP observation lost; previous observations are stale.") from exc
            if agent:
                snapshot["agents"] = [entry for entry in snapshot.get("agents", [])
                                      if entry["agentId"] == agent or entry.get("name", "").casefold() == agent.casefold()]
            if as_json:
                click.echo(json.dumps({"available": True, **snapshot}, ensure_ascii=True))
            else:
                _print_status(snapshot)
            seen += 1
            if not watch or (count is not None and seen >= count):
                return
            await asyncio.sleep(interval)
    finally:
        await connection.close()


@main.command("status")
@click.option("--watch", is_flag=True, help="Poll until Ctrl+C; a lost connection exits with an error.")
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON object per observation (NDJSON with --watch).")
@click.option("--agent", help="Filter by exact instance ID or display name.")
@click.option("--interval", default=2.0, type=click.FloatRange(0.25, 60), show_default=True)
@click.option("--count", type=click.IntRange(1), help="Stop after this many observations when watching.")
@click.option("--port", default=8787, type=click.IntRange(1, 65535), show_default=True)
@click.option("--data-dir", default=Path.home() / ".ofp-playground" / "status", type=click.Path(path_type=Path), show_default=True)
def status(watch: bool, as_json: bool, agent: str | None, interval: float, count: int | None,
           port: int, data_dir: Path) -> None:
    """Read authenticated ASP observations without starting a new conversation."""
    try:
        asyncio.run(_read_status(data_dir=data_dir, port=port, watch=watch, interval=interval,
                                 count=count, agent=agent, as_json=as_json))
    except KeyboardInterrupt:
        pass


def _print_personas(search: str, as_json: bool, preview: bool) -> None:
    from ofp_playground.agents import library

    entries = [entry for slug, entry in library.load().items()
               if search.casefold() in (slug + " " + entry["display_name"]).casefold()]
    if as_json:
        click.echo(json.dumps([{key: entry[key] for key in ("slug", "display_name", "category", *(["content"] if preview else []))}
                               for entry in entries], ensure_ascii=True))
    else:
        for entry in entries:
            click.echo(f"{entry['slug']} — {entry['display_name']}")
            if preview:
                click.echo(entry["content"] + "\n")
        if not entries:
            click.echo("No matching personas. Try ofp personas without a search term.")


@main.command("personas")
@click.argument("search", default="")
@click.option("--json", "as_json", is_flag=True)
@click.option("--preview", is_flag=True, help="Show the resolved SOUL.md prompt before using it.")
def personas(search: str, as_json: bool, preview: bool) -> None:
    """Search the installed persona library by name or category/slug."""
    _print_personas(search, as_json, preview)


def _print_models(settings: Settings, as_json: bool) -> None:
    from ofp_playground.agents.llm.model_catalog import MODEL_CATALOG

    configured = {key: value for key, value in asdict(settings.defaults).items() if "model" in key}
    if as_json:
        click.echo(json.dumps({"configuredDefaults": configured, "catalog": list(MODEL_CATALOG),
                               "availability": "not-checked"}, ensure_ascii=True))
    else:
        click.echo("Configured model defaults (availability depends on your provider account):")
        for key, value in configured.items():
            click.echo(f"{key}: {value}")
        click.echo("Catalog choices (custom provider IDs are also accepted): " + ", ".join(MODEL_CATALOG))


@main.command("models")
@click.option("--json", "as_json", is_flag=True)
def models(as_json: bool) -> None:
    """Show configured provider model choices; never prints API keys."""
    _print_models(Settings.load(), as_json)


@main.command()
@click.option(
    "--policy", "-p",
    default="sequential",
    help="Floor policy: sequential, round_robin, moderated, free_for_all, showrunner_driven",
)
@click.option(
    "--agent", "-a",
    "agents",
    multiple=True,
    metavar="TYPE:NAME[:DESCRIPTION]",
    help="Pre-spawn an agent (e.g. anthropic:Claude:You are helpful)",
)
@click.option(
    "--remote", "-r",
    "remotes",
    multiple=True,
    metavar="URL_OR_NAME",
    help="Connect to a remote OFP agent by URL or known slug (e.g. polly, arxiv, wikipedia)",
)
@click.option(
    "--no-human",
    is_flag=True,
    default=False,
    help="Run without a human agent (autonomous agent conversation)",
)
@click.option(
    "--topic", "-t",
    default=None,
    help="Seed topic to start the conversation (used with --no-human)",
)
@click.option(
    "--max-turns", "-n",
    default=None,
    type=int,
    help="Stop automatically after N utterances",
)
@click.option(
    "--human-name",
    default="User",
    help="Display name for the human participant (default: User)",
)
@click.option(
    "--show-floor-events",
    is_flag=True,
    default=False,
    help="Show floor grant/request system events (hidden by default)",
)
@click.option(
    "--verbose", "-v",
    is_flag=True,
    default=False,
    help="Enable debug logging",
)
@click.pass_context
def start(ctx: click.Context, policy: str, agents: tuple, remotes: tuple,
          no_human: bool, topic: str | None, max_turns: int | None,
          human_name: str, show_floor_events: bool, verbose: bool):
    """Start an interactive OFP conversation session.

    Agent spec formats (both supported):\n
      hf:Name:System prompt.:model-id\n
      -provider hf -name Name -system System prompt. -model model-id
    """
    verbose = verbose or ctx.obj.get("verbose", False)
    _configure_logging(verbose)
    settings = Settings.load()
    floor_policy = _parse_policy(policy)

    try:
        asyncio.run(_run_session(
            floor_policy, agents, remotes, settings, verbose,
            no_human=no_human, topic=topic, max_turns=max_turns,
            human_name=human_name, show_floor_events=show_floor_events,
        ))
    except KeyboardInterrupt:
        console.print("\n[dim]Session interrupted.[/dim]")
        ctx.exit(130)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option("--policy", "-p", default="sequential",
              help="Floor policy: sequential, round_robin, moderated, free_for_all, showrunner_driven")
@click.option("--agent", "-a", "agents", multiple=True, metavar="TYPE:NAME[:DESCRIPTION]",
              help="Pre-spawn an agent (e.g. anthropic:Claude:You are helpful)")
@click.option("--remote", "-r", "remotes", multiple=True, metavar="URL_OR_NAME",
              help="Connect to a remote OFP agent by URL or known slug (e.g. polly, arxiv, wikipedia)")
@click.option("--show-floor-events", is_flag=True, default=False,
              help="Show floor grant/request system events in the chat (hidden by default)")
@click.option("--topic", "-t", default=None,
              help="Seed topic to start the conversation automatically")
@click.option("--no-human", is_flag=True, default=False,
              help="Watch-only mode: agents talk autonomously, UI shows the conversation")
@click.option("--max-turns", "-n", default=None, type=int,
              help="Stop automatically after N utterances")
@click.option("--human-name", default="User",
              help="Display name for the human participant (default: User)")
@click.option("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
@click.option("--port", default=7860, type=int, help="Port to listen on (default: 7860)")
@click.option("--share", is_flag=True, default=False,
              help="Create a public Gradio share link")
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Enable debug logging")
@click.pass_context
def web(ctx: click.Context, policy: str, agents: tuple, remotes: tuple, show_floor_events: bool,
        topic: str | None, no_human: bool, max_turns: int | None, human_name: str,
        host: str, port: int, share: bool, verbose: bool):
    """Start the OFP Playground Gradio web UI.

    Opens a browser-based chat interface. Use --no-human to watch agents
    talk autonomously.
    """
    verbose = verbose or ctx.obj.get("verbose", False)
    _configure_logging(verbose)
    _load_dotenv()
    settings = Settings.load()
    floor_policy = _parse_policy(policy)

    try:
        asyncio.run(_run_web_session(
            floor_policy, agents, settings, verbose,
            topic=topic, no_human=no_human, max_turns=max_turns,
            human_name=human_name, host=host, port=port, share=share,
            remote_urls=remotes, show_floor_events=show_floor_events,
        ))
    except KeyboardInterrupt:
        console.print("\n[dim]Web session interrupted.[/dim]")
        ctx.exit(130)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("canvas")
@click.option("--port", default=8765, show_default=True, help="Port for the canvas server.")
@click.option("--host", default="localhost", show_default=True, help="Host to bind.")
@click.option("--open/--no-open", "open_browser", default=True, show_default=True)
@click.option("--policy", "-p", default=None, help="Floor policy (SEQUENTIAL, ROUND_ROBIN, FREE_FOR_ALL, MODERATED, SHOWRUNNER_DRIVEN).")
@click.option("--agent", "-a", "agents", multiple=True, metavar="SPEC", help="Agent spec (same format as 'start').")
@click.option("--remote", "-r", "remotes", multiple=True, metavar="URL_OR_NAME",
              help="Remote OFP agent by URL or known slug (same as 'start --remote').")
@click.option("--show-floor-events", is_flag=True, default=False,
              help="Pre-enable floor grant/request events on the Floor node.")
@click.option("--topic", "-t", default=None, help="Initial topic seeded to the floor.")
@click.option("--no-human", is_flag=True, default=False, help="Omit the human participant node.")
@click.option("--max-turns", "-n", default=None, type=int, help="Stop after this many turns.")
@click.option("--human-name", default="User", show_default=True, help="Display name for the human node.")
@click.pass_context
def canvas(
    ctx: click.Context,
    port: int,
    host: str,
    open_browser: bool,
    policy: str | None,
    agents: tuple[str, ...],
    remotes: tuple[str, ...],
    show_floor_events: bool,
    topic: str | None,
    no_human: bool,
    max_turns: int | None,
    human_name: str,
) -> None:
    """Launch the visual canvas for building and running floors.

    Accepts the same --policy / --agent / --topic flags as 'start'.
    When provided, the canvas opens pre-populated with those nodes.
    Press Run to start the session.
    """
    import sys
    import webbrowser
    from pathlib import Path

    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise click.ClickException("Canvas controls support loopback hosts only in this local profile")

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    try:
        import uvicorn

        from canvas.backend.main import build_app
    except ImportError as exc:
        raise click.ClickException(
            "Canvas dependencies not installed. Run: pip install -e '.[canvas]'"
        ) from exc

    initial_config = None
    if agents or remotes or policy or topic:
        initial_config = _build_canvas_config(
            policy=policy,
            agents=agents,
            topic=topic,
            no_human=no_human,
            max_turns=max_turns,
            human_name=human_name,
            remotes=remotes,
            show_floor_events=show_floor_events,
        )

    app = build_app(initial_config=initial_config)
    url = f"http://{host}:{port}"

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    click.echo(f"Canvas running at {url}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


async def _run_web_session(
    policy: FloorPolicy,
    agent_specs: tuple[str, ...],
    settings: Settings,
    verbose: bool,
    topic: str | None = None,
    no_human: bool = False,
    max_turns: int | None = None,
    human_name: str = "User",
    host: str = "127.0.0.1",
    port: int = 7860,
    share: bool = False,
    remote_urls: tuple[str, ...] = (),
    show_floor_events: bool = False,
) -> None:
    """Run the web UI session (Gradio + OFP bus in the same process)."""
    from ofp_playground.status.server import is_loopback

    if not is_loopback(host) or share:
        raise ValueError("The web control UI supports loopback only; public sharing needs an authenticated deployment profile.")
    from ofp_playground.agents.registry import AgentRegistry

    # Create the Gradio renderer early so spawn events appear in the web UI
    from ofp_playground.renderer.gradio_ui import (
        GradioFloorEvents,
        GradioRenderer,
        launch_web_session,
    )
    _human_uris: set[str] = set()
    web_renderer = GradioRenderer(agent_names={}, human_uris=_human_uris)

    bus = MessageBus()
    # Floor/orchestrator system events (assignments, rounds, manuscript) go to the
    # web chat like they go to the terminal in `start`.
    floor = FloorManager(
        bus, policy=policy,
        renderer=GradioFloorEvents(web_renderer, show_floor_events=show_floor_events),
    )
    registry = AgentRegistry()
    web_renderer.add_system_event(
        "Running in autonomous mode" if no_human else "Interactive mode"
    )
    web_renderer.add_system_event(f"Conversation started (ID: {floor.conversation_id[:8]}...)")

    human = None
    tasks = []
    agent_display_names: list[str] = []

    if not no_human:
        from ofp_playground.agents.web_human import WebHumanAgent
        human = WebHumanAgent(
            name=human_name,
            bus=bus,
            conversation_id=floor.conversation_id,
            floor_policy=policy.value,  # same turn-taking as the terminal human
        )
        human.notice_callback = web_renderer.add_system_event
        floor.register_agent(human.speaker_uri, human.name)
        registry.register(human)
        agent_display_names.append(human_name)
        _human_uris.add(human.speaker_uri)
        web_renderer.add_agent(human.speaker_uri, human_name)

    # Attach spawn + breakout callbacks (same backbone as terminal session)
    term_renderer = TerminalRenderer(console, show_floor_events=False)
    session = await _attach_floor_callbacks(floor, bus, registry, settings, term_renderer, web_renderer=web_renderer)
    if human is not None:
        registry.start(human)

    # Spawn pre-configured LLM agents
    for spec in agent_specs:
        try:
            agent_type, name, description, model_ov, max_tokens_ov, timeout_ov, retries_ov = _parse_agent_spec(spec)
            await _spawn_llm_agent(
                agent_type, name, description, floor, bus, registry,
                term_renderer, settings, model_ov, max_tokens_ov, timeout_ov, retries_ov,
                web_renderer=web_renderer,
            )
            agent_display_names.append(name)
        except Exception as e:
            console.print(f"[red]Failed to spawn agent: {e}[/red]")
            await session.close()
            raise ValueError(f"Could not start agent: {e}") from e

    # Connect remote agents (same as `start --remote`)
    for slug_or_url in remote_urls:
        try:
            remote = await _connect_remote_agent(slug_or_url, floor, bus, registry, term_renderer)
            web_renderer.add_agent(remote.speaker_uri, remote.name)
            web_renderer.add_system_event(f"Connected {remote.name} → {slug_or_url}")
            agent_display_names.append(remote.name)
        except Exception as e:
            console.print(f"[red]Failed to connect remote agent: {e}[/red]")
            await session.close()
            raise ValueError(f"Could not connect remote agent: {e}") from e

    floor.max_turns = max_turns
    from ofp_playground.session import kickoff
    tasks.append(kickoff(
        floor, bus, topic=topic, human=human, notify=web_renderer.add_system_event,
        delay=1.5, seed=_seed_topic,
    ))

    # Get the running loop and launch Gradio from this coroutine's thread
    loop = asyncio.get_event_loop()

    async def _web_spawn_fn(spec: str) -> None:
        """Spawn an agent (or connect a remote one) from the Gradio UI."""
        try:
            if spec.startswith("remote "):
                remote = await _connect_remote_agent(
                    spec.split(maxsplit=1)[1].strip(), floor, bus, registry, term_renderer,
                )
                web_renderer.add_agent(remote.speaker_uri, remote.name)
                web_renderer.add_system_event(f"Connected {remote.name}")
                return
            at, name, desc, model_ov, maxtok_ov, timeout_ov, maxretry_ov = _parse_agent_spec(spec)
            await _spawn_llm_agent(
                at, name, desc, floor, bus, registry,
                term_renderer, settings, model_ov, maxtok_ov,
                timeout_ov, maxretry_ov,
                web_renderer=web_renderer,
            )
        except Exception as e:
            web_renderer.add_system_event(f"Spawn failed: {e}")
            raise

    async def _web_remove_fn(uri: str, mode: str) -> dict:
        result = await session.remove(uri, mode=mode)
        web_renderer.add_system_event(f"Remove {floor.active_agents.get(uri, uri)}: {result.get('outcome', 'requested')}")
        return result

    async def _web_governance_fn(uri: str, value: str) -> dict:
        result = await session.set_governance(uri, value)
        web_renderer.add_system_event(f"Governance {value}: {result.get('outcome', 'requested')}")
        return result

    async def _web_input_fn(text: str) -> None:
        if human is None or human.speaker_uri not in floor.active_agents or floor._stop_event.is_set():
            raise ValueError("Human input is no longer available")
        if text.strip().startswith("/"):
            # Same floor commands as the terminal: /grant NAME, /pass, /floor, /agents.
            result = await session.floor_command(text, human.speaker_uri)
            web_renderer.add_system_event(
                result or f"Unknown command {text.split()[0]} — try /grant NAME, /pass, /floor, /agents"
            )
            return
        await session.enqueue_input(human.speaker_uri, text)
        web_renderer.add_user_input(human.name, text)

    async def _refresh_status():
        while True:
            tracked = {**session.participants, **(session.status.agents if session.status else {})}
            participants = {agent.instance_id: agent.name for agent in tracked.values()}
            choices = [(agent.name + (" (removed)" if uri not in floor.active_agents else ""), uri)
                       for uri, agent in tracked.items()]
            snapshot = None
            available = False
            problem = session.status_problem
            if session.status:
                try:
                    snapshot = await session.status.connection.snapshot()
                    snapshot["agents"] = [agent for agent in snapshot.get("agents", []) if agent["agentId"] in participants]
                    available = True
                    problem = session.status.connection.problem
                except (httpx.HTTPError, OSError, RuntimeError, ValueError):
                    problem = "Status service unavailable; retained values are stale"
            web_renderer.update_status(snapshot, participants, choices, available=available, problem=problem)
            await asyncio.sleep(1)

    async def _serve():
        launcher = asyncio.create_task(asyncio.to_thread(
            launch_web_session,
            floor=floor,
            bus=bus,
            human_agent=human,
            agent_specs_display=agent_display_names,
            policy_name=policy.value,
            loop=loop,
            host=host,
            port=port,
            share=share,
            gradio_renderer=web_renderer,
            spawn_fn=_web_spawn_fn,
            remove_fn=_web_remove_fn,
            governance_fn=_web_governance_fn,
            input_fn=_web_input_fn if human else None,
            stop_fn=floor.stop,
        ))
        demo = None
        try:
            demo = await asyncio.shield(launcher)
            console.print(f"[green]Web UI ready → http://{host}:{port}[/green]")
            await floor._stop_event.wait()
        finally:
            # to_thread cannot cancel a launch already in progress. Join it and
            # close the returned server even when stop arrives during startup.
            if demo is None:
                demo = await launcher
            await asyncio.to_thread(demo.close)

    async def _observe():
        from ofp_playground.renderer.gradio_ui import _drain_output_queue

        observer_uri = "tag:ofp-playground.local,2025:web-observer"
        queue = human.output_queue if human is not None else asyncio.Queue()
        if human is None:
            await bus.register(observer_uri, queue)
        try:
            await _drain_output_queue(queue, web_renderer)
        finally:
            if human is None:
                await bus.unregister(observer_uri)

    tasks.extend([_serve(), _observe(), _refresh_status()])

    try:
        await session.run(*tasks)
    finally:
        await session.close()


@main.command()
def agents():
    """List available agent types."""
    console.print(
        "[bold]Available agent types:[/bold]\n"
        "  [cyan]anthropic[/cyan] / claude  — Anthropic Claude (requires ANTHROPIC_API_KEY)\n"
        "                             -type defaults to Text-Generation\n"
        "  [cyan]openai[/cyan] / gpt        — OpenAI GPT (requires OPENAI_API_KEY)\n"
        "                             -type Text-to-Image uses OpenAI image models\n"
        "  [cyan]google[/cyan] / gemini     — Google Gemini (requires GOOGLE_API_KEY)\n"
        "  [cyan]hf[/cyan] / huggingface    — HuggingFace Inference API (requires HF_API_KEY)\n"
        "                             -type defaults to Text-Generation\n"
        "\n"
        "  [bold]Anthropic generative tasks (-type):[/bold]\n"
        "    Text-Generation          — chat/text LLM (default: claude-haiku-4-5-20251001)\n"
        "    Image-to-Text            — analyze images via Claude vision (default: claude-haiku-4-5-20251001)\n"
        "\n"
        "  [bold]OpenAI generative tasks (-type):[/bold]\n"
        "    Text-Generation          — chat/text LLM (default: gpt-5.4-nano)\n"
        "    Text-to-Image            — generate images via Responses API (default: gpt-5.4)\n"
        "    Image-to-Text            — analyze images via vision (default: gpt-4o-mini)\n"
        "\n"
        "  [bold]Google generative tasks (-type):[/bold]\n"
        "    Text-Generation          — chat/text LLM (default: gemini-3.1-flash-lite)\n"
        "    Text-to-Image            — generate images via Nano Banana (default: gemini-3.1-flash-image-preview)\n"
        "    Image-to-Text            — analyze images via Gemini vision (default: gemini-3-flash-preview)\n"
        "    Text-to-Music            — generate music via Lyria RealTime (default: lyria-realtime-exp)\n"
        "\n"
        "  [bold]HF generative tasks (-type):[/bold]\n"
        "    Text-Generation          — chat/text LLM (default)\n"
        "    Text-to-Image            — generate images from conversation (default: FLUX.1-dev)\n"
        "    Text-to-Video            — generate video clips (default: Wan2.2-TI2V-5B)\n"
        "    Image-Text-to-Text       — vision-language model (default: Qwen2.5-VL-7B)\n"
        "\n"
        "  [bold]HF perception tasks (-type):[/bold]\n"
        "    Image-Classification     — label images with predicted classes (default: vit-base-patch16-224)\n"
        "    Object-Detection         — detect & count objects in images (default: detr-resnet-50)\n"
        "    Image-Segmentation       — segment image into labeled regions (default: segformer_b2_clothes)\n"
        "    Image-to-Text            — OCR / read text from images (default: GLM-OCR)\n"
        "    Text-Classification      — classify conversation sentiment (default: roberta-base-sentiment)\n"
        "    Token-Classification     — extract named entities (default: bert-multilingual-ner)\n"
        "    Summarization            — periodically summarize the conversation (default: bart-large-cnn)\n"
        "\n"
        "  [cyan]director[/cyan]            — Narrative director (HF LLM, speaks first each round)\n"
        "                             -system carries the story outline; '6 PARTS ...' sets part count\n"
        "  [cyan]hf -type ShowRunner[/cyan] — Show runner (HF LLM, speaks last each round; synthesizes + directs)\n"
        "                             -system carries the story outline; '6 PARTS ...' sets part count\n"
        "  [cyan]orchestrator[/cyan]        — Intelligent orchestrator (SHOWRUNNER_DRIVEN policy)\n"
        "                             speaks first, assigns tasks, accepts/rejects/spawns/kicks agents\n"
        "                             -system carries the mission description\n"
        "                             directives: [ASSIGN], [ACCEPT], [REJECT], [KICK], [SPAWN], [TASK_COMPLETE]\n"
        "  [cyan]human[/cyan]               — Human participant (stdin/stdout)\n"
        "  [cyan]remote[/cyan]              — Remote OFP agent via HTTP (use --remote URL_OR_NAME)\n"
        "                             known slugs: polly, arxiv, github, sec, web-search, wikipedia, stella, verity, profanity\n"
        "                             full registry: https://openfloor.dev/agent-registry"
    )


@main.command()
@click.argument("envelope_file", type=click.Path(exists=True))
def validate(envelope_file: str):
    """Validate an OFP envelope JSON file."""
    import json
    try:
        with open(envelope_file) as f:
            data = json.load(f)
        of_data = data.get("openFloor", data)
        from openfloor import Envelope
        envelope = Envelope(**of_data)
        console.print("[green]✓ Valid OFP envelope[/green]")
        console.print(f"  Conversation: {envelope.conversation.id if envelope.conversation else 'N/A'}")
        console.print(f"  Sender: {envelope.sender.speakerUri if envelope.sender else 'N/A'}")
        console.print(f"  Events: {len(envelope.events or [])}")
    except Exception as e:
        logger.exception("Operation failed at an application error boundary")
        console.print(f"[red]✗ Invalid envelope: {e}[/red]")
        sys.exit(1)
