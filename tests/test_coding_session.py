"""Tests for coding sessions — tools, executor, session manager, directives."""
import pytest

from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.cli import _create_breakout_agent
from ofp_playground.floor.policy import FloorPolicy
from ofp_playground.floor.coding_session import (
    CodingSessionManager,
    CODING_FM_URI,
    DEFAULT_CODING_AGENT_TIMEOUT_SECONDS,
    _build_workspace_snapshot,
    _extract_and_save_code_blocks,
    _render_required_context_files,
)
from ofp_playground.floor.sandbox_context import (
    BROWSER_MEMORY_RELATIVE_PATH,
    SESSION_MEMORY_RELATIVE_PATH,
    sync_sandbox_context,
)
from ofp_playground.floor.coding_session_tools import (
    TodoList,
    build_file_tools,
    build_coding_session_tool,
    execute_coding_tool,
    is_file_tool,
    tool_use_to_coding_session_directive,
    _safe_resolve,
)
from ofp_playground.memory.artifact_store import ArtifactStore
from ofp_playground.memory.store import MemoryStore


# ---------------------------------------------------------------------------
# TodoList
# ---------------------------------------------------------------------------

class TestTodoList:
    def test_empty(self):
        todo = TodoList()
        rendered = todo.render()
        assert "no TODO" in rendered

    def test_add_and_render(self):
        todo = TodoList(["Create index.html", "Write CSS"])
        rendered = todo.render()
        assert "Create index.html" in rendered
        assert "Write CSS" in rendered
        assert "0/2 done" in rendered

    def test_mark_done(self):
        todo = TodoList(["A", "B", "C"])
        todo.mark_done([0, 2])
        rendered = todo.render()
        assert "[x] 0. A" in rendered
        assert "[ ] 1. B" in rendered
        assert "[x] 2. C" in rendered
        assert "2/3 done" in rendered

    def test_add_items(self):
        todo = TodoList(["A"])
        todo.add_items(["B", "C"])
        rendered = todo.render()
        assert "0/3 done" in rendered


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

class TestSafeResolve:
    def test_normal_path(self, tmp_path):
        (tmp_path / "file.txt").touch()
        resolved = _safe_resolve(tmp_path, "file.txt")
        assert resolved == (tmp_path / "file.txt").resolve()

    def test_nested_path(self, tmp_path):
        (tmp_path / "js").mkdir()
        (tmp_path / "js" / "main.js").touch()
        resolved = _safe_resolve(tmp_path, "js/main.js")
        assert resolved == (tmp_path / "js" / "main.js").resolve()

    def test_absolute_path_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="Absolute"):
            _safe_resolve(tmp_path, "/etc/passwd")

    def test_parent_traversal_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="traversal"):
            _safe_resolve(tmp_path, "../../../etc/passwd")

    def test_dot_dot_in_middle_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="traversal"):
            _safe_resolve(tmp_path, "js/../../etc/passwd")

    def test_symlink_escape_rejected(self, tmp_path):
        """Symlinks pointing outside sandbox are rejected."""
        link_path = tmp_path / "escape"
        outside = tmp_path.parent / f"{tmp_path.name}-outside"
        outside.mkdir()
        try:
            link_path.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows requires Developer Mode or symlink privileges")
            raise
        with pytest.raises(ValueError, match="escapes"):
            _safe_resolve(tmp_path, "escape/test")


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

