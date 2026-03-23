"""Tests for the ephemeral session memory system."""
from __future__ import annotations

import pytest
from ofp_playground.memory.store import MemoryStore, MemoryCategory
from ofp_playground.memory.tools import (
    build_memory_tools,
    execute_memory_tool,
    parse_remember_directives,
)


# ── MemoryStore CRUD ─────────────────────────────────────────────────────────


class TestMemoryStore:
    def test_store_and_recall_by_category(self):
        ms = MemoryStore()
        ms.store("decisions", "provider_choice", "Using HF for writer agent", author="Orchestrator")
        entries = ms.recall(category="decisions")
        assert len(entries) == 1
        assert entries[0].key == "provider_choice"
        assert entries[0].content == "Using HF for writer agent"
        assert entries[0].author == "Orchestrator"

    def test_store_returns_entry_id(self):
        ms = MemoryStore()
        entry_id = ms.store("tasks", "chapter_1", "pending")
        assert isinstance(entry_id, str)
        assert len(entry_id) == 8

    def test_update_existing_entry_by_category_key(self):
        ms = MemoryStore()
        ms.store("tasks", "chapter_1", "pending")
        ms.store("tasks", "chapter_1", "complete")
        entries = ms.recall(category="tasks")
        assert len(entries) == 1
        assert entries[0].content == "complete"

    def test_update_is_case_insensitive_on_key(self):
        ms = MemoryStore()
        ms.store("tasks", "Chapter_1", "pending")
        ms.store("tasks", "chapter_1", "done")  # same key, different casing
        entries = ms.recall(category="tasks")
        assert len(entries) == 1
        assert entries[0].content == "done"

    def test_recall_by_key_substring(self):
        ms = MemoryStore()
        ms.store("agent_profiles", "Writer_quality", "good")
        ms.store("agent_profiles", "Painter_quality", "excellent")
        entries = ms.recall(key="Writer")
        assert len(entries) == 1
        assert entries[0].key == "Writer_quality"

    def test_recall_all(self):
        ms = MemoryStore()
        ms.store("decisions", "d1", "v1")
        ms.store("tasks", "t1", "v2")
        entries = ms.recall()
        assert len(entries) == 2

    def test_recall_empty_store(self):
        ms = MemoryStore()
        assert ms.recall() == []
        assert ms.recall(category="goals") == []

    def test_seed_goal(self):
        ms = MemoryStore()
        ms.seed_goal("Write a short mystery story with 3 chapters")
        entries = ms.recall(category="goals")
        assert len(entries) == 1
        assert "mystery" in entries[0].content
        assert entries[0].key == "original_goal"

    def test_seed_goal_updates_on_duplicate(self):
        ms = MemoryStore()
        ms.seed_goal("First goal")
        ms.seed_goal("Revised goal")
        entries = ms.recall(category="goals")
        assert len(entries) == 1
        assert entries[0].content == "Revised goal"

    def test_list_categories(self):
        ms = MemoryStore()
        ms.store("decisions", "d1", "v1")
        ms.store("decisions", "d2", "v2")
        ms.store("tasks", "t1", "v3")
        cats = ms.list_categories()
        assert cats["decisions"] == 2
        assert cats["tasks"] == 1
        assert "goals" not in cats

    def test_is_empty(self):
        ms = MemoryStore()
        assert ms.is_empty()
        ms.store("goals", "goal", "test")
        assert not ms.is_empty()

    def test_all_entries(self):
        ms = MemoryStore()
        ms.store("decisions", "d1", "v1")
        ms.store("tasks", "t1", "v2")
        entries = ms.all_entries()
        assert len(entries) == 2

    def test_enum_category_works(self):
        ms = MemoryStore()
        ms.store(MemoryCategory.LESSONS, "lesson_1", "Don't assign Writer to image tasks")
        entries = ms.recall(category=MemoryCategory.LESSONS)
        assert len(entries) == 1

    def test_invalid_category_raises(self):
        ms = MemoryStore()
        with pytest.raises(ValueError):
            ms.store("invalid_category", "key", "content")

    # ── Summary ──────────────────────────────────────────────────────────────

    def test_get_summary_empty(self):
        ms = MemoryStore()
        assert ms.get_summary() == ""

    def test_get_summary_contains_all_categories(self):
        ms = MemoryStore()
        ms.seed_goal("Write a novel")
        ms.store("tasks", "chapter_1", "done")
        ms.store("decisions", "use_claude", "Claude for writer")
        summary = ms.get_summary()
        assert "[GOALS]" in summary
        assert "[TASKS]" in summary
        assert "[DECISIONS]" in summary
        assert "Write a novel" in summary

    def test_get_summary_priority_goals_first(self):
        ms = MemoryStore()
        ms.store("decisions", "d1", "v1")
        ms.seed_goal("primary mission")
        summary = ms.get_summary()
        goals_pos = summary.index("[GOALS]")
        decisions_pos = summary.index("[DECISIONS]")
        assert goals_pos < decisions_pos

    def test_get_summary_truncation(self):
        ms = MemoryStore()
        for i in range(50):
            ms.store("lessons", f"lesson_{i}", "x" * 100)
        summary = ms.get_summary(max_chars=200)
        assert "truncated" in summary
        assert len(summary) <= 250  # some tolerance for the truncation message

    # ── Serialization ────────────────────────────────────────────────────────

    def test_to_dict_structure(self):
        ms = MemoryStore()
        ms.store("decisions", "key1", "value1")
        d = ms.to_dict()
        assert "entries" in d
        assert len(d["entries"]) == 1
        e = d["entries"][0]
        assert e["category"] == "decisions"
        assert e["key"] == "key1"
        assert e["content"] == "value1"
        assert "timestamp" in e
        assert "id" in e


