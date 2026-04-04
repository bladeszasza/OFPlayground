"""Show Runner agent — speaks last each round, synthesizes and directs next round."""
from __future__ import annotations

import logging
from typing import Callable, Optional

from openfloor import Envelope

from ofp_playground.agents.llm.base import _uri_to_name
from ofp_playground.agents.llm.anthropic import AnthropicAgent
from ofp_playground.agents.llm.google import GoogleAgent
from ofp_playground.agents.llm.huggingface import HuggingFaceAgent
from ofp_playground.agents.llm.openai import OpenAIAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

SHOWRUNNER_SYSTEM_PROMPT = """You are {name}, the SHOW RUNNER of a collaborative multi-agent story.

Story agents you are directing:
{agent_list}

YOUR JOB: After each round of storytelling, synthesize what happened and direct the next round.
Output EXACTLY this format — nothing else:

STORY SO FAR: [one paragraph, max 60 words, summarizing everything canonical, resolving contradictions]

[DIRECTIVE for AgentName]: [concrete instruction, max 15 words, for what to write next]
... (one [DIRECTIVE for Name] line per agent listed above)
[IMAGE]: [clean visual scene description, max 20 words, most visually striking moment this round]

STRICT RULES:
- No preamble, no commentary, no explanations.
- STORY SO FAR must be internally consistent — pick one version if agents contradicted each other.
- Each [DIRECTIVE for Name] must name a concrete action or dialogue beat, under 15 words.
- [IMAGE] is a pure visual description: no meta-text, no instructions, no character name lists.
- Do NOT write story content yourself. ONLY synthesize and direct.
- On the FINAL part (Part {total_parts}), append exactly this line on its own: [STORY COMPLETE]
"""


class ShowRunnerAgent(HuggingFaceAgent):
    """Synthesis agent that speaks LAST each round.

    Reads all round contributions, outputs:
    - STORY SO FAR: canonical summary
    - [DIRECTIVE for Name]: per-agent instruction for next round
    - [IMAGE]: clean prompt for Canvas

    Never requests floor on utterances — FloorManager grants at round boundary.
    """

    def __init__(
        self,
        name: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str,
        stop_callback: Optional[callable] = None,
        total_parts: int = 6,
    ) -> None:
        super().__init__(
            name=name,
            synopsis="Show runner — synthesizes round output and directs next round.",
            bus=bus,
            conversation_id=conversation_id,
            api_key=api_key,
            model=model,
            relevance_filter=False,
        )
        self._stop_callback = stop_callback
        self._total_parts = total_parts
        self._current_part = 0

    @property
    def _story_agent_names(self) -> list[str]:
        """Names of story agents (excludes self, media agents, floor-manager)."""
        names = []
        for uri, name in self._name_registry.items():
            if uri == self.speaker_uri:
                continue
            if any(k in uri for k in ("image", "video", "audio", "floor-manager")):
                continue
            names.append(name)
        return names

    def _build_system_prompt(self, participants: list[str]) -> str:
        agent_list = "\n".join(f"- {n}" for n in self._story_agent_names)
        if not agent_list:
            agent_list = "- (story agents joining...)"
        return SHOWRUNNER_SYSTEM_PROMPT.format(
            name=self._name,
            agent_list=agent_list,
            total_parts=self._total_parts,
        )

    async def _handle_utterance(self, envelope: Envelope) -> None:
        """Record context but never request floor — wait to be granted at round boundary."""
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return
        text = self._extract_text_from_envelope(envelope)
        if not text:
            return
        # Skip ShowRunner-format messages looping back or director-style messages
        if "[DIRECTIVE for" in text or text.strip().startswith("[SCENE]"):
            return
        # Skip media agent utterances
        if any(k in sender_uri for k in ("image", "video", "audio")):
            return
        sender_name = self._name_registry.get(sender_uri) or _uri_to_name(sender_uri)
        self._append_to_context(sender_name, text, is_self=False)
        # ShowRunner never requests floor on its own

    async def _handle_grant_floor(self) -> None:
        """Floor granted at round boundary — synthesize and direct next round."""
        self._has_floor = True
        self._current_part += 1
        try:
            if self._total_parts and self._current_part > self._total_parts:
                finale = (
                    f"STORY SO FAR: The story is complete after {self._total_parts} rounds. "
                    f"Outstanding work, everyone!\n\n[STORY COMPLETE]"
                )
                await self.send_envelope(self._make_utterance_envelope(finale))
                logger.info("[%s] story complete after %d parts", self._name, self._total_parts)
                if self._stop_callback:
                    self._stop_callback()
                return

            response = await self._call_with_retry(lambda: self._generate_response([]))
            if response:
                self._append_to_context(self._name, response, is_self=True)
                await self.send_envelope(self._make_utterance_envelope(response))
                self._consecutive_errors = 0
                if "[STORY COMPLETE]" in response and self._current_part >= self._total_parts:
                    if self._stop_callback:
                        self._stop_callback()
        except Exception as e:
            logger.error("[%s] showrunner error: %s", self._name, e, exc_info=True)
        finally:
            self._has_floor = False
            await self.yield_floor()