class TestExecuteCodingTool:
    def test_list_workspace_empty(self, tmp_path):
        todo = TodoList()
        result = execute_coding_tool("list_workspace", {}, tmp_path, todo)
        assert "empty" in result

    def test_write_and_read(self, tmp_path):
        todo = TodoList()
        result = execute_coding_tool(
            "write_file",
            {"path": "hello.txt", "content": "world"},
            tmp_path,
            todo,
        )
        assert "Created" in result

        result = execute_coding_tool(
            "read_file",
            {"path": "hello.txt"},
            tmp_path,
            todo,
        )
        assert result == "world"

    def test_write_with_subdirectory(self, tmp_path):
        todo = TodoList()
        result = execute_coding_tool(
            "write_file",
            {"path": "js/main.js", "content": "console.log('hi')"},
            tmp_path,
            todo,
        )
        assert "Created" in result
        assert (tmp_path / "js" / "main.js").exists()

    def test_edit_file(self, tmp_path):
        todo = TodoList()
        execute_coding_tool(
            "write_file",
            {"path": "test.js", "content": "const x = 1;\nconst y = 2;"},
            tmp_path,
            todo,
        )
        result = execute_coding_tool(
            "edit_file",
            {"path": "test.js", "old_text": "const x = 1;", "new_text": "const x = 42;"},
            tmp_path,
            todo,
        )
        assert "Edited" in result
        content = (tmp_path / "test.js").read_text()
        assert "const x = 42;" in content
        assert "const y = 2;" in content

    def test_edit_file_not_found(self, tmp_path):
        todo = TodoList()
        result = execute_coding_tool(
            "edit_file",
            {"path": "missing.js", "old_text": "x", "new_text": "y"},
            tmp_path,
            todo,
        )
        assert "not found" in result

    def test_edit_file_no_match(self, tmp_path):
        todo = TodoList()
        (tmp_path / "test.js").write_text("hello world")
        result = execute_coding_tool(
            "edit_file",
            {"path": "test.js", "old_text": "not here", "new_text": "x"},
            tmp_path,
            todo,
        )
        assert "not found" in result

    def test_edit_file_multiple_matches(self, tmp_path):
        todo = TodoList()
        (tmp_path / "test.js").write_text("aaa\naaa\naaa")
        result = execute_coding_tool(
            "edit_file",
            {"path": "test.js", "old_text": "aaa", "new_text": "bbb"},
            tmp_path,
            todo,
        )
        assert "3 times" in result

    def test_list_workspace_with_files(self, tmp_path):
        todo = TodoList()
        (tmp_path / "index.html").write_text("<html></html>")
        (tmp_path / "js").mkdir()
        (tmp_path / "js" / "main.js").write_text("// main")
        result = execute_coding_tool("list_workspace", {}, tmp_path, todo)
        assert "index.html" in result
        assert "js/" in result
        assert "main.js" in result

    def test_update_todo(self, tmp_path):
        todo = TodoList(["Task A", "Task B"])
        result = execute_coding_tool(
            "update_todo",
            {"done_items": [0], "add_items": ["Task C"]},
            tmp_path,
            todo,
        )
        assert "TODO updated" in result
        assert "[x] 0. Task A" in result
        assert "Task C" in result

    def test_path_traversal_rejected(self, tmp_path):
        todo = TodoList()
        result = execute_coding_tool(
            "write_file",
            {"path": "../../etc/evil", "content": "bad"},
            tmp_path,
            todo,
        )
        assert "Error" in result

    def test_unknown_tool(self, tmp_path):
        todo = TodoList()
        result = execute_coding_tool("hack_system", {}, tmp_path, todo)
        assert "unknown tool" in result

    def test_is_file_tool(self):
        assert is_file_tool("read_file")
        assert is_file_tool("write_file")
        assert is_file_tool("edit_file")
        assert is_file_tool("list_workspace")
        assert is_file_tool("update_todo")
        assert not is_file_tool("create_breakout_session")
        assert not is_file_tool("spawn_text_agent")


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