# ── Memory tool definitions ───────────────────────────────────────────────────


class TestBuildMemoryTools:
    def test_returns_two_tools(self):
        tools = build_memory_tools()
        assert len(tools) == 2

    def test_tool_names(self):
        tools = build_memory_tools()
        names = {t["name"] for t in tools}
        assert "store_memory" in names
        assert "recall_memory" in names

    def test_store_memory_required_fields(self):
        tools = build_memory_tools()
        store = next(t for t in tools if t["name"] == "store_memory")
        required = store["input_schema"]["required"]
        assert "category" in required
        assert "key" in required
        assert "content" in required

    def test_store_memory_category_enum(self):
        tools = build_memory_tools()
        store = next(t for t in tools if t["name"] == "store_memory")
        enum_vals = store["input_schema"]["properties"]["category"]["enum"]
        assert "decisions" in enum_vals
        assert "goals" in enum_vals
        assert len(enum_vals) == 6

    def test_recall_memory_no_required_fields(self):
        tools = build_memory_tools()
        recall = next(t for t in tools if t["name"] == "recall_memory")
        assert recall["input_schema"]["required"] == []

    def test_anthropic_format_compatible(self):
        """Tool definitions must follow the Anthropic canonical format."""
        tools = build_memory_tools()
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"

    def test_compatible_with_spawn_tools_hf_converter(self):
        from ofp_playground.agents.llm.spawn_tools import to_hf_tools
        tools = build_memory_tools()
        hf = to_hf_tools(tools)
        assert len(hf) == 2
        assert hf[0]["type"] == "function"
        assert "name" in hf[0]["function"]
        assert "parameters" in hf[0]["function"]

    def test_compatible_with_spawn_tools_openai_converter(self):
        from ofp_playground.agents.llm.spawn_tools import to_openai_tools
        tools = build_memory_tools()
        openai = to_openai_tools(tools)
        assert len(openai) == 2
        assert openai[0]["type"] == "function"
        assert "name" in openai[0]
        assert "parameters" in openai[0]


# ── execute_memory_tool ──────────────────────────────────────────────────────