# ============================================================
# Orchestrator agents — for SHOWRUNNER_DRIVEN floor policy
# ============================================================

# Legacy free-text spawn syntax — kept for reference and as fallback documentation.
ORCHESTRATOR_SYSTEM_PROMPT = """You are {name}, an intelligent project manager orchestrating a team of AI agents.

Your team:
{agent_list}

YOUR MISSION: {mission}

You will receive the conversation topic on your first turn. On every subsequent turn you will see the latest output from your assigned agent.

Respond ONLY with structured directives — one per line, no preamble, no commentary:

    [ASSIGN AgentName]: <concrete task, max 25 words>
    [ACCEPT]
    [REJECT AgentName]: <reason for revision, max 20 words>
    [KICK AgentName]
    [SKIP AgentName]: <reason — note why task was skipped>
    [SPAWN -provider <provider> -name <Name> -type <type> -system <system prompt> -model <model-id>]
    [TASK_COMPLETE]

SPAWN AGENT TYPES — use -type to select the right specialist:

  Text agents (provider: hf, anthropic, openai, google) — default type is text-generation, omit -type:
    [SPAWN -provider hf -name Writer -system <prompt>]
      default model: meta-llama/Llama-3.2-1B-Instruct

  Image generation (creates image files from text prompts):
    [SPAWN -provider hf -name Painter -type Text-to-Image -system <style description>]
      default model: black-forest-labs/FLUX.1-dev

  Video generation (creates video files from text prompts):
    [SPAWN -provider hf -name Filmmaker -type Text-to-Video -system <style description>]
      default model: Wan-AI/Wan2.2-TI2V-5B

  Music generation (Google Lyria):
    [SPAWN -provider google -name Composer -type Text-to-Music -system <style description>]

You may omit -model to use the default for that type.

RULES:
- On your FIRST turn: analyze the mission, silently plan your task list, then issue [ASSIGN] to the first agent.
- After each agent speaks: evaluate their output, then issue [ACCEPT] followed by the next [ASSIGN], OR issue [REJECT AgentName]: reason.
- Assign to EXACTLY ONE agent at a time. Never assign multiple simultaneously.
- [ASSIGN] must name a concrete deliverable.
- RESILIENCE — when an agent fails to deliver:
    1st failure: [REJECT AgentName]: clearer instructions (rephrase the task concisely).
    2nd failure: [KICK AgentName] then [SPAWN -provider <different-provider> -name <NewName>] and immediately [ASSIGN NewName]: same task with clearer wording.
    3rd failure (new agent also fails): [SKIP OriginalAgentName]: could not complete — <brief reason>. Move on.
- [KICK] only if an agent is unresponsive or persistently wrong type for the job.
- [SKIP] records the task as skipped in the manuscript so the final output is still complete.
- [SPAWN] to add a specialist you need but don't have. IMMEDIATELY follow every [SPAWN] with an [ASSIGN NewAgentName] directive.
- NEVER [SPAWN] an agent whose name already appears in your team list above — assign to them instead.
- [TASK_COMPLETE] when every piece of the mission is done and the final product is assembled.
- NEVER write story, creative, or prose content yourself. You only direct.
"""