class TestToolSchemas:
    def test_build_file_tools(self):
        tools = build_file_tools()
        names = {t["name"] for t in tools}
        assert "list_workspace" in names
        assert "read_file" in names
        assert "write_file" in names
        assert "edit_file" in names
        assert "update_todo" in names
        # Each tool has required schema fields
        for t in tools:
            assert "description" in t
            assert "input_schema" in t
            assert t["input_schema"]["type"] == "object"

    def test_build_coding_session_tool_no_keys(self):
        from unittest.mock import MagicMock
        settings = MagicMock()
        settings.get_anthropic_key.return_value = None
        settings.get_openai_key.return_value = None
        settings.get_google_key.return_value = None
        settings.get_huggingface_key.return_value = None
        tools = build_coding_session_tool(settings)
        assert tools == []

    def test_build_coding_session_tool_with_keys(self):
        from unittest.mock import MagicMock
        settings = MagicMock()
        settings.get_anthropic_key.return_value = "sk-test"
        settings.get_openai_key.return_value = "sk-test"
        settings.get_google_key.return_value = None
        settings.get_huggingface_key.return_value = None
        tools = build_coding_session_tool(settings)
        assert len(tools) == 1
        assert tools[0]["name"] == "create_coding_session"
        topic_schema = tools[0]["input_schema"]["properties"]["topic"]
        assert topic_schema["maxLength"] == 500
        assert "artifact_refs" in tools[0]["input_schema"]["properties"]
        assert "context_files" in tools[0]["input_schema"]["properties"]
        assert "todo_items" in tools[0]["input_schema"]["properties"]
        # Check providers enum includes available ones
        providers = tools[0]["input_schema"]["properties"]["agents"]["items"]["properties"]["provider"]["enum"]
        assert "anthropic" in providers
        assert "openai" in providers
        assert "google" not in providers

    def test_build_coding_session_tool_includes_timeout_field(self):
        from unittest.mock import MagicMock
        settings = MagicMock()
        settings.get_anthropic_key.return_value = None
        settings.get_openai_key.return_value = "sk-test"
        settings.get_google_key.return_value = None
        settings.get_huggingface_key.return_value = None
        tools = build_coding_session_tool(settings)
        agent_props = tools[0]["input_schema"]["properties"]["agents"]["items"]["properties"]
        assert "timeout" in agent_props


# ---------------------------------------------------------------------------
# Directive converter
# ---------------------------------------------------------------------------

class TestDirectiveConverter:
    def test_basic(self):
        result = tool_use_to_coding_session_directive({
            "topic": "Build a web app",
            "policy": "round_robin",
            "max_rounds": 16,
            "agents": [
                {"name": "Dev1", "provider": "openai", "system": "You are a developer"},
                {"name": "Dev2", "provider": "anthropic", "system": "You are a reviewer"},
            ],
        })
        assert "[CODING_SESSION policy=round_robin max_rounds=16 topic=Build a web app]" in result
        assert "[CODING_AGENT -provider openai -name Dev1" in result
        assert "[CODING_AGENT -provider anthropic -name Dev2" in result

    def test_with_model(self):
        result = tool_use_to_coding_session_directive({
            "topic": "test",
            "agents": [
                {"name": "A", "provider": "openai", "system": "sys", "model": "gpt-5.4"},
            ],
        })
        assert "-model gpt-5.4" in result

    def test_with_timeout(self):
        result = tool_use_to_coding_session_directive({
            "topic": "test",
            "agents": [
                {"name": "A", "provider": "openai", "system": "sys", "timeout": 1200},
            ],
        })
        assert "-timeout 1200" in result

    def test_with_context_refs(self):
        result = tool_use_to_coding_session_directive({
            "topic": "Build the runtime skeleton",
            "artifact_refs": ["geom-builder", "game-architect"],
            "context_files": ["memory/browser-sandbox-delivery.md", "textures_data.js"],
            "agents": [
                {"name": "DevAlpha", "provider": "openai", "system": "@development/threejs-developer"},
            ],
        })
        assert "[CODING_CONTEXT artifact=geom-builder]" in result
        assert "[CODING_CONTEXT artifact=game-architect]" in result
        assert "[CODING_CONTEXT file=memory/browser-sandbox-delivery.md]" in result
        assert "[CODING_CONTEXT file=textures_data.js]" in result

    def test_with_todo_items_derives_rounds(self):
        result = tool_use_to_coding_session_directive({
            "topic": "Build the runtime skeleton",
            "todo_items": ["Create index.html", "Write main.js", "Polish HUD"],
            "agents": [
                {"name": "DevAlpha", "provider": "openai", "system": "@development/threejs-developer"},
                {"name": "Animator", "provider": "openai", "system": "@development/threejs-animator"},
            ],
        })
        assert "[CODING_SESSION policy=round_robin max_rounds=6 topic=Build the runtime skeleton]" in result
        assert "[CODING_TODO Create index.html]" in result
        assert "[CODING_TODO Write main.js]" in result
        assert "[CODING_TODO Polish HUD]" in result

    def test_explicit_rounds_override_todo_derived_rounds(self):
        result = tool_use_to_coding_session_directive({
            "topic": "Build the runtime skeleton",
            "max_rounds": 9,
            "todo_items": ["Create index.html", "Write main.js", "Polish HUD"],
            "agents": [
                {"name": "DevAlpha", "provider": "openai", "system": "@development/threejs-developer"},
                {"name": "Animator", "provider": "openai", "system": "@development/threejs-animator"},
            ],
        })
        assert "[CODING_SESSION policy=round_robin max_rounds=9 topic=Build the runtime skeleton]" in result


