from __future__ import annotations

from click.testing import CliRunner

from ofp_playground import cli


def test_resolve_agent_slug_with_trailing_instructions(monkeypatch):
    import ofp_playground.agents.library as library

    def fake_resolve_slug(ref: str) -> str:
        if ref == "@development/threejs-developer":
            return "BASE SOUL"
        raise ValueError(f"Agent not found: {ref}")

    monkeypatch.setattr(library, "resolve_slug", fake_resolve_slug)

    resolved = cli._resolve_agent_slug(
        "@development/threejs-developer. Write actual workspace files index.html and main.js."
    )

    assert resolved == (
        "BASE SOUL\n\n"
        "Additional instructions:\n"
        "Write actual workspace files index.html and main.js."
    )


def test_parse_agent_spec_allows_slug_prefix_with_extra_text(monkeypatch):
    import ofp_playground.agents.library as library

    def fake_resolve_slug(ref: str) -> str:
        if ref == "@development/threejs-developer":
            return "BASE SOUL"
        raise ValueError(f"Agent not found: {ref}")

    monkeypatch.setattr(library, "resolve_slug", fake_resolve_slug)

    agent_type, name, description, model_override, _, _, _ = cli._parse_agent_spec(
        "-provider openai -name DevGamma -system @development/threejs-developer. Keep hero.z at 0. -model gpt-5.4"
    )

    assert agent_type == "openai"
    assert name == "DevGamma"
    assert description.startswith("BASE SOUL\n\nAdditional instructions:\nKeep hero.z at 0.")
    assert model_override == "gpt-5.4"


def test_parse_agent_spec_ignores_flags_inside_bracketed_directives():
    agent_type, name, description, model_override, _, _, _ = cli._parse_agent_spec(
        "-provider google -type orchestrator -name SeriesDirector -system Use this breakout template exactly: [BREAKOUT_AGENT -provider hf -model MiniMaxAI/MiniMax-M2.7 -name <CharacterName2> -system <paste full memory here>] -model gemini-3.1-flash-lite"
    )

    assert agent_type == "google:orchestrator"
    assert name == "SeriesDirector"
    assert model_override == "gemini-3.1-flash-lite"
    assert "[BREAKOUT_AGENT -provider hf -model MiniMaxAI/MiniMax-M2.7" in description
    assert "-name <CharacterName2>" in description


def test_parse_agent_spec_keeps_prose_model_mentions_inside_system():
    mission = """Run phases in order.

MODEL MIX:
- Use only Anthropic and OpenAI breakout agents.
- Do not specify -model in [BREAKOUT_AGENT] lines; let provider defaults apply.

Assign MusicGen to create a 2-minute cinematic martial-cosmic theme based on the max
500 word summary of the novel.

After MusicGen yields the floor:
[TASK_COMPLETE]"""

    agent_type, name, description, model_override, _, _, _ = cli._parse_agent_spec(
        f"-provider anthropic -type orchestrator -name SeriesDirector -system {mission}"
    )

    assert agent_type == "anthropic:orchestrator"
    assert name == "SeriesDirector"
    assert model_override is None
    assert "Do not specify -model in [BREAKOUT_AGENT] lines" in description
    assert "Assign MusicGen to create" in description


def test_canvas_command_exists():
    runner = CliRunner()

    result = runner.invoke(cli.main, ["canvas", "--help"])

    assert result.exit_code == 0
    assert "--port" in result.output
    assert "--host" in result.output


def test_build_canvas_config_colon_format():
    config = cli._build_canvas_config(
        policy="free_for_all",
        agents=("anthropic:Alice:You are Alice.", "openai:Bob:You are Bob."),
        topic="test topic",
        no_human=True,
        max_turns=10,
        human_name="User",
    )
    node_types = {n["id"]: n["type"] for n in config["nodes"]}
    assert node_types["floor-main"] == "FloorNode"
    assert node_types["conv-main"] == "ConversationNode"
    assert node_types["agent-Alice"] == "AgentNode"
    assert node_types["agent-Bob"] == "AgentNode"
    # No HumanNode when no_human=True
    assert all(n["type"] != "HumanNode" for n in config["nodes"])
    floor = next(n for n in config["nodes"] if n["id"] == "floor-main")
    assert floor["data"]["policy"] == "FREE_FOR_ALL"
    assert floor["data"]["topic"] == "test topic"
    assert floor["data"]["maxTurns"] == 10
    assert floor["data"]["noHuman"] is True
    alice = next(n for n in config["nodes"] if n["id"] == "agent-Alice")
    assert alice["data"]["provider"] == "anthropic"
    assert alice["data"]["name"] == "Alice"
    assert "Alice" in alice["data"]["systemPrompt"]


def test_build_canvas_config_flag_format():
    config = cli._build_canvas_config(
        policy="SEQUENTIAL",
        agents=("-provider anthropic -name Carol -system You are Carol.",),
        topic=None,
        no_human=False,
        max_turns=None,
        human_name="Dev",
    )
    node_types = {n["id"]: n["type"] for n in config["nodes"]}
    assert node_types["agent-Carol"] == "AgentNode"
    # HumanNode present when no_human=False
    assert any(n["type"] == "HumanNode" for n in config["nodes"])
    human = next(n for n in config["nodes"] if n["type"] == "HumanNode")
    assert human["data"]["humanName"] == "Dev"


def test_build_canvas_config_edges_connect_floor_to_agents():
    config = cli._build_canvas_config(
        policy="SEQUENTIAL",
        agents=("anthropic:Alice", "openai:Bob"),
        topic=None,
        no_human=True,
        max_turns=None,
        human_name="User",
    )
    edge_targets = {e["target"] for e in config["edges"]}
    assert "conv-main" in edge_targets
    assert "agent-Alice" in edge_targets
    assert "agent-Bob" in edge_targets
    assert all(e["source"] == "floor-main" for e in config["edges"])


def test_build_canvas_config_agent_type_subtype():
    config = cli._build_canvas_config(
        policy="SHOWRUNNER_DRIVEN",
        agents=("-provider anthropic -type orchestrator -name Director -system You direct.",),
        topic=None,
        no_human=True,
        max_turns=None,
        human_name="User",
    )
    director = next(n for n in config["nodes"] if n["data"].get("name") == "Director")
    assert director["data"]["agentType"] == "orchestrator"
    assert director["data"]["provider"] == "anthropic"
def test_retry_zero_is_distinct_from_unspecified_default():
    assert cli._parse_agent_spec('-provider demo -name Worker -max-retries 0')[-1] == 0
    assert cli._parse_agent_spec('demo:Worker:Synthetic worker')[-1] is None
