"""Tests for BaseLLMAgent — pure logic: prompt building, message parsing, context."""
from __future__ import annotations

import pytest

from ofp_playground.agents.llm.base import BaseLLMAgent, _uri_to_name
from ofp_playground.bus.message_bus import MessageBus


# ---------------------------------------------------------------------------
# Minimal concrete LLM agent (skips actual LLM calls)
# ---------------------------------------------------------------------------

class MockLLMAgent(BaseLLMAgent):
    """Concrete subclass with no-op LLM methods for testing pure logic."""

    async def _generate_response(self, participants):
        return "mock response"

    async def _quick_check(self, prompt: str) -> str:
        return "YES"


def _make_agent(name="TestBot", synopsis="A helpful assistant", relevance=True):
    return MockLLMAgent(
        name=name,
        synopsis=synopsis,
        bus=MessageBus(),
        conversation_id="conv:test",
        relevance_filter=relevance,
    )


# ---------------------------------------------------------------------------
# _uri_to_name helper
# ---------------------------------------------------------------------------

class TestUriToName:
    def test_extracts_last_segment(self):
        assert _uri_to_name("tag:ofp-playground.local,2025:llm-alice") == "Alice"

    def test_strips_llm_prefix(self):
        assert _uri_to_name("tag:x:llm-bob") == "Bob"

    def test_strips_human_prefix(self):
        assert _uri_to_name("tag:x:human-charlie") == "Charlie"

    def test_strips_image_prefix(self):
        assert _uri_to_name("tag:x:image-painter") == "Painter"

    def test_strips_director_prefix(self):
        assert _uri_to_name("tag:x:director-showrunner") == "Showrunner"

    def test_hyphen_to_title_case(self):
        assert _uri_to_name("tag:x:llm-my-agent") == "My Agent"

    def test_no_known_prefix(self):
        assert _uri_to_name("tag:x:custom-thing") == "Custom Thing"


# ---------------------------------------------------------------------------
# task_type property
# ---------------------------------------------------------------------------

class TestTaskType:
    def test_default_text_generation(self):
        agent = _make_agent()
        assert agent.task_type == "text-generation"


# ---------------------------------------------------------------------------
# _build_system_prompt
# ---------------------------------------------------------------------------

class TestBuildSystemPrompt:
    def test_contains_agent_name(self):
        agent = _make_agent(name="Aria")
        prompt = agent._build_system_prompt(["Aria", "Bob"])
        assert "Aria" in prompt

    def test_contains_synopsis(self):
        agent = _make_agent(synopsis="You are a pirate")
        prompt = agent._build_system_prompt([])
        assert "pirate" in prompt

    def test_contains_participants(self):
        agent = _make_agent()
        prompt = agent._build_system_prompt(["Alice", "Bob", "Charlie"])
        assert "Alice" in prompt
        assert "Bob" in prompt

    def test_empty_participants_shows_fallback(self):
        agent = _make_agent()
        prompt = agent._build_system_prompt([])
        # Should not crash and contain something sensible
        assert len(prompt) > 50

    def test_director_instruction_injected(self):
        agent = _make_agent()
        agent._current_director_instruction = "Write chapter 3"
        prompt = agent._build_system_prompt(["Alice"])
        assert "Write chapter 3" in prompt

    def test_no_director_instruction_no_task_line(self):
        agent = _make_agent()
        agent._current_director_instruction = ""
        prompt = agent._build_system_prompt(["Alice"])
        assert "YOUR TASK THIS ROUND" not in prompt

    def test_memory_store_injected_when_not_empty(self):
        from ofp_playground.memory.store import MemoryStore
        agent = _make_agent()
        ms = MemoryStore()
        ms.store("decisions", "choice", "Use Anthropic for text agents")
        agent._memory_store = ms
        prompt = agent._build_system_prompt(["Alice"])
        assert "SESSION MEMORY" in prompt
        assert "Use Anthropic" in prompt

    def test_memory_store_not_injected_when_empty(self):
        from ofp_playground.memory.store import MemoryStore
        agent = _make_agent()
        agent._memory_store = MemoryStore()  # empty
        prompt = agent._build_system_prompt(["Alice"])
        assert "SESSION MEMORY" not in prompt


# ---------------------------------------------------------------------------
# _parse_director_message
# ---------------------------------------------------------------------------