class TestCodingAgentSpecParsing:
    def test_create_breakout_agent_applies_timeout_override(self):
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.get_openai_key.return_value = "sk-test"
        settings.get_anthropic_key.return_value = None
        settings.get_google_key.return_value = None
        settings.get_huggingface_key.return_value = None
        settings.defaults.llm_model_openai = "gpt-5.4-nano"

        agent = _create_breakout_agent(
            "-provider openai -name DevAlpha -system You are a coder -timeout 1200",
            MessageBus(),
            "conv:test",
            settings,
        )

        assert agent is not None
        assert agent._timeout == 1200.0


# ---------------------------------------------------------------------------
# CodingSessionManager
# ---------------------------------------------------------------------------

class TestCodingSessionManager:
    def test_init(self, tmp_path):
        bus = MessageBus()
        mgr = CodingSessionManager(
            bus=bus,
            policy=FloorPolicy.ROUND_ROBIN,
            parent_conversation_id="conv:parent",
            sandbox_dir=tmp_path,
            required_context_files=("phases/03_geom-builder.md",),
            initial_todo_items=("Build index.html", "Build main.js"),
        )
        assert mgr.conversation_id.startswith("coding:")
        assert mgr.speaker_uri == CODING_FM_URI
        assert mgr.sandbox_dir == tmp_path
        assert mgr.required_context_files == ("phases/03_geom-builder.md",)
        assert "Build index.html" in mgr.todo.render()
        assert "Build main.js" in mgr.todo.render()

    def test_register_agent(self, tmp_path):
        bus = MessageBus()
        mgr = CodingSessionManager(
            bus=bus,
            policy=FloorPolicy.ROUND_ROBIN,
            parent_conversation_id="conv:parent",
            sandbox_dir=tmp_path,
        )
        mgr.register_agent("tag:test:coder-1", "Dev1")
        assert "tag:test:coder-1" in mgr._agents
        assert mgr._agents["tag:test:coder-1"] == "Dev1"


class TestRequiredContextRendering:
    def test_render_required_context_files(self):
        rendered = _render_required_context_files((
            "phases/03_geom-builder.md",
            "memory/browser-sandbox-delivery.md",
        ))
        assert "REQUIRED CONTEXT FILES" in rendered
        assert "- phases/03_geom-builder.md" in rendered
        assert "- memory/browser-sandbox-delivery.md" in rendered

    def test_default_coding_agent_timeout_constant(self):
        assert DEFAULT_CODING_AGENT_TIMEOUT_SECONDS == 1200.0


# ---------------------------------------------------------------------------
# Workspace snapshot
# ---------------------------------------------------------------------------