# System prompt for tool-calling orchestrators — all four providers use this.
TOOL_ORCHESTRATOR_SYSTEM_PROMPT = """You are {name}, an intelligent project manager orchestrating a team of AI agents.

Your team:
{agent_list}

YOUR MISSION: {mission}

You will receive the conversation topic on your first turn. On every subsequent turn you will see the latest output from your assigned agent.

To add a new specialist, call one of the spawn_* tools — each tool requires an initial_task so the agent gets work the moment it joins. Do not write [SPAWN ...] text yourself.

MEMORY TOOLS: Use store_memory and recall_memory to track session knowledge.
- store_memory: Record key decisions, agent performance notes, task status, lessons, and goal refinements.
- recall_memory: Retrieve specific memories by category or key when you need more detail.
- The full memory summary is auto-injected below each turn — you rarely need recall_memory explicitly.
- Workers can write memories using [REMEMBER category]: content in their output text.

BREAKOUT SESSIONS: Use create_breakout_session to spin up a temporary sub-floor.
- A breakout is a short, focused discussion between 2+ freshly-spawned agents under their own floor policy.
- Use breakouts for: focused deliberation, brainstorming, peer review, or side-discussions.
- The breakout runs independently; when it completes a summary is returned to you.
- Breakouts CANNOT nest — they run one level deep only. Each breakout gets its own agents.
- Choose the right policy for the breakout (sequential, round_robin, moderated, free_for_all).
- Do NOT call create_breakout_session and [ASSIGN] in the same turn — wait for the breakout summary first.
- Do NOT repeat breakout topics that have already been completed — check the SESSION MEMORY below for completed breakouts. The system will reject duplicates.
- Once all required research breakouts are done, move on to the next phase of your mission. Do not keep cycling through the same topics.

For all other control, respond ONLY with structured directives — one per line, no preamble, no commentary:

    [ASSIGN AgentName]: <concrete task, max 25 words>
    [ACCEPT]
    [REJECT AgentName]: <reason for revision, max 20 words>
    [KICK AgentName]
    [SKIP AgentName]: <reason — note why task was skipped>
    [TASK_COMPLETE]

RULES:
- On your FIRST turn: analyze the mission, silently plan your task list, then spawn or assign the first agent.
- After each agent speaks: evaluate their output, then issue [ACCEPT] followed by the next [ASSIGN], OR issue [REJECT AgentName]: reason.
- Assign to EXACTLY ONE agent at a time. Never assign multiple simultaneously.
- [ASSIGN] must name a concrete deliverable — "Write 2 paragraphs introducing Gerald discovering pigeons, dry British tone, 80 words" not "write the intro".
- RESILIENCE — when an agent fails to deliver:
    1st failure: [REJECT AgentName]: clearer instructions (rephrase the task concisely).
    2nd failure: use a spawn tool to create a replacement with a different provider, then [ASSIGN NewName]: same task.
    3rd failure (replacement also fails): [SKIP OriginalAgentName]: could not complete — <brief reason>. Move on.
- [KICK] only if an agent is unresponsive or persistently wrong type for the job.
- [SKIP] records the task as skipped in the manuscript so the final output is still complete.
- NEVER spawn an agent whose name already appears in your team list above — assign to them instead.
- [TASK_COMPLETE] when every piece of the mission is done and the final product is assembled.
- NEVER write story, creative, or prose content yourself. You only direct.
{memory_section}"""