class TestParseDirectorMessage:
    def test_assigned_returns_true(self):
        agent = _make_agent(name="Alice")
        text = "[SCENE] Opening scene\n[Alice] Write the introduction."
        assert agent._parse_director_message(text) is True

    def test_not_assigned_returns_false(self):
        agent = _make_agent(name="Alice")
        text = "[SCENE] Opening scene\n[Bob] Write the introduction."
        assert agent._parse_director_message(text) is False

    def test_assignment_stored_in_instruction(self):
        agent = _make_agent(name="Alice")
        agent._parse_director_message("[SCENE] Context\n[Alice] Craft the opening paragraph.")
        assert "Craft the opening paragraph" in agent._current_director_instruction

    def test_scene_context_appended(self):
        agent = _make_agent(name="Alice")
        agent._parse_director_message("[SCENE] A dark forest\n[Alice] Describe the scene.")
        assert "dark forest" in agent._current_director_instruction

    def test_case_insensitive_match(self):
        agent = _make_agent(name="Alice")
        # Name in directive might vary in capitalisation
        assert agent._parse_director_message("[SCENE] x\n[alice] do something") is True

    def test_stale_instruction_cleared_when_not_assigned(self):
        agent = _make_agent(name="Alice")
        agent._current_director_instruction = "old stale task"
        agent._parse_director_message("[SCENE] x\n[Bob] do Bob's thing")
        assert agent._current_director_instruction == ""

    def test_no_scene_line_still_works(self):
        agent = _make_agent(name="Bob")
        assert agent._parse_director_message("[Bob] Write your part now.") is True
        assert "Write your part now" in agent._current_director_instruction


# ---------------------------------------------------------------------------
# _parse_showrunner_message
# ---------------------------------------------------------------------------

class TestParseShowrunnerMessage:
    def test_assigned_returns_true(self):
        agent = _make_agent(name="Writer")
        text = "[DIRECTIVE for Writer]: Please draft chapter 2 now."
        assert agent._parse_showrunner_message(text) is True

    def test_not_assigned_returns_false(self):
        agent = _make_agent(name="Writer")
        text = "[DIRECTIVE for Painter]: Create an illustration."
        assert agent._parse_showrunner_message(text) is False

    def test_instruction_stored(self):
        agent = _make_agent(name="Writer")
        agent._parse_showrunner_message("[DIRECTIVE for Writer]: Write chapter 2 now.")
        assert "Write chapter 2 now" in agent._current_director_instruction

    def test_case_insensitive(self):
        agent = _make_agent(name="Writer")
        assert agent._parse_showrunner_message("[DIRECTIVE for writer]: task") is True

    def test_stale_cleared_when_not_assigned(self):
        agent = _make_agent(name="Writer")
        agent._current_director_instruction = "stale"
        agent._parse_showrunner_message("[DIRECTIVE for Painter]: paint something")
        assert agent._current_director_instruction == ""

    def test_multiline_directive_captured(self):
        agent = _make_agent(name="Writer")
        text = "[DIRECTIVE for Writer]: Write a detailed backstory\nwith multiple sentences."
        agent._parse_showrunner_message(text)
        assert "detailed backstory" in agent._current_director_instruction
        # The continuation line (and any context the floor appends) must survive.
        assert "with multiple sentences." in agent._current_director_instruction


# ---------------------------------------------------------------------------
# _append_to_context
# ---------------------------------------------------------------------------

class TestAppendToContext:
    def test_own_message_stored_as_assistant(self):
        agent = _make_agent(name="Alice")
        agent._append_to_context("Alice", "Hello!", is_self=True)
        assert agent._conversation_history[-1]["role"] == "assistant"

    def test_other_message_stored_as_user(self):
        agent = _make_agent(name="Alice")
        agent._append_to_context("Bob", "Hello!", is_self=False)
        assert agent._conversation_history[-1]["role"] == "user"

    def test_other_message_includes_speaker_prefix(self):
        agent = _make_agent(name="Alice")
        agent._append_to_context("Bob", "Hello!", is_self=False)
        content = agent._conversation_history[-1]["content"]
        assert "Bob" in content
        assert "Hello!" in content

    def test_own_message_no_prefix(self):
        agent = _make_agent(name="Alice")
        agent._append_to_context("Alice", "My contribution", is_self=True)
        content = agent._conversation_history[-1]["content"]
        # Own message should contain just the text, not "Alice: My contribution"
        assert content == "My contribution"

    def test_appended_to_pending_context(self):
        agent = _make_agent()
        agent._append_to_context("Bob", "hi", is_self=False)
        assert len(agent._pending_context) == 1

    def test_multiple_appends_accumulate(self):
        agent = _make_agent()
        for i in range(5):
            agent._append_to_context(f"Agent{i}", f"msg{i}", is_self=False)
        assert len(agent._conversation_history) == 5


# ---------------------------------------------------------------------------
# set_name_registry / set_memory_store
# ---------------------------------------------------------------------------

class TestRegistryAndMemory:
    def test_set_name_registry(self):
        agent = _make_agent()
        registry = {"tag:test:alice": "Alice", "tag:test:bob": "Bob"}
        agent.set_name_registry(registry)
        assert agent._name_registry == registry

    def test_set_memory_store(self):
        from ofp_playground.memory.store import MemoryStore
        agent = _make_agent()
        ms = MemoryStore()
        agent.set_memory_store(ms)
        assert agent._memory_store is ms