class TestWorkspaceSnapshot:
    def test_empty_workspace(self, tmp_path):
        todo = TodoList()
        snapshot = _build_workspace_snapshot(tmp_path, todo)
        assert "WORKSPACE STATUS" in snapshot
        assert "no files yet" in snapshot
        assert "no TODO" in snapshot

    def test_with_files_and_todo(self, tmp_path):
        (tmp_path / "index.html").write_text("<html></html>")
        (tmp_path / "js").mkdir()
        (tmp_path / "js" / "main.js").write_text("// code")
        todo = TodoList(["Create index.html", "Write CSS"])
        todo.mark_done([0])
        snapshot = _build_workspace_snapshot(tmp_path, todo)
        assert "index.html" in snapshot
        assert "main.js" in snapshot
        assert "[x] 0" in snapshot
        assert "1/2 done" in snapshot

    def test_includes_mirrored_phase_and_memory_files(self, tmp_path):
        artifact_store = ArtifactStore(tmp_path / "session")
        artifact_store.save(agent_name="GeomBuilder", content="body")
        memory_store = MemoryStore()
        memory_store.store("tasks", "runner", "Build browser main.js")

        sandbox_dir = tmp_path / "sandbox"
        sync_sandbox_context(sandbox_dir, artifact_store, memory_store)

        snapshot = _build_workspace_snapshot(sandbox_dir, TodoList())
        assert "01_geom-builder.md" in snapshot
        assert "browser-sandbox-delivery.md" in snapshot
        assert "session-memory.md" in snapshot


class TestSandboxContextSync:
    def test_sync_mirrors_phase_and_memory_files(self, tmp_path):
        artifact_store = ArtifactStore(tmp_path / "session")
        artifact_store.save(agent_name="GeomBuilder", content="phase content")
        memory_store = MemoryStore()
        memory_store.store("tasks", "runner", "Build browser main.js")

        sandbox_dir = tmp_path / "sandbox"
        context = sync_sandbox_context(sandbox_dir, artifact_store, memory_store)

        assert "phases/01_geom-builder.md" in context.phase_files
        assert BROWSER_MEMORY_RELATIVE_PATH in context.memory_files
        assert SESSION_MEMORY_RELATIVE_PATH in context.memory_files

        phase_path = sandbox_dir / "phases" / "01_geom-builder.md"
        assert phase_path.exists()
        assert "phase: 1" in phase_path.read_text(encoding="utf-8")

        browser_note = sandbox_dir / BROWSER_MEMORY_RELATIVE_PATH
        assert browser_note.exists()
        assert "file://" in browser_note.read_text(encoding="utf-8")

        session_memory = sandbox_dir / SESSION_MEMORY_RELATIVE_PATH
        assert session_memory.exists()
        assert "Build browser main.js" in session_memory.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fallback code block extraction
# ---------------------------------------------------------------------------

class TestCodeBlockExtraction:
    def test_extract_with_filename_hint(self, tmp_path):
        text = '```js // filename: js/main.js\nconsole.log("hello");\n```'
        saved = _extract_and_save_code_blocks(text, tmp_path, "TestAgent")
        assert len(saved) == 1
        assert saved[0].name == "main.js"
        assert saved[0].read_text() == 'console.log("hello");'

    def test_extract_without_filename(self, tmp_path):
        text = '```html\n<html></html>\n```'
        saved = _extract_and_save_code_blocks(text, tmp_path, "TestAgent")
        assert len(saved) == 1
        assert saved[0].suffix == ".html"

    def test_extract_multiple_blocks(self, tmp_path):
        text = '```js\nconst a=1;\n```\n\n```css\nbody{}\n```'
        saved = _extract_and_save_code_blocks(text, tmp_path, "TestAgent")
        assert len(saved) == 2

    def test_no_code_blocks(self, tmp_path):
        text = "Just some plain text without code blocks."
        saved = _extract_and_save_code_blocks(text, tmp_path, "TestAgent")
        assert len(saved) == 0