class _OrchestratorBase:
    """Mixin providing shared orchestration logic for all provider orchestrators.

    Must be listed BEFORE the provider base class in the MRO so its methods
    override BaseLLMAgent defaults:

        class MyOrchestrator(_OrchestratorBase, MyProviderAgent): ...
    """

    _URI_TYPE_LABELS = {
        "image-": "image generation",
        "video-": "video generation",
        "audio-": "audio generation",
        "multimodal-": "vision/multimodal",
        "llm-": "text",
    }

    def set_manifest_registry(self, manifests: dict) -> None:
        self._manifest_registry = manifests

    def set_memory_store(self, store) -> None:
        """Attach the shared session MemoryStore (set by FloorManager on agent registration)."""
        self._memory_store = store

    def _resolve_name_in_registry(self, name: str) -> Optional[str]:
        """Return the speakerUri if *name* (or its underscore variant) is already on the floor.

        Comparison is case-insensitive with underscores treated as spaces so that
        an orchestrator asking for ``Analyst`` or ``Wikipedia_Research_Specialist``
        correctly finds an already-registered agent.
        """
        normalized = name.lower().replace("_", " ")
        for uri, registered in self._name_registry.items():
            if registered.lower().replace("_", " ") == normalized:
                return uri
        return None

    def _spawn_or_assign(self, tool_name: str, args: dict) -> str:
        """Convert a spawn tool call to a directive string.

        * If the named agent is **already on the floor**: return only ``[ASSIGN name]: task``
          so no duplicate spawn attempt is emitted.
        * If the agent is **new**: return the full ``[SPAWN ...]\n[ASSIGN ...]`` directive.
        """
        from ofp_playground.agents.llm.spawn_tools import tool_use_to_directives
        agent_name = args.get("name", "")
        initial_task = args.get("initial_task", "")
        if agent_name and self._resolve_name_in_registry(agent_name):
            # Already on the floor — only issue an assignment, skip the spawn
            return f"[ASSIGN {agent_name}]: {initial_task}" if initial_task else ""
        return tool_use_to_directives(tool_name, args)

    def _agent_type_label(self, uri: str) -> str:
        tail = uri.split(":")[-1]
        for prefix, label in self._URI_TYPE_LABELS.items():
            if tail.startswith(prefix):
                return label
        return "text"

    def _build_agent_list(self) -> str:
        lines = []
        for uri, name in self._name_registry.items():
            if uri == self.speaker_uri or "floor-manager" in uri:
                continue
            type_label = self._agent_type_label(uri)
            manifest = getattr(self, "_manifest_registry", {}).get(uri)
            if manifest:
                caps = [kp for cap in (manifest.capabilities or []) for kp in (cap.keyphrases or [])]
                role = (manifest.identification.role or "")[:80]
                detail_parts = []
                if caps:
                    detail_parts.append(f"capabilities: {', '.join(caps)}")
                if role:
                    detail_parts.append(f"role: {role}")
                detail = " | ".join(detail_parts)
                lines.append(f"- {name} ({type_label}) — {detail}" if detail else f"- {name} ({type_label})")
            else:
                lines.append(f"- {name} ({type_label})")
        return "\n".join(lines) if lines else "- (agents joining...)"

    def _build_system_prompt(self, participants: list[str]) -> str:
        memory_store = getattr(self, "_memory_store", None)
        memory_section = ""
        if memory_store and not memory_store.is_empty():
            summary = memory_store.get_summary(max_chars=1500)
            memory_section = f"\n\n--- SESSION MEMORY ---\n{summary}\n---"
        return TOOL_ORCHESTRATOR_SYSTEM_PROMPT.format(
            name=self._name,
            agent_list=self._build_agent_list(),
            mission=self._mission,
            memory_section=memory_section,
        )

    async def _handle_utterance(self, envelope: "Envelope") -> None:
        """Record worker output into context. Never request floor — FloorManager grants it reactively."""
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return
        text = self._extract_text_from_envelope(envelope)
        if not text:
            return
        _MEDIA_URI_KEYS = ("image-", "video-", "audio-")
        uri_tail = sender_uri.split(":")[-1]
        media_type: str | None = None
        for prefix in _MEDIA_URI_KEYS:
            if uri_tail.startswith(prefix):
                media_type = prefix.rstrip("-")
                break
        if media_type is not None:
            # Inform orchestrator the output was auto-accepted so it doesn't re-issue [ASSIGN]
            sender_name = self._name_registry.get(sender_uri) or _uri_to_name(sender_uri)
            self._append_to_context(
                sender_name, f"[auto-accepted {media_type} output]: {text}", is_self=False
            )
            return
        sender_name = self._name_registry.get(sender_uri) or _uri_to_name(sender_uri)
        self._append_to_context(sender_name, text, is_self=False)

    async def _handle_grant_floor(self) -> None:
        """Floor granted by FloorManager — issue next directive based on current context."""
        self._has_floor = True
        try:
            response = await self._call_with_retry(lambda: self._generate_response([]))
            if response:
                self._append_to_context(self._name, response, is_self=True)
                await self.send_envelope(self._make_utterance_envelope(response))
                self._consecutive_errors = 0
                if "[TASK_COMPLETE]" in response:
                    if self._stop_callback:
                        self._stop_callback()
        except Exception as e:
            logger.error("[%s] orchestrator error: %s", self._name, e, exc_info=True)
        finally:
            self._has_floor = False
            await self.yield_floor()


