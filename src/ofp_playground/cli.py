"""CLI entry point for OFP Playground."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

import click
from rich.console import Console

from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.config.settings import Settings
from ofp_playground.floor.manager import FloorManager
from ofp_playground.floor.policy import FloorPolicy
from ofp_playground.renderer.terminal import TerminalRenderer
from ofp_playground.agents.human import HumanAgent
from ofp_playground.agents.registry import AgentRegistry

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


def _parse_agent_spec(spec: str) -> tuple[str, str, str, Optional[str], Optional[int], Optional[float], int]:
    """Parse agent spec in two supported formats:

    Colon format:  type:name[:description[:model]]
    Flag format:   -provider TYPE -name NAME [-system DESCRIPTION] [-model MODEL]
                   [-max-tokens N] [-timeout SECONDS] [-max-retries N]

    Examples:
        hf:Astronomer:You are a skeptical astronomer.:MiniMaxAI/MiniMax-M2.5
        -provider hf -name Astronomer -system You are a skeptical astronomer. -model MiniMaxAI/MiniMax-M2.5
        -provider hf -name FastTask -timeout 30 -max-retries 2

    Returns: (agent_type, name, description, model_override, max_tokens_override, timeout, max_retries)
    """
    import re

    spec = spec.strip()

    if spec.startswith("-"):
        # Flag-based format: find each -flag and collect its value up to the next -flag
        flag_re = re.compile(r"-(provider|name|system|model|type|max-tokens|timeout|max-retries)\s+", re.IGNORECASE)
        matches = list(flag_re.finditer(spec))
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
        max_retries_override = int(max_retries_raw) if max_retries_raw and max_retries_raw.isdigit() else 0

        if not provider:
            raise click.BadParameter(f"Missing -provider in agent spec: {spec}")
        if not name:
            raise click.BadParameter(f"Missing -name in agent spec: {spec}")
        return agent_type, name, description, model_override, max_tokens_override, timeout_override, max_retries_override

    # Colon-separated format.
    # Supports both:
    #   type:name[:description[:model]]              e.g. openai:Artbot:painter
    #   type:subtype:name[:description[:model]]      e.g. openai:text-to-image:Artbot:painter
    TASK_SUBTYPES = {
        "text-to-image", "image-to-text", "text-to-video", "text-generation",
        "image-text-to-text", "image-classification", "object-detection",
        "image-segmentation", "token-classification", "text-classification",
        "summarization", "showrunner",
    }
    parts = spec.split(":", 4)  # up to 5 parts to accommodate type:subtype:name:desc:model
    if len(parts) < 2:
        raise click.BadParameter(
            f"Invalid agent spec: '{spec}'. "
            f"Use 'type:name[:description[:model]]' or "
            f"'type:subtype:name[:description[:model]]' or "
            f"'-provider TYPE -name NAME [-system DESC] [-model MODEL]'"
        )
    # Detect type:subtype:name:... format
    if len(parts) >= 3 and parts[1].lower() in TASK_SUBTYPES:
        agent_type = f"{parts[0]}:{parts[1]}".lower()
        name = parts[2]
        description = parts[3] if len(parts) > 3 else f"I am {name}, an AI assistant."
        model_override = parts[4] if len(parts) > 4 else None
    else:
        agent_type = parts[0].lower()
        name = parts[1]
        description = parts[2] if len(parts) > 2 else f"I am {name}, an AI assistant."
        model_override = parts[3] if len(parts) > 3 else None
    return agent_type, name, description, model_override, None, None, 0


async def _seed_topic(topic: str, floor: "FloorManager", bus: "MessageBus") -> None:
    """Inject a topic message from the floor manager to kick off the conversation."""
    from openfloor import Envelope, Sender, Conversation, UtteranceEvent, DialogEvent, TextFeature, Token
    import uuid
    from ofp_playground.bus.message_bus import FLOOR_MANAGER_URI

    de = DialogEvent(
        speakerUri=FLOOR_MANAGER_URI,
        id=str(uuid.uuid4()),
        features={"text": TextFeature(tokens=[Token(value=topic)])},
    )
    envelope = Envelope(
        sender=Sender(speakerUri=FLOOR_MANAGER_URI, serviceUrl="local://floor-manager"),
        conversation=Conversation(id=floor.conversation_id),
        events=[UtteranceEvent(dialogEvent=de)],
    )
    await bus.send(envelope)


async def _run_session(
    policy: FloorPolicy,
    agent_specs: tuple[str, ...],
    remote_urls: tuple[str, ...],
    settings: Settings,
    verbose: bool,
    no_human: bool = False,
    topic: Optional[str] = None,
    max_turns: Optional[int] = None,
    human_name: str = "User",
    show_floor_events: bool = False,
) -> None:
    """Run the main conversation session."""
    renderer = TerminalRenderer(console, show_floor_events=show_floor_events)
    bus = MessageBus()

    floor = FloorManager(bus, policy=policy, renderer=renderer)

    renderer.show_header(floor.conversation_id, policy.value, 0)

    registry = AgentRegistry()

    # Spawn callback for OrchestratorAgent [SPAWN] directives (set after registry exists)
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
                renderer.show_system_event(
                    f"[Orchestrator] Skipping spawn of '{name}' — "
                    f"'{existing_name}' already registered (capabilities: [{caps_str}])"
                )
                return

            # Registry fallback: name check that works even before manifests arrive
            if registry.by_name(name):
                renderer.show_system_event(
                    f"[Orchestrator] Skipping spawn of '{name}' — agent already registered"
                )
                return

            await _spawn_llm_agent(
                agent_type, name, description, floor, bus, registry, renderer, settings,
                model_ov, max_tokens_ov, timeout_ov, max_retries_ov,
            )
        except Exception as e:
            logger.error("spawn_callback failed for spec '%s': %s", spec_str, e)
            renderer.show_system_event(f"Orchestrator spawn failed: {e}")

    floor._spawn_callback = _floor_spawn_callback
    tasks = [floor.run()]

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

        async def handle_agents(_args: str):
            renderer.show_agents_table(floor.active_agents, floor.floor_holder)

        async def handle_help(_args: str):
            renderer.show_help()

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
            holder = floor.floor_holder
            holder_name = floor.active_agents.get(holder, holder) if holder else "Nobody"
            renderer.show_system_event(f"Current floor holder: {holder_name}")
            queue = floor._policy.queue
            if queue:
                waiting = [floor.active_agents.get(uri, uri) for uri, _ in queue]
                renderer.show_system_event(f"Waiting: {', '.join(waiting)}")

        async def handle_spawn(args: str):
            parts = args.split(maxsplit=3)
            if len(parts) < 2:
                renderer.show_system_event("Usage: /spawn <type> <name> [description] [model]")
                return
            agent_type = parts[0].lower()
            name = parts[1]
            description = parts[2] if len(parts) > 2 else f"I am {name}, an AI assistant."
            model_ov = parts[3] if len(parts) > 3 else None
            await _spawn_llm_agent(agent_type, name, description, floor, bus, registry, renderer, settings, model_ov)

        async def handle_kick(args: str):
            name = args.strip()
            agent = registry.by_name(name)
            if agent:
                agent.stop()
                floor.unregister_agent(agent.speaker_uri)
                registry.unregister(agent.speaker_uri)
                renderer.show_system_event(f"Removed {name} from the conversation")
            else:
                renderer.show_system_event(f"Agent '{name}' not found")

        human.register_command("agents", handle_agents)
        human.register_command("help", handle_help)
        human.register_command("history", handle_history)
        human.register_command("floor", handle_floor)
        human.register_command("spawn", handle_spawn)
        human.register_command("kick", handle_kick)
        tasks.append(human.run())
    else:
        renderer.show_system_event("Running in autonomous mode — press Ctrl+C to stop")

    # Spawn pre-configured agents
    for spec in agent_specs:
        try:
            agent_type, name, description, model_ov, max_tokens_ov, timeout_ov, max_retries_ov = _parse_agent_spec(spec)
            await _spawn_llm_agent(agent_type, name, description, floor, bus, registry, renderer, settings, model_ov, max_tokens_ov, timeout_ov, max_retries_ov)
        except Exception as e:
            renderer.show_system_event(f"Failed to spawn agent: {e}")

    # Connect remote agents
    for url in remote_urls:
        try:
            from ofp_playground.agents.remote import RemoteOFPAgent
            remote_name = f"Remote-{url.split('//')[-1].split('/')[0][:16]}"
            remote = RemoteOFPAgent(
                service_url=url,
                name=remote_name,
                bus=bus,
                conversation_id=floor.conversation_id,
            )
            floor.register_agent(remote.speaker_uri, remote.name)
            registry.register(remote)
            renderer.show_system_event(f"Connected remote agent {remote_name} → {url}")
            asyncio.create_task(remote.run())
        except Exception as e:
            renderer.show_system_event(f"Failed to connect to {url}: {e}")

    # LLM/remote agents are started via asyncio.create_task in _spawn_llm_agent;
    # only add the human agent run to tasks if present (handled above in the not no_human block)

    # Seed topic + optional turn watchdog
    if topic or max_turns:
        async def _orchestrate():
            await asyncio.sleep(1.0)
            if topic:
                renderer.show_system_event(f'Topic: "{topic}"')
                await _seed_topic(topic, floor, bus)
                # If human holds the floor in sequential mode, yield it so agents
                # can respond to the seeded topic; human re-queues for next turn.
                if not no_human and floor.floor_holder == human.speaker_uri:
                    human._has_floor = False
                    await human.yield_floor()
                    await human.request_floor()
                # SHOWRUNNER_DRIVEN: orchestrator speaks first
                if floor._orchestrator_uri:
                    await asyncio.sleep(0.2)
                    await floor.grant_to(floor._orchestrator_uri)
                # Director gets the floor first — but only when ShowRunner is not present
                # (ShowRunner speaks last, so Director directs before story agents respond)
                elif floor._director_uri and not floor._showrunner_uri:
                    await asyncio.sleep(0.2)  # let topic envelope reach all agent queues
                    await floor.grant_to(floor._director_uri)
            if max_turns:
                # Poll history and stop when turn count is reached
                while floor.history.__len__() < max_turns:
                    await asyncio.sleep(2.0)
                renderer.show_system_event(
                    f"Reached {max_turns} turns — stopping conversation."
                )
                floor.stop()

        tasks.append(_orchestrate())

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        floor.stop()
        renderer.show_system_event("Conversation ended. Goodbye!")


async def _spawn_llm_agent(
    agent_type: str,
    name: str,
    description: str,
    floor: FloorManager,
    bus: MessageBus,
    registry: AgentRegistry,
    renderer: TerminalRenderer,
    settings: Settings,
    model_override: Optional[str] = None,
    max_tokens_override: Optional[int] = None,
    timeout_override: Optional[float] = None,
    max_retries_override: int = 0,
) -> None:
    """Spawn and register an LLM agent."""
    agent = None

    if agent_type in ("anthropic", "claude"):
        api_key = settings.get_anthropic_key()
        if not api_key:
            api_key = click.prompt(f"Enter Anthropic API key for {name}", hide_input=True)
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

    elif agent_type in ("openai", "gpt") or agent_type.startswith(("openai:", "gpt:")):
        api_key = settings.get_openai_key()
        if not api_key:
            api_key = click.prompt(f"Enter OpenAI API key for {name}", hide_input=True)

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
        else:
            renderer.show_system_event(
                f"Unknown OpenAI task: {task}. Use OpenAI for text-generation, text-to-image, or image-to-text."
            )
            return

    elif agent_type in ("google", "gemini") or agent_type.startswith(("google:", "gemini:")):
        api_key = settings.get_google_key()
        if not api_key:
            api_key = click.prompt(f"Enter Google API key for {name}", hide_input=True)

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
        else:
            renderer.show_system_event(
                f"Unknown Google task: {task}. Use google for text-generation, text-to-image, or image-to-text."
            )
            return

    elif agent_type in ("huggingface", "hf") or agent_type.startswith(("huggingface:", "hf:")):
        api_key = settings.get_huggingface_key()
        if not api_key:
            api_key = click.prompt(f"Enter HuggingFace API key for {name}", hide_input=True)

        # Extract task type from compound agent_type string (e.g. "hf:text-to-image")
        task = agent_type.split(":", 1)[1] if ":" in agent_type else "text-generation"

        if task == "text-to-image":
            from ofp_playground.agents.llm.image import ImageAgent
            from ofp_playground.agents.llm.image import DEFAULT_MODEL as DEFAULT_IMAGE_MODEL
            agent = ImageAgent(
                name=name,
                style=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_IMAGE_MODEL,
            )
        elif task == "text-to-video":
            from ofp_playground.agents.llm.video import VideoAgent
            from ofp_playground.agents.llm.video import DEFAULT_MODEL as DEFAULT_VIDEO_MODEL
            agent = VideoAgent(
                name=name,
                style=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_VIDEO_MODEL,
            )
        elif task == "image-text-to-text":
            from ofp_playground.agents.llm.multimodal import MultimodalAgent
            from ofp_playground.agents.llm.multimodal import DEFAULT_MODEL as DEFAULT_MULTIMODAL_MODEL
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
            from ofp_playground.agents.llm.classifier import ImageClassificationAgent
            from ofp_playground.agents.llm.classifier import DEFAULT_MODEL as DEFAULT_CLASSIFIER_MODEL
            agent = ImageClassificationAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_CLASSIFIER_MODEL,
            )
        elif task == "object-detection":
            from ofp_playground.agents.llm.detector import ObjectDetectionAgent
            from ofp_playground.agents.llm.detector import DEFAULT_MODEL as DEFAULT_DETECTOR_MODEL
            agent = ObjectDetectionAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_DETECTOR_MODEL,
            )
        elif task == "image-segmentation":
            from ofp_playground.agents.llm.segmenter import ImageSegmentationAgent
            from ofp_playground.agents.llm.segmenter import DEFAULT_MODEL as DEFAULT_SEGMENTER_MODEL
            agent = ImageSegmentationAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_SEGMENTER_MODEL,
            )
        elif task == "image-to-text":
            from ofp_playground.agents.llm.ocr import OCRAgent
            from ofp_playground.agents.llm.ocr import DEFAULT_MODEL as DEFAULT_OCR_MODEL
            agent = OCRAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_OCR_MODEL,
            )
        elif task == "text-classification":
            from ofp_playground.agents.llm.text_classifier import TextClassificationAgent
            from ofp_playground.agents.llm.text_classifier import DEFAULT_MODEL as DEFAULT_TEXTCLS_MODEL
            agent = TextClassificationAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_TEXTCLS_MODEL,
            )
        elif task == "token-classification":
            from ofp_playground.agents.llm.ner import NERAgent
            from ofp_playground.agents.llm.ner import DEFAULT_MODEL as DEFAULT_NER_MODEL
            agent = NERAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_NER_MODEL,
            )
        elif task == "summarization":
            from ofp_playground.agents.llm.summarizer import SummarizationAgent
            from ofp_playground.agents.llm.summarizer import DEFAULT_MODEL as DEFAULT_SUMMARIZER_MODEL
            agent = SummarizationAgent(
                name=name,
                synopsis=description,
                bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key,
                model=model_override or DEFAULT_SUMMARIZER_MODEL,
            )
        elif task == "showrunner":
            from ofp_playground.agents.llm.showrunner import ShowRunnerAgent
            from ofp_playground.agents.llm.director import parse_total_parts
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
                max_tokens=max_tokens_override or 500,
            )

    elif agent_type in ("orchestrator",):
        api_key = settings.get_huggingface_key()
        if not api_key:
            api_key = click.prompt(f"Enter HuggingFace API key for {name}", hide_input=True)
        from ofp_playground.agents.llm.showrunner import OrchestratorAgent
        agent = OrchestratorAgent(
            name=name,
            mission=description,
            bus=bus,
            conversation_id=floor.conversation_id,
            api_key=api_key,
            model=model_override or settings.defaults.llm_model_huggingface,
            stop_callback=floor.stop,
        )
        floor.register_orchestrator(agent.speaker_uri)

    elif agent_type in ("director", "director-hf"):
        api_key = settings.get_huggingface_key()
        if not api_key:
            api_key = click.prompt(f"Enter HuggingFace API key for {name}", hide_input=True)
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
            f"Unknown agent type: {agent_type}. Use: anthropic, openai, google, hf"
            f" (with -type for HF tasks, e.g. -type Text-to-Image,"
            f" -type Image-Classification, -type Summarization, etc."
            f" Run 'ofp-playground agents' for the full list)"
        )
        return

    if agent:
        # Apply timeout / retry settings from CLI flags
        if timeout_override is not None:
            agent._timeout = timeout_override
        if max_retries_override:
            agent._max_retries = max_retries_override
        # Give every LLM agent access to the live URI→name registry
        if hasattr(agent, "set_name_registry"):
            agent.set_name_registry(floor._agents)
        # Give orchestrator access to the live manifest registry for capability-aware prompts
        if hasattr(agent, "set_manifest_registry"):
            agent.set_manifest_registry(floor._manifests)
        floor.register_agent(agent.speaker_uri, agent.name)
        registry.register(agent)
        model_name = model_override or getattr(agent, "_model", "default")
        renderer.show_system_event(f"Spawned {name} ({agent_type} / {model_name}) — joining conversation...")
        asyncio.create_task(agent.run())


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.pass_context
def main(ctx: click.Context, verbose: bool):
    """OFP Playground — Multi-party OFP conversation tool."""
    _load_dotenv()
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)


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
    metavar="URL",
    help="Connect to remote OFP agent URL",
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
@click.pass_context
def start(ctx: click.Context, policy: str, agents: tuple, remotes: tuple,
          no_human: bool, topic: Optional[str], max_turns: Optional[int],
          human_name: str, show_floor_events: bool):
    """Start an interactive OFP conversation session.

    Agent spec formats (both supported):\n
      hf:Name:System prompt.:model-id\n
      -provider hf -name Name -system System prompt. -model model-id
    """
    verbose = ctx.obj.get("verbose", False)
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
    finally:
        # Background threads (HTTP calls to LLM APIs) may still be running.
        # os._exit skips atexit/thread-join to avoid the Python 3.13
        # "Exception ignored on threading shutdown" traceback on Ctrl+C.
        import os
        os._exit(0)


@main.command()
@click.option("--policy", "-p", default="sequential",
              help="Floor policy: sequential, round_robin, moderated, free_for_all")
@click.option("--agent", "-a", "agents", multiple=True, metavar="TYPE:NAME[:DESCRIPTION]",
              help="Pre-spawn an agent (e.g. anthropic:Claude:You are helpful)")
@click.option("--topic", "-t", default=None,
              help="Seed topic to start the conversation automatically")
@click.option("--no-human", is_flag=True, default=False,
              help="Watch-only mode: agents talk autonomously, UI shows the conversation")
@click.option("--max-turns", "-n", default=None, type=int,
              help="Stop automatically after N utterances")
@click.option("--human-name", default="User",
              help="Display name for the human participant (default: User)")
@click.option("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
@click.option("--port", default=7860, type=int, help="Port to listen on (default: 7860)")
@click.option("--share", is_flag=True, default=False,
              help="Create a public Gradio share link")
@click.pass_context
def web(ctx: click.Context, policy: str, agents: tuple, topic: Optional[str],
        no_human: bool, max_turns: Optional[int], human_name: str,
        host: str, port: int, share: bool):
    """Start the OFP Playground Gradio web UI.

    Opens a browser-based chat interface. Use --no-human to watch agents
    talk autonomously.
    """
    verbose = ctx.obj.get("verbose", False)
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    _load_dotenv()
    settings = Settings.load()
    floor_policy = _parse_policy(policy)

    try:
        asyncio.run(_run_web_session(
            floor_policy, agents, settings, verbose,
            topic=topic, no_human=no_human, max_turns=max_turns,
            human_name=human_name, host=host, port=port, share=share,
        ))
    except KeyboardInterrupt:
        console.print("\n[dim]Web session interrupted.[/dim]")
    finally:
        import os
        os._exit(0)


async def _run_web_session(
    policy: FloorPolicy,
    agent_specs: tuple[str, ...],
    settings: Settings,
    verbose: bool,
    topic: Optional[str] = None,
    no_human: bool = False,
    max_turns: Optional[int] = None,
    human_name: str = "User",
    host: str = "0.0.0.0",
    port: int = 7860,
    share: bool = False,
) -> None:
    """Run the web UI session (Gradio + OFP bus in the same process)."""
    from ofp_playground.renderer.gradio_ui import launch_web_session
    from ofp_playground.agents.registry import AgentRegistry

    bus = MessageBus()
    floor = FloorManager(bus, policy=policy, renderer=None)
    registry = AgentRegistry()

    human = None
    tasks = [floor.run()]
    agent_display_names: list[str] = []

    if not no_human:
        from ofp_playground.agents.web_human import WebHumanAgent
        human = WebHumanAgent(
            name=human_name,
            bus=bus,
            conversation_id=floor.conversation_id,
        )
        floor.register_agent(human.speaker_uri, human.name)
        registry.register(human)
        tasks.append(human.run())
        agent_display_names.append(human_name)

    # Spawn pre-configured LLM agents
    term_renderer = TerminalRenderer(console, show_floor_events=False)
    for spec in agent_specs:
        try:
            agent_type, name, description, model_ov, max_tokens_ov = _parse_agent_spec(spec)
            await _spawn_llm_agent(
                agent_type, name, description, floor, bus, registry,
                term_renderer, settings, model_ov, max_tokens_ov,
            )
            agent_display_names.append(name)
        except Exception as e:
            console.print(f"[red]Failed to spawn agent: {e}[/red]")

    if topic or max_turns:
        async def _orchestrate():
            await asyncio.sleep(1.5)
            if topic:
                term_renderer.show_system_event(f'Topic: "{topic}"')
                await _seed_topic(topic, floor, bus)
            if max_turns:
                while floor.history.__len__() < max_turns:
                    await asyncio.sleep(2.0)
                term_renderer.show_system_event(
                    f"Reached {max_turns} turns — stopping."
                )
                floor.stop()
        tasks.append(_orchestrate())

    # Get the running loop and launch Gradio from this coroutine's thread
    loop = asyncio.get_event_loop()

    def _launch():
        launch_web_session(
            floor=floor,
            bus=bus,
            human_agent=human,
            agent_specs_display=agent_display_names,
            policy_name=policy.value,
            loop=loop,
            host=host,
            port=port,
            share=share,
        )
        console.print(f"[green]Web UI ready → http://{host}:{port}[/green]")

    # Launch Gradio in a thread so it doesn't block the event loop
    threading.Thread(target=_launch, daemon=True).start()

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        floor.stop()


@main.command()
def agents():
    """List available agent types."""
    console.print(
        "[bold]Available agent types:[/bold]\n"
        "  [cyan]anthropic[/cyan] / claude  — Anthropic Claude (requires ANTHROPIC_API_KEY)\n"
        "  [cyan]openai[/cyan] / gpt        — OpenAI GPT (requires OPENAI_API_KEY)\n"
        "                             -type Text-to-Image uses OpenAI image models\n"
        "  [cyan]google[/cyan] / gemini     — Google Gemini (requires GOOGLE_API_KEY)\n"
        "  [cyan]hf[/cyan] / huggingface    — HuggingFace Inference API (requires HF_API_KEY)\n"
        "                             -type defaults to Text-Generation\n"
        "\n"
        "  [bold]OpenAI generative tasks (-type):[/bold]\n"
        "    Text-Generation          — chat/text LLM (default: gpt-5.4-nano)\n"
        "    Text-to-Image            — generate images via Responses API (default: gpt-4o)\n"
        "    Image-to-Text            — analyze images via vision (default: gpt-4o-mini)\n"
        "\n"
        "  [bold]Google generative tasks (-type):[/bold]\n"
        "    Text-Generation          — chat/text LLM (default: gemini-3.1-flash-lite-preview)\n"
        "    Text-to-Image            — generate images via Nano Banana (default: gemini-3.1-flash-image-preview)\n"
        "    Image-to-Text            — analyze images via Gemini vision (default: gemini-2.0-flash)\n"
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
        "  [cyan]remote[/cyan]              — Remote OFP agent via HTTP"
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
        console.print(f"[green]✓ Valid OFP envelope[/green]")
        console.print(f"  Conversation: {envelope.conversation.id if envelope.conversation else 'N/A'}")
        console.print(f"  Sender: {envelope.sender.speakerUri if envelope.sender else 'N/A'}")
        console.print(f"  Events: {len(envelope.events or [])}")
    except Exception as e:
        console.print(f"[red]✗ Invalid envelope: {e}[/red]")
        sys.exit(1)
