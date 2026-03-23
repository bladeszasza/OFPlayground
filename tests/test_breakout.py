"""Tests for breakout sessions — BreakoutFloorManager, tools, directives, and runner."""
import asyncio
import pytest

from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.floor.policy import FloorPolicy, FloorController
from ofp_playground.floor.breakout import (
    BreakoutFloorManager,
    extract_breakout_summary,
    run_breakout_session,
    BREAKOUT_FM_URI,
)
from ofp_playground.floor.breakout_tools import (
    build_breakout_tools,
    tool_use_to_breakout_directive,
)
from ofp_playground.models.artifact import Utterance


# ---------------------------------------------------------------------------
# FloorPolicy description / use_case properties
# ---------------------------------------------------------------------------

class TestFloorPolicyMeta:
    def test_all_policies_have_description(self):
        for p in FloorPolicy:
            assert isinstance(p.description, str)
            assert len(p.description) > 10

    def test_all_policies_have_use_case(self):
        for p in FloorPolicy:
            assert isinstance(p.use_case, str)
            assert len(p.use_case) > 10

    def test_sequential_description_keywords(self):
        desc = FloorPolicy.SEQUENTIAL.description
        assert "order" in desc.lower() or "turn" in desc.lower()

    def test_showrunner_driven_description(self):
        desc = FloorPolicy.SHOWRUNNER_DRIVEN.description
        assert "orchestrator" in desc.lower()


# ---------------------------------------------------------------------------
# BreakoutFloorManager
# ---------------------------------------------------------------------------

class TestBreakoutFloorManager:
    def test_init(self):
        bus = MessageBus()
        bfm = BreakoutFloorManager(bus, FloorPolicy.ROUND_ROBIN, "conv:parent")
        assert bfm.conversation_id.startswith("breakout:")
        assert bfm.speaker_uri == BREAKOUT_FM_URI

    def test_register_agent(self):
        bus = MessageBus()
        bfm = BreakoutFloorManager(bus, FloorPolicy.ROUND_ROBIN, "conv:parent")
        bfm.register_agent("tag:test:agent-1", "Alice")
        assert "tag:test:agent-1" in bfm._agents
        assert bfm._agents["tag:test:agent-1"] == "Alice"

    def test_max_rounds_default(self):
        bus = MessageBus()
        bfm = BreakoutFloorManager(bus, FloorPolicy.SEQUENTIAL, "conv:parent")
        assert bfm._max_rounds == 6

    def test_custom_max_rounds(self):
        bus = MessageBus()
        bfm = BreakoutFloorManager(bus, FloorPolicy.SEQUENTIAL, "conv:parent", max_rounds=10)
        assert bfm._max_rounds == 10

    def test_policy_type(self):
        bus = MessageBus()
        bfm = BreakoutFloorManager(bus, FloorPolicy.FREE_FOR_ALL, "conv:parent")
        assert isinstance(bfm._policy, FloorController)


# ---------------------------------------------------------------------------
# extract_breakout_summary
# ---------------------------------------------------------------------------

class TestExtractBreakoutSummary:
    def test_empty_history(self):
        result = extract_breakout_summary([], "test topic")
        assert "test topic" in result
        assert "no contributions" in result

    def test_basic_summary(self):
        history = [
            Utterance.from_text("tag:test:agent-1", "Alice", "I think we should use Redis"),
            Utterance.from_text("tag:test:agent-2", "Bob", "I agree, Redis is fast"),
        ]
        result = extract_breakout_summary(history, "Database choice")
        assert "[BREAKOUT SUMMARY: Database choice]" in result
        assert "[Alice]" in result
        assert "[Bob]" in result
        assert "Redis" in result

    def test_skips_floor_manager(self):
        history = [
            Utterance.from_text("tag:test:floor-manager", "FM", "Topic seeded"),
            Utterance.from_text("tag:test:agent-1", "Alice", "My perspective"),
        ]
        result = extract_breakout_summary(history, "test")
        assert "FM" not in result
        assert "[Alice]" in result

    def test_skips_breakout_fm(self):
        history = [
            Utterance.from_text(BREAKOUT_FM_URI, "BreakoutFM", "Topic seeded"),
            Utterance.from_text("tag:test:agent-1", "Alice", "My input"),
        ]
        result = extract_breakout_summary(history, "test")
        assert "BreakoutFM" not in result

    def test_truncates_long_utterances(self):
        history = [
            Utterance.from_text("tag:test:agent-1", "Alice", "x" * 500),
        ]
        result = extract_breakout_summary(history, "test")
        assert "..." in result

    def test_max_chars_truncation(self):
        history = [
            Utterance.from_text("tag:test:agent-1", "Alice", "A" * 200),
            Utterance.from_text("tag:test:agent-2", "Bob", "B" * 200),
        ]
        result = extract_breakout_summary(history, "test", max_chars=100)
        assert len(result) <= 110  # allow small overshoot from line boundary