class OrchestratorAgent(_OrchestratorBase, HuggingFaceAgent):
    """Intelligent project manager for SHOWRUNNER_DRIVEN (HuggingFace-backed).

    Uses HuggingFace chat completions with tool calling when spawn tools
    are available, falling back gracefully when none are configured.
    """

    def __init__(
        self,
        name: str,
        mission: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str,
        stop_callback: Optional[Callable] = None,
        settings=None,
    ) -> None:
        super().__init__(
            name=name,
            synopsis="Orchestrator — manages agents via structured directives.",
            bus=bus,
            conversation_id=conversation_id,
            api_key=api_key,
            model=model,
            relevance_filter=False,
        )
        self._mission = mission
        self._stop_callback = stop_callback
        self._manifest_registry: dict = {}
        self._settings = settings

    async def _generate_response(self, participants: list[str]) -> Optional[str]:
        import asyncio
        import json
        loop = asyncio.get_event_loop()
        from ofp_playground.agents.llm.spawn_tools import build_spawn_tools, to_hf_tools, tool_use_to_directives
        from ofp_playground.memory.tools import build_memory_tools, execute_memory_tool
        from ofp_playground.floor.breakout_tools import build_breakout_tools, tool_use_to_breakout_directive

        system = self._build_system_prompt([])
        messages = [{"role": "system", "content": system}] + list(self._conversation_history)
        if len(messages) == 1:
            messages.append({"role": "user", "content": "Begin your mission."})

        # Prepend /no_think to suppress chain-of-thought on reasoning models
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "user":
                messages[i] = {**messages[i], "content": "/no_think\n" + messages[i]["content"]}
                break

        spawn_tools = build_spawn_tools(self._settings) if self._settings else []
        memory_store = getattr(self, "_memory_store", None)
        mem_tools = build_memory_tools() if memory_store else []
        breakout_tools = build_breakout_tools(self._settings) if self._settings else []
        all_tools = spawn_tools + mem_tools + breakout_tools
        hf_tools = to_hf_tools(all_tools) if all_tools else None

        def _call(msgs):
            client = self._get_client()
            kwargs: dict = {
                "model": self._model,
                "max_tokens": self._max_tokens,
                "messages": msgs,
            }
            if hf_tools:
                kwargs["tools"] = hf_tools
                kwargs["tool_choice"] = "auto"
            return client.chat.completions.create(**kwargs)

        response = await loop.run_in_executor(None, lambda: _call(messages))

        spawn_directives: list[str] = []
        final_text = ""
        choice = response.choices[0].message

        if choice.tool_calls:
            # Collect HF tool call objects for potential recall loop
            recall_results: list[tuple] = []  # (tc, result_str)
            for tc in choice.tool_calls:
                args = json.loads(tc.function.arguments)
                name = tc.function.name
                if name in ("store_memory", "recall_memory") and memory_store:
                    result = execute_memory_tool(name, args, memory_store, self._name)
                    if name == "recall_memory":
                        recall_results.append((tc, result))
                    # store_memory: fire-and-forget, result logged but not fed back
                elif name == "create_breakout_session":
                    directive = tool_use_to_breakout_directive(args)
                    if directive:
                        spawn_directives.append(directive)
                else:
                    directive = self._spawn_or_assign(name, args)
                    if directive:
                        spawn_directives.append(directive)

            # If recall was requested, feed results back and call once more
            if recall_results:
                # Append assistant message (with tool_calls) then tool result messages
                asst_msg: dict = {
                    "role": "assistant",
                    "content": choice.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc, _ in recall_results
                    ],
                }
                messages = messages + [asst_msg] + [
                    {"role": "tool", "tool_call_id": tc.id, "content": result}
                    for tc, result in recall_results
                ]
                response2 = await loop.run_in_executor(None, lambda: _call(messages))
                choice2 = response2.choices[0].message
                if choice2.tool_calls:
                    for tc in choice2.tool_calls:
                        args = json.loads(tc.function.arguments)
                        directive = self._spawn_or_assign(tc.function.name, args)
                        if directive:
                            spawn_directives.append(directive)
                if choice2.content:
                    final_text = self._clean(choice2.content)
            else:
                if choice.content:
                    final_text = self._clean(choice.content)
        else:
            if choice.content:
                final_text = self._clean(choice.content)

        text_parts = spawn_directives + ([final_text] if final_text else [])
        return "\n".join(text_parts) or None


