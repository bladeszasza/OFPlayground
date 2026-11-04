"""Narrative Director agent — speaks first each round and assigns per-agent tasks."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable

from openfloor import Envelope

from ofp_playground.agents.llm.base import _uri_to_name
from ofp_playground.agents.llm.huggingface import HuggingFaceAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

DIRECTOR_SYSTEM_PROMPT = """You are {name}, the NARRATIVE DIRECTOR of a collaborative multi-agent story.

Story outline — follow this exactly, one part per round:
{story_outline}

Agents you are directing:
{agent_list}

YOUR ONLY JOB EACH ROUND:
Write a directing message in this EXACT format — nothing else:

[SCENE] One sentence setting the scene for Part {part_num} of {total_parts}.
[AgentName] One specific instruction for that agent, max 15 words.
[AgentName] One specific instruction for that agent, max 15 words.
(one [AgentName] line per agent in the list above)

RULES:
- Be concrete: "Write Goku teaching Michelangelo a ki blast" beats "continue the story".
- Stay strictly inside the story outline for this part. Do NOT invent new elements.
- Each [AgentName] instruction must be under 15 words.
- Do NOT write any story content yourself. ONLY direct.
- Vary each agent's assignment every round — no two rounds should feel the same.
- On the FINAL part (Part {total_parts}), append exactly this line on its own: [STORY COMPLETE]
"""


class DirectorAgent(HuggingFaceAgent):
    """LLM-powered narrative director.

    Speaks first in each round (floor granted by FloorManager at round boundaries).
    Assigns a specific task to every story agent. Signals story completion.
    """

    def __init__(
        self,
        name: str,
        story_outline: str,
        total_parts: int,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str,
        stop_callback: Callable | None = None,
    ) -> None:
        super().__init__(
            name=name,
            synopsis="Narrative director — assigns story beats to agents each round.",
            bus=bus,
            conversation_id=conversation_id,
            api_key=api_key,
            model=model,
            relevance_filter=False,
        )
        self._story_outline = story_outline
        self._total_parts = total_parts
        self._current_part = 0
        self._stop_callback = stop_callback

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _story_agent_names(self) -> list[str]:
        """Names of registered story agents (excludes self, media agents, floor-manager)."""
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
        return DIRECTOR_SYSTEM_PROMPT.format(
            name=self._name,
            story_outline=self._story_outline,
            agent_list=agent_list,
            part_num=self._current_part,
            total_parts=self._total_parts,
        )

    # ------------------------------------------------------------------
    # Floor event overrides
    # ------------------------------------------------------------------

    async def _handle_utterance(self, envelope: Envelope) -> None:
        """Record context but never request floor — wait to be granted at round boundary."""
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return
        text = self._extract_text_from_envelope(envelope)
        if not text:
            return
        # Skip Director-format messages (shouldn't loop back to itself)
        if text.strip().startswith("[SCENE]"):
            return
        sender_name = self._name_registry.get(sender_uri) or _uri_to_name(sender_uri)
        self._append_to_context(sender_name, text, is_self=False)
        # Director never requests floor on its own — FloorManager grants it between rounds.

    async def _handle_grant_floor(self) -> None:
        """Floor granted at round boundary — generate direction for next part."""
        self._has_floor = True
        self._current_part += 1
        try:
            if self._current_part > self._total_parts:
                finale = (
                    f"[STORY COMPLETE] All {self._total_parts} parts are told. "
                    f"Outstanding work, everyone!"
                )
                await self.send_envelope(self._make_utterance_envelope(finale))
                logger.info("[%s] story complete after %d parts", self._name, self._total_parts)
                if self._stop_callback:
                    self._stop_callback()
                return

            response = await self._generate_response([])
            if response:
                self._append_to_context(self._name, response, is_self=True)
                await self.send_envelope(self._make_utterance_envelope(response))
                self._consecutive_errors = 0
                # If the LLM included the completion marker on the final part, stop
                if ("[STORY COMPLETE]" in response and self._current_part >= self._total_parts
                        and self._stop_callback):
                    self._stop_callback()
        except Exception:
            logger.exception('[%s] director error: see traceback', self._name)
        finally:
            self._has_floor = False
            await self.yield_floor()


def parse_total_parts(description: str, default: int = 6) -> int:
    """Extract part count from a story outline string (e.g. '6 PARTS ...')."""
    m = re.search(r"(\d+)\s*PARTS?", description, re.IGNORECASE)
    return int(m.group(1)) if m else default