# ---------------------------------------------------------------------------
# build_breakout_tools
# ---------------------------------------------------------------------------

class TestBuildBreakoutTools:
    def _make_settings(self, **keys):
        """Create a mock settings object with specified API keys."""
        from unittest.mock import MagicMock
        s = MagicMock()
        s.get_anthropic_key.return_value = keys.get("anthropic", "")
        s.get_openai_key.return_value = keys.get("openai", "")
        s.get_google_key.return_value = keys.get("google", "")
        s.get_huggingface_key.return_value = keys.get("hf", "")
        return s

    def test_no_keys_returns_empty(self):
        settings = self._make_settings()
        tools = build_breakout_tools(settings)
        assert tools == []

    def test_with_hf_key(self):
        settings = self._make_settings(hf="fake-key")
        tools = build_breakout_tools(settings)
        assert len(tools) == 1
        assert tools[0]["name"] == "create_breakout_session"

    def test_schema_has_required_fields(self):
        settings = self._make_settings(hf="fake-key")
        tools = build_breakout_tools(settings)
        schema = tools[0]["input_schema"]
        assert "topic" in schema["properties"]
        assert "policy" in schema["properties"]
        assert "agents" in schema["properties"]
        assert set(schema["required"]) == {"topic", "policy", "agents"}

    def test_showrunner_excluded_from_policies(self):
        settings = self._make_settings(hf="fake-key")
        tools = build_breakout_tools(settings)
        policies = tools[0]["input_schema"]["properties"]["policy"]["enum"]
        assert "showrunner_driven" not in policies
        assert "round_robin" in policies
        assert "sequential" in policies

    def test_available_providers(self):
        settings = self._make_settings(anthropic="key-a", openai="key-o")
        tools = build_breakout_tools(settings)
        providers = tools[0]["input_schema"]["properties"]["agents"]["items"]["properties"]["provider"]["enum"]
        assert "anthropic" in providers
        assert "openai" in providers
        assert "hf" not in providers
        assert "google" not in providers

    def test_all_providers(self):
        settings = self._make_settings(anthropic="a", openai="o", google="g", hf="h")
        tools = build_breakout_tools(settings)
        providers = tools[0]["input_schema"]["properties"]["agents"]["items"]["properties"]["provider"]["enum"]
        assert set(providers) == {"anthropic", "openai", "google", "hf"}

    def test_min_agents(self):
        settings = self._make_settings(hf="fake-key")
        tools = build_breakout_tools(settings)
        assert tools[0]["input_schema"]["properties"]["agents"]["minItems"] == 2


# ---------------------------------------------------------------------------
# tool_use_to_breakout_directive
# ---------------------------------------------------------------------------

class TestToolUseToBreakoutDirective:
    def test_basic_directive(self):
        args = {
            "topic": "Should we use Postgres or Redis?",
            "policy": "round_robin",
            "max_rounds": 4,
            "agents": [
                {"name": "Advocate", "system": "You advocate for Postgres", "provider": "hf"},
                {"name": "Skeptic", "system": "You advocate for Redis", "provider": "hf"},
            ],
        }
        result = tool_use_to_breakout_directive(args)
        lines = result.strip().split("\n")
        assert len(lines) == 3
        assert "[BREAKOUT policy=round_robin max_rounds=4" in lines[0]
        assert "Should we use Postgres or Redis?" in lines[0]
        assert "[BREAKOUT_AGENT -provider hf -name Advocate" in lines[1]
        assert "[BREAKOUT_AGENT -provider hf -name Skeptic" in lines[2]

    def test_with_model_override(self):
        args = {
            "topic": "Test",
            "policy": "sequential",
            "agents": [
                {"name": "A", "system": "sys", "provider": "anthropic", "model": "claude-3-haiku"},
                {"name": "B", "system": "sys2", "provider": "anthropic"},
            ],
        }
        result = tool_use_to_breakout_directive(args)
        assert "-model claude-3-haiku" in result
        # Agent B has no model override
        lines = result.strip().split("\n")
        assert "-model" not in lines[2]

    def test_defaults_when_missing(self):
        args = {}
        result = tool_use_to_breakout_directive(args)
        assert "[BREAKOUT policy=round_robin max_rounds=6" in result

    def test_three_agents(self):
        args = {
            "topic": "Debate",
            "policy": "free_for_all",
            "agents": [
                {"name": "A", "system": "s1", "provider": "hf"},
                {"name": "B", "system": "s2", "provider": "openai"},
                {"name": "C", "system": "s3", "provider": "google"},
            ],
        }
        result = tool_use_to_breakout_directive(args)
        lines = result.strip().split("\n")
        assert len(lines) == 4  # 1 header + 3 agents