class AnthropicOrchestratorAgent(_OrchestratorBase, AnthropicAgent):
    """Intelligent project manager backed by Anthropic Claude.

    Uses Anthropic native tool calling for spawning agents.
    """

    def __init__(
        self,
        name: str,
        mission: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str,
        stop_callback: Optional[Callable] = None,
        settings=None,
    ) -> None:
        super().__init__(
            name=name,
            synopsis="Orchestrator — manages agents via structured directives.",
            bus=bus,
            conversation_id=conversation_id,
            api_key=api_key,
            model=model,
            relevance_filter=False,
        )
        self._mission = mission
        self._stop_callback = stop_callback
        self._manifest_registry: dict = {}
        self._settings = settings

    async def _generate_response(self, participants: list[str]) -> Optional[str]:
        import asyncio
        loop = asyncio.get_event_loop()
        from ofp_playground.agents.llm.spawn_tools import build_spawn_tools, tool_use_to_directives
        from ofp_playground.memory.tools import build_memory_tools, execute_memory_tool
        from ofp_playground.floor.breakout_tools import build_breakout_tools, tool_use_to_breakout_directive

        client = self._get_client()
        system = self._build_system_prompt([])
        messages = list(self._conversation_history)
        if not messages:
            messages = [{"role": "user", "content": "Begin your mission."}]

        spawn_tools = build_spawn_tools(self._settings) if self._settings else []
        memory_store = getattr(self, "_memory_store", None)
        mem_tools = build_memory_tools() if memory_store else []
        breakout_tools = build_breakout_tools(self._settings) if self._settings else []
        all_tools = spawn_tools + mem_tools + breakout_tools

        kwargs: dict = {
            "model": self._model,
            "max_tokens": max(self._max_tokens, 8048),
            "system": system,
            "messages": messages,
        }
        if all_tools:
            kwargs["tools"] = all_tools
            kwargs["tool_choice"] = {"type": "auto"}

        def _call(kw):
            return client.messages.create(**kw)

        response = await loop.run_in_executor(None, lambda: _call(kwargs))

        spawn_directives: list[str] = []
        final_text = ""
        recall_blocks: list[tuple] = []  # (block, result_str)

        for block in response.content:
            if block.type == "text":
                final_text = block.text.strip()
            elif block.type == "tool_use":
                if block.name in ("store_memory", "recall_memory") and memory_store:
                    result = execute_memory_tool(block.name, block.input, memory_store, self._name)
                    if block.name == "recall_memory":
                        recall_blocks.append((block, result))
                elif block.name == "create_breakout_session":
                    directive = tool_use_to_breakout_directive(block.input)
                    if directive:
                        spawn_directives.append(directive)
                else:
                    directive = self._spawn_or_assign(block.name, block.input)
                    if directive:
                        spawn_directives.append(directive)

        # Feed recall results back for a second Anthropic call.
        # Include tool_results for ALL tool_use blocks — Anthropic rejects
        # mismatched tool_use/tool_result pairs.
        if recall_blocks:
            recall_ids = {blk.id for blk, _ in recall_blocks}
            all_tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if block.id in recall_ids:
                        result = next(r for b, r in recall_blocks if b.id == block.id)
                        all_tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
                    else:
                        all_tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": "OK"})
            kwargs2 = dict(kwargs)
            kwargs2["messages"] = list(messages) + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": all_tool_results},
            ]
            response2 = await loop.run_in_executor(None, lambda: _call(kwargs2))
            for block in response2.content:
                if block.type == "text" and block.text.strip():
                    final_text = block.text.strip()
                elif block.type == "tool_use":
                    if block.name in ("store_memory", "recall_memory") and memory_store:
                        execute_memory_tool(block.name, block.input, memory_store, self._name)
                    elif block.name == "create_breakout_session":
                        directive = tool_use_to_breakout_directive(block.input)
                        if directive:
                            spawn_directives.append(directive)
                    else:
                        directive = self._spawn_or_assign(block.name, block.input)
                        if directive:
                            spawn_directives.append(directive)

        text_parts = spawn_directives + ([final_text] if final_text else [])
        return "\n".join(text_parts) or None


