"""Exercise each interface's actual session runner without model/network calls."""
from __future__ import annotations

import asyncio
import io

import pytest
from rich.console import Console
from click.testing import CliRunner

from canvas.backend.main import CanvasNode, _run_canvas_session
from canvas.backend.session_bridge import SessionBridge
from ofp_playground import cli
from ofp_playground.config.settings import Settings
from ofp_playground.floor.policy import FloorPolicy
from tests.test_session_lifecycle import ScriptedAgent


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "console", Console(file=io.StringIO()))
    agents = []
    floors = []

    async def spawn(agent_type, name, description, floor, bus, registry, *args, **kwargs):
        agent = ScriptedAgent(name, bus, floor)
        agents.append(agent)
        floors.append(floor)
        floor.register_agent(agent.speaker_uri, name)
        registry.register(agent)
        await bus.register(agent.speaker_uri, agent.queue)
        registry.start(agent)
        return agent

    async def complete(topic, floor, bus):
        floor.stop()  # Completion arrives far below the requested turn cap.

    monkeypatch.setattr(cli, "_spawn_llm_agent", spawn)
    monkeypatch.setattr(cli, "_seed_topic", complete)
    return agents, floors


async def test_cli_early_completion_joins_owned_tasks(runtime):
    agents, _ = runtime
    await asyncio.wait_for(cli._run_session(
        FloorPolicy.SEQUENTIAL, ("hf:A:work",), (), Settings(), False,
        no_human=True, topic="provider-free cleanup", max_turns=999,
    ), timeout=3)
    assert agents[0].finished.is_set()


async def test_canvas_early_completion_joins_owned_tasks(runtime):
    agents, _ = runtime
    state = {}
    bridge = SessionBridge()
    await asyncio.wait_for(_run_canvas_session(
        bridge=bridge,
        nodes=[CanvasNode(id="a", type="AgentNode", data={"name": "A", "provider": "hf"})],
        policy=FloorPolicy.SEQUENTIAL, topic="provider-free cleanup", max_turns=999,
        show_floor_events=False, no_human=True, human_name="User",
        settings=Settings(), state=state,
    ), timeout=3)
    assert agents[0].finished.is_set()
    assert state["bus"].registered_agents == []
    assert bridge.event_log[-1]["type"] == "session_ended"


async def test_web_early_completion_closes_server_and_observer(runtime, monkeypatch):
    from ofp_playground.renderer import gradio_ui

    agents, floors = runtime
    closed = asyncio.Event()
    loop = asyncio.get_running_loop()

    class Server:
        def close(self):
            loop.call_soon_threadsafe(closed.set)

    monkeypatch.setattr(gradio_ui, "launch_web_session", lambda **kwargs: Server())
    await asyncio.wait_for(cli._run_web_session(
        FloorPolicy.SEQUENTIAL, ("hf:A:work",), Settings(), False,
        no_human=True, topic="provider-free cleanup", max_turns=999,
    ), timeout=3)
    assert closed.is_set()
    assert agents[0].finished.is_set()
    assert floors[0]._bus.registered_agents == []


def test_cli_reports_startup_failure_with_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["start", "--no-human", "--agent", "unknown:A:work"])
    assert result.exit_code == 1
    assert "Could not start agent" in result.output


def test_empty_autonomous_cli_explains_required_participant(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["start", "--no-human"])
    assert result.exit_code == 1
    assert "at least one --agent or --remote" in result.output