# ---------------------------------------------------------------------------
# FloorManager directive parsing for breakout
# ---------------------------------------------------------------------------

class TestFloorManagerBreakoutParsing:
    @pytest.mark.asyncio
    async def test_parse_breakout_directives(self):
        """Test that _handle_orchestrator_directives collects breakout info."""
        from ofp_playground.floor.manager import FloorManager

        bus = MessageBus()
        fm = FloorManager(bus, policy=FloorPolicy.SHOWRUNNER_DRIVEN)
        fm.register_agent("tag:test:orchestrator", "Orch")
        fm.register_orchestrator("tag:test:orchestrator")

        # Track what the breakout callback receives
        callback_args = {}
        async def mock_breakout_callback(topic, policy, max_rounds, agent_specs):
            callback_args["topic"] = topic
            callback_args["policy"] = policy
            callback_args["max_rounds"] = max_rounds
            callback_args["agent_specs"] = agent_specs
            return "[BREAKOUT SUMMARY: test] — mock result"

        fm._breakout_callback = mock_breakout_callback

        # Register the orchestrator so we can grant floor
        orc_queue = asyncio.Queue()
        await bus.register("tag:test:orchestrator", orc_queue)
        fm_queue = asyncio.Queue()
        await bus.register(fm.speaker_uri, fm_queue)

        text = (
            "[BREAKOUT policy=round_robin max_rounds=4 topic=Should we use Postgres?]\n"
            "[BREAKOUT_AGENT -provider hf -name Advocate -system You love Postgres]\n"
            "[BREAKOUT_AGENT -provider hf -name Skeptic -system You prefer Redis]\n"
        )
        await fm._handle_orchestrator_directives(text)

        assert callback_args["topic"] == "Should we use Postgres?"
        assert callback_args["policy"] == FloorPolicy.ROUND_ROBIN
        assert callback_args["max_rounds"] == 4
        assert len(callback_args["agent_specs"]) == 2

    @pytest.mark.asyncio
    async def test_breakout_no_callback_logged(self):
        """Without a callback, the breakout is just logged."""
        from ofp_playground.floor.manager import FloorManager

        bus = MessageBus()
        fm = FloorManager(bus, policy=FloorPolicy.SHOWRUNNER_DRIVEN)
        fm.register_agent("tag:test:orchestrator", "Orch")
        fm.register_orchestrator("tag:test:orchestrator")
        # No breakout_callback set

        text = (
            "[BREAKOUT policy=sequential max_rounds=3 topic=Test]\n"
            "[BREAKOUT_AGENT -provider hf -name A -system sys]\n"
            "[BREAKOUT_AGENT -provider hf -name B -system sys]\n"
        )
        # Should not raise
        await fm._handle_orchestrator_directives(text)

    @pytest.mark.asyncio
    async def test_breakout_header_without_agents_ignored(self):
        """A [BREAKOUT] without [BREAKOUT_AGENT] lines is ignored."""
        from ofp_playground.floor.manager import FloorManager

        bus = MessageBus()
        fm = FloorManager(bus, policy=FloorPolicy.SHOWRUNNER_DRIVEN)

        called = False
        async def mock_cb(*args):
            nonlocal called
            called = True
            return "summary"

        fm._breakout_callback = mock_cb

        text = "[BREAKOUT policy=round_robin max_rounds=4 topic=Lonely breakout]\n"
        await fm._handle_orchestrator_directives(text)
        assert not called

    @pytest.mark.asyncio
    async def test_breakout_invalid_policy_defaults(self):
        """Invalid policy falls back to round_robin."""
        from ofp_playground.floor.manager import FloorManager

        bus = MessageBus()
        fm = FloorManager(bus, policy=FloorPolicy.SHOWRUNNER_DRIVEN)
        fm.register_agent("tag:test:orchestrator", "Orch")
        fm.register_orchestrator("tag:test:orchestrator")

        callback_args = {}
        async def mock_cb(topic, policy, max_rounds, agent_specs):
            callback_args["policy"] = policy
            return "summary"

        fm._breakout_callback = mock_cb

        orc_queue = asyncio.Queue()
        await bus.register("tag:test:orchestrator", orc_queue)
        fm_queue = asyncio.Queue()
        await bus.register(fm.speaker_uri, fm_queue)

        text = (
            "[BREAKOUT policy=nonexistent_policy max_rounds=4 topic=test]\n"
            "[BREAKOUT_AGENT -provider hf -name A -system s]\n"
            "[BREAKOUT_AGENT -provider hf -name B -system s]\n"
        )
        await fm._handle_orchestrator_directives(text)
        assert callback_args["policy"] == FloorPolicy.ROUND_ROBIN

    @pytest.mark.asyncio
    async def test_breakout_showrunner_policy_prevented(self):
        """showrunner_driven is downgraded to round_robin in breakouts."""
        from ofp_playground.floor.manager import FloorManager

        bus = MessageBus()
        fm = FloorManager(bus, policy=FloorPolicy.SHOWRUNNER_DRIVEN)
        fm.register_agent("tag:test:orchestrator", "Orch")
        fm.register_orchestrator("tag:test:orchestrator")

        callback_args = {}
        async def mock_cb(topic, policy, max_rounds, agent_specs):
            callback_args["policy"] = policy
            return "summary"

        fm._breakout_callback = mock_cb

        orc_queue = asyncio.Queue()
        await bus.register("tag:test:orchestrator", orc_queue)
        fm_queue = asyncio.Queue()
        await bus.register(fm.speaker_uri, fm_queue)

        text = (
            "[BREAKOUT policy=showrunner_driven max_rounds=4 topic=test]\n"
            "[BREAKOUT_AGENT -provider hf -name A -system s]\n"
            "[BREAKOUT_AGENT -provider hf -name B -system s]\n"
        )
        await fm._handle_orchestrator_directives(text)
        assert callback_args["policy"] == FloorPolicy.ROUND_ROBIN

    @pytest.mark.asyncio
    async def test_breakout_max_rounds_clamped(self):
        """max_rounds is clamped to 2-20."""
        from ofp_playground.floor.manager import FloorManager

        bus = MessageBus()
        fm = FloorManager(bus, policy=FloorPolicy.SHOWRUNNER_DRIVEN)
        fm.register_agent("tag:test:orchestrator", "Orch")
        fm.register_orchestrator("tag:test:orchestrator")

        callback_args = {}
        async def mock_cb(topic, policy, max_rounds, agent_specs):
            callback_args["max_rounds"] = max_rounds
            return "summary"

        fm._breakout_callback = mock_cb

        orc_queue = asyncio.Queue()
        await bus.register("tag:test:orchestrator", orc_queue)
        fm_queue = asyncio.Queue()
        await bus.register(fm.speaker_uri, fm_queue)

        # Test too low
        text = (
            "[BREAKOUT policy=sequential max_rounds=0 topic=test]\n"
            "[BREAKOUT_AGENT -provider hf -name A -system s]\n"
            "[BREAKOUT_AGENT -provider hf -name B -system s]\n"
        )
        await fm._handle_orchestrator_directives(text)
        assert callback_args["max_rounds"] == 2

    @pytest.mark.asyncio
    async def test_mixed_directives_with_breakout(self):
        """Breakout directives can coexist with other directives."""
        from ofp_playground.floor.manager import FloorManager

        bus = MessageBus()
        fm = FloorManager(bus, policy=FloorPolicy.SHOWRUNNER_DRIVEN)
        fm.register_agent("tag:test:orchestrator", "Orch")
        fm.register_orchestrator("tag:test:orchestrator")

        breakout_called = False
        async def mock_cb(topic, policy, max_rounds, agent_specs):
            nonlocal breakout_called
            breakout_called = True
            return "summary"

        fm._breakout_callback = mock_cb

        orc_queue = asyncio.Queue()
        await bus.register("tag:test:orchestrator", orc_queue)
        fm_queue = asyncio.Queue()
        await bus.register(fm.speaker_uri, fm_queue)

        # [ACCEPT] + breakout in the same utterance
        fm._last_worker_text = "some text"
        text = (
            "[ACCEPT]\n"
            "[BREAKOUT policy=round_robin max_rounds=4 topic=Side discussion]\n"
            "[BREAKOUT_AGENT -provider hf -name A -system s]\n"
            "[BREAKOUT_AGENT -provider hf -name B -system s]\n"
        )
        await fm._handle_orchestrator_directives(text)
        assert breakout_called
        # [ACCEPT] should also have been processed
        assert "some text" in fm._manuscript


# ---------------------------------------------------------------------------
# run_breakout_session
# ---------------------------------------------------------------------------

class TestRunBreakoutSession:
    @pytest.mark.asyncio
    async def test_too_few_agents(self):
        result = await run_breakout_session(
            topic="Test",
            agents=[],
            policy=FloorPolicy.ROUND_ROBIN,
            parent_conversation_id="conv:test",
        )
        assert "need at least 2 agents" in result

    @pytest.mark.asyncio
    async def test_one_agent(self):
        from unittest.mock import MagicMock
        agent = MagicMock()
        agent.name = "Solo"
        result = await run_breakout_session(
            topic="Test",
            agents=[agent],
            policy=FloorPolicy.ROUND_ROBIN,
            parent_conversation_id="conv:test",
        )
        assert "need at least 2 agents" in result