class OpenAIOrchestratorAgent(_OrchestratorBase, OpenAIAgent):
    """Intelligent project manager backed by OpenAI.

    Uses OpenAI Responses API function calling for spawning agents.
    """

    def __init__(
        self,
        name: str,
        mission: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str,
        stop_callback: Optional[Callable] = None,
        settings=None,
    ) -> None:
        super().__init__(
            name=name,
            synopsis="Orchestrator — manages agents via structured directives.",
            bus=bus,
            conversation_id=conversation_id,
            api_key=api_key,
            model=model,
            relevance_filter=False,
        )
        self._mission = mission
        self._stop_callback = stop_callback
        self._manifest_registry: dict = {}
        self._settings = settings

    async def _generate_response(self, participants: list[str]) -> Optional[str]:
        import asyncio
        import json
        loop = asyncio.get_event_loop()
        from ofp_playground.agents.llm.spawn_tools import build_spawn_tools, to_openai_tools, tool_use_to_directives
        from ofp_playground.memory.tools import build_memory_tools, execute_memory_tool
        from ofp_playground.floor.breakout_tools import build_breakout_tools, tool_use_to_breakout_directive

        client = self._get_client()
        system = self._build_system_prompt([])
        history = list(self._conversation_history)
        if not history:
            history = [{"role": "user", "content": "Begin your mission."}]

        spawn_tools = build_spawn_tools(self._settings) if self._settings else []
        memory_store = getattr(self, "_memory_store", None)
        mem_tools = build_memory_tools() if memory_store else []
        breakout_tools = build_breakout_tools(self._settings) if self._settings else []
        all_tools = spawn_tools + mem_tools + breakout_tools
        openai_tools = to_openai_tools(all_tools) if all_tools else None

        def _call(inp):
            kwargs: dict = {
                "model": self._model,
                "instructions": system,
                "input": inp,
                "max_output_tokens": 1000,
            }
            if openai_tools:
                kwargs["tools"] = openai_tools
                kwargs["tool_choice"] = "auto"
            return client.responses.create(**kwargs)

        response = await loop.run_in_executor(None, lambda: _call(history))

        spawn_directives: list[str] = []
        final_text = ""
        recall_items: list[tuple] = []  # (item, result_str)

        for item in response.output:
            if item.type == "function_call":
                args = json.loads(item.arguments)
                if item.name in ("store_memory", "recall_memory") and memory_store:
                    result = execute_memory_tool(item.name, args, memory_store, self._name)
                    if item.name == "recall_memory":
                        recall_items.append((item, result))
                elif item.name == "create_breakout_session":
                    directive = tool_use_to_breakout_directive(args)
                    if directive:
                        spawn_directives.append(directive)
                else:
                    directive = self._spawn_or_assign(item.name, args)
                    if directive:
                        spawn_directives.append(directive)
            elif item.type == "message":
                for content_block in item.content:
                    if hasattr(content_block, "text") and content_block.text:
                        final_text = content_block.text.strip()

        # Feed recall results back for a second OpenAI Responses call
        if recall_items:
            extra_input: list = []
            for item, result in recall_items:
                # Add the function_call item itself, then the output
                fc_dict = {
                    "type": "function_call",
                    "call_id": item.call_id,
                    "name": item.name,
                    "arguments": item.arguments,
                }
                extra_input.append(fc_dict)
                extra_input.append({"type": "function_call_output", "call_id": item.call_id, "output": result})
            response2 = await loop.run_in_executor(None, lambda: _call(history + extra_input))
            for item2 in response2.output:
                if item2.type == "function_call":
                    args2 = json.loads(item2.arguments)
                    directive2 = self._spawn_or_assign(item2.name, args2)
                    if directive2:
                        spawn_directives.append(directive2)
                elif item2.type == "message":
                    for cb in item2.content:
                        if hasattr(cb, "text") and cb.text:
                            final_text = cb.text.strip()

        text_parts = spawn_directives + ([final_text] if final_text else [])
        return "\n".join(text_parts) or None