class TestExecuteMemoryTool:
    def test_store_memory(self):
        ms = MemoryStore()
        result = execute_memory_tool(
            "store_memory",
            {"category": "decisions", "key": "provider", "content": "Using Anthropic"},
            ms,
            author="Director",
        )
        assert "decisions" in result
        assert "provider" in result
        assert not ms.is_empty()

    def test_store_memory_missing_key(self):
        ms = MemoryStore()
        result = execute_memory_tool("store_memory", {"category": "tasks", "key": "", "content": "x"}, ms)
        assert "missing" in result.lower()

    def test_recall_memory_empty_store(self):
        ms = MemoryStore()
        result = execute_memory_tool("recall_memory", {}, ms)
        assert "empty" in result.lower() or "no memory" in result.lower()

    def test_recall_memory_all(self):
        ms = MemoryStore()
        ms.store("tasks", "ch1", "done")
        result = execute_memory_tool("recall_memory", {}, ms)
        assert "ch1" in result
        assert "done" in result

    def test_recall_memory_by_category(self):
        ms = MemoryStore()
        ms.store("tasks", "ch1", "done")
        ms.store("decisions", "choice", "use hf")
        result = execute_memory_tool("recall_memory", {"category": "tasks"}, ms)
        assert "ch1" in result
        assert "choice" not in result

    def test_recall_memory_by_key(self):
        ms = MemoryStore()
        ms.store("agent_profiles", "Writer_notes", "reliable")
        ms.store("agent_profiles", "Painter_notes", "slow")
        result = execute_memory_tool("recall_memory", {"key": "Writer"}, ms)
        assert "Writer_notes" in result
        assert "Painter" not in result

    def test_recall_memory_no_results(self):
        ms = MemoryStore()
        ms.store("tasks", "ch1", "done")
        result = execute_memory_tool("recall_memory", {"category": "goals"}, ms)
        assert "no memory" in result.lower() or "not found" in result.lower()

    def test_unknown_tool(self):
        ms = MemoryStore()
        result = execute_memory_tool("unknown_tool", {}, ms)
        assert "Unknown" in result


# ── parse_remember_directives ────────────────────────────────────────────────


class TestParseRememberDirectives:
    def test_no_directives_unchanged(self):
        ms = MemoryStore()
        text = "This is just regular prose. No memory directives here."
        result = parse_remember_directives(text, ms, "Writer")
        assert result == text
        assert ms.is_empty()

    def test_strips_remember_line(self):
        ms = MemoryStore()
        text = "Great prose output.\n[REMEMBER lessons]: Don't use passive voice.\nMore prose."
        result = parse_remember_directives(text, ms, "Writer")
        assert "[REMEMBER" not in result
        assert "Great prose output." in result
        assert "More prose." in result

    def test_stores_to_memory(self):
        ms = MemoryStore()
        text = "[REMEMBER decisions]: Use Oxford comma throughout."
        parse_remember_directives(text, ms, "Editor")
        entries = ms.recall(category="decisions")
        assert len(entries) == 1
        assert "Oxford comma" in entries[0].content
        assert entries[0].author == "Editor"

    def test_explicit_key_format(self):
        ms = MemoryStore()
        text = "[REMEMBER tasks:chapter_3_status]: chapter 3 is complete."
        parse_remember_directives(text, ms, "Writer")
        entries = ms.recall(key="chapter_3_status")
        assert len(entries) == 1
        assert entries[0].key == "chapter_3_status"

    def test_multiple_remember_directives(self):
        ms = MemoryStore()
        text = (
            "Prose line one.\n"
            "[REMEMBER lessons]: Avoid repetition.\n"
            "[REMEMBER preferences:style]: Warm, friendly tone.\n"
            "Final prose line."
        )
        result = parse_remember_directives(text, ms, "Writer")
        assert ms.list_categories().get("lessons") == 1
        assert ms.list_categories().get("preferences") == 1
        assert "[REMEMBER" not in result

    def test_invalid_category_silently_ignored(self):
        ms = MemoryStore()
        text = "[REMEMBER invalid_category]: Some content."
        # Should not raise; content stays in cleaned text (line not recognized as directive)
        result = parse_remember_directives(text, ms, "Agent")
        assert ms.is_empty()

    def test_case_insensitive_directive(self):
        ms = MemoryStore()
        text = "[remember TASKS:my_task]: task is pending"
        parse_remember_directives(text, ms, "Agent")
        entries = ms.recall(category="tasks")
        assert len(entries) == 1