class GoogleOrchestratorAgent(_OrchestratorBase, GoogleAgent):
    """Intelligent project manager backed by Google Gemini.

    Uses Gemini function declarations for spawning agents.
    """

    def __init__(
        self,
        name: str,
        mission: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str,
        stop_callback: Optional[Callable] = None,
        settings=None,
    ) -> None:
        super().__init__(
            name=name,
            synopsis="Orchestrator — manages agents via structured directives.",
            bus=bus,
            conversation_id=conversation_id,
            api_key=api_key,
            model=model,
            relevance_filter=False,
        )
        self._mission = mission
        self._stop_callback = stop_callback
        self._manifest_registry: dict = {}
        self._settings = settings

    async def _generate_response(self, participants: list[str]) -> Optional[str]:
        import asyncio
        loop = asyncio.get_event_loop()
        from google import genai
        from google.genai import types
        from ofp_playground.agents.llm.spawn_tools import build_spawn_tools, to_google_tools, tool_use_to_directives
        from ofp_playground.memory.tools import build_memory_tools, execute_memory_tool
        from ofp_playground.floor.breakout_tools import build_breakout_tools, tool_use_to_breakout_directive

        system = self._build_system_prompt([])
        history = list(self._conversation_history)
        if not history:
            history = [{"role": "user", "content": "Begin your mission."}]

        contents = [
            types.Content(
                parts=[types.Part(text=msg["content"])],
                role="model" if msg["role"] == "assistant" else "user",
            )
            for msg in history
        ]

        spawn_tools = build_spawn_tools(self._settings) if self._settings else []
        memory_store = getattr(self, "_memory_store", None)
        mem_tools = build_memory_tools() if memory_store else []
        breakout_tools = build_breakout_tools(self._settings) if self._settings else []
        all_tools = spawn_tools + mem_tools + breakout_tools
        google_tools = to_google_tools(all_tools) if all_tools else None

        def _call(cts):
            client = genai.Client(api_key=self._api_key)
            config_kwargs: dict = {
                "system_instruction": system,
                "max_output_tokens": 1000,
            }
            if google_tools:
                config_kwargs["tools"] = google_tools
            config = types.GenerateContentConfig(**config_kwargs)
            return client.models.generate_content(
                model=self._model,
                contents=cts,
                config=config,
            )

        response = await loop.run_in_executor(None, lambda: _call(contents))

        spawn_directives: list[str] = []
        final_text = ""
        # Collect all parts from the first response
        first_response_parts = []
        recall_results: list[tuple] = []  # (name, result_str)

        for candidate in (response.candidates or []):
            for part in (candidate.content.parts or []):
                first_response_parts.append(part)
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    args = dict(fc.args)
                    if fc.name in ("store_memory", "recall_memory") and memory_store:
                        result = execute_memory_tool(fc.name, args, memory_store, self._name)
                        if fc.name == "recall_memory":
                            recall_results.append((fc.name, result))
                    elif fc.name == "create_breakout_session":
                        directive = tool_use_to_breakout_directive(args)
                        if directive:
                            spawn_directives.append(directive)
                    else:
                        directive = self._spawn_or_assign(fc.name, args)
                        if directive:
                            spawn_directives.append(directive)
                elif hasattr(part, "text") and part.text:
                    final_text = part.text.strip()

        # Feed recall results back for a second Google call
        if recall_results:
            contents2 = list(contents) + [
                types.Content(parts=first_response_parts, role="model"),
                types.Content(
                    parts=[
                        types.Part(function_response=types.FunctionResponse(
                            name=name,
                            response={"result": result},
                        ))
                        for name, result in recall_results
                    ],
                    role="user",
                ),
            ]
            response2 = await loop.run_in_executor(None, lambda: _call(contents2))
            for candidate in (response2.candidates or []):
                for part in (candidate.content.parts or []):
                    if hasattr(part, "function_call") and part.function_call:
                        directive = self._spawn_or_assign(part.function_call.name, dict(part.function_call.args))
                        if directive:
                            spawn_directives.append(directive)
                    elif hasattr(part, "text") and part.text:
                        final_text = part.text.strip()

        text_parts = spawn_directives + ([final_text] if final_text else [])
        return "\n".join(text_parts) or None
