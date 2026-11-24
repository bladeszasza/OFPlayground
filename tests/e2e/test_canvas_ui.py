"""Canvas UI journeys, recorded for demos.

Offline tier (``OFP_E2E=1``): scripted local agents, no API key. Live tier
(``OFP_E2E_LIVE=1`` too): a round-robin of three Claude Haiku 5.5 agents
(about six short model calls) and Verity, a remote OFP fact-checker on a
third-party host that receives the conversation text.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
import uuid
from pathlib import Path

import httpx
import pytest

if os.environ.get("OFP_E2E") != "1":
    pytest.skip("Canvas browser tests are opt-in: set OFP_E2E=1", allow_module_level=True)
sync_api = pytest.importorskip("playwright.sync_api",
                               reason="install the e2e extra: uv sync --extra e2e")

from .conftest import REPO

expect = sync_api.expect
pytestmark = pytest.mark.e2e

ASP_PRESET = "ASP showcase · no API keys"
HAIKU_PRESET = "Claude Haiku 5.5 roundtable"
HAIKU_AGENTS = ("Strategist", "Engineer", "Editor")
REMOTE_AGENT = "Verity"


def _agent_node(page, name: str):
    # By node id: agents quote each other, so a text match can hit several nodes.
    return page.locator(f'.react-flow__node-AgentNode[data-id="agent-{name}"]')


def _load_preset(page, title: str, agents: tuple[str, ...]) -> None:
    page.get_by_label("Load preset").select_option(label=title)
    for name in agents:
        expect(_agent_node(page, name)).to_have_count(1)


def _expect_offline(page, names) -> None:
    """Agents report shutdown to ASP shortly after the session ends; nobody keeps the floor."""
    for name in names:
        row = page.locator(".asp-agent-list .asp-agent", has_text=name).first
        expect(row).to_contain_text("offline", timeout=15_000)
    expect(page.locator(".react-flow__node-AgentNode", has_text="has floor")).to_have_count(0)


def _fit(page) -> None:
    page.get_by_role("button", name="Fit selection / all").click()


def _run(page) -> None:
    page.get_by_role("button", name="Run session").click()
    expect(page.get_by_role("button", name="Stop session")).to_be_visible()
    expect(page.get_by_text("Connected", exact=True)).to_be_visible()


def test_asp_showcase_observe_and_quarantine(ui):
    page, shot = ui
    shot("empty-canvas")

    _load_preset(page, ASP_PRESET, ("Worker", "Retry", "Input", "Silent", "Stalled"))
    _fit(page)
    shot("asp-preset-loaded")

    _run(page)
    summary = page.locator(".asp-summary")
    expect(summary).to_have_text(re.compile(r"^([4-9]|\d{2,}) observed"), timeout=45_000)
    stalled = page.locator(".asp-agent", has_text="Stalled")
    expect(stalled).to_be_visible()
    # Give the scripted agents time to work, retry, wait for input and stall.
    page.wait_for_timeout(6_000)
    _fit(page)
    shot("asp-session-live")

    stalled.click()
    details = page.get_by_role("complementary", name="Agent Status").get_by_label(
        "Stalled status details")
    expect(details).to_be_visible()
    shot("stalled-agent-details")

    details.get_by_label("Governance authority").select_option("guardrail")
    details.get_by_role("button", name="Quarantine & stop").click()
    expect(details.get_by_role("status").filter(has_text="Requested:")).to_be_visible()
    expect(stalled).to_contain_text("quarantined", timeout=15_000)
    shot("stalled-quarantined")

    page.get_by_role("button", name="Stop session").click()
    expect(page.get_by_role("button", name="Run session")).to_be_visible(timeout=30_000)
    shot("session-stopped")


def test_turn_limit_ends_session(ui, tmp_path):
    """A round-robin with max turns stops by itself and the UI returns to Run."""
    page, shot = ui
    preset = json.loads((REPO / "canvas/presets/asp-showcase.json").read_text(encoding="utf-8"))
    keep = {"floor-main", "conv-main", "agent-Worker", "agent-Retry"}
    preset["nodes"] = [node for node in preset["nodes"] if node["id"] in keep]
    preset["edges"] = [edge for edge in preset["edges"] if edge["target"] in keep]
    preset["nodes"][0]["data"].update(policy="ROUND_ROBIN", maxTurns=4, noHuman=True,
                                      topic="Two scripted agents take four turns.")
    config = tmp_path / "turn-limit.json"
    config.write_text(json.dumps(preset), encoding="utf-8")

    page.get_by_label("Load configuration JSON").set_input_files(str(config))
    for name in ("Worker", "Retry"):
        expect(_agent_node(page, name)).to_have_count(1)
    _fit(page)
    shot("turn-limit-loaded")

    _run(page)
    # The Floor node hides its turn counter once stopped; agent messages stay.
    expect(page.get_by_role("button", name="Run session")).to_be_visible(timeout=90_000)
    for name in ("Worker", "Retry"):
        expect(_agent_node(page, name)).to_contain_text(re.compile(r"\d+ messages?"))
    _expect_offline(page, ("Worker", "Retry"))
    _fit(page)
    shot("turn-limit-finished")


def test_haiku_preset_loads(ui):
    """The live preset's graph, checked without calling a model."""
    page, shot = ui
    _load_preset(page, HAIKU_PRESET, HAIKU_AGENTS)
    for name in HAIKU_AGENTS:
        expect(_agent_node(page, name)).to_contain_text("claude-haiku-5-5")
    expect(_agent_node(page, REMOTE_AGENT)).to_contain_text(re.compile("remote", re.IGNORECASE))
    expect(page.locator(".react-flow__node-FloorNode")).to_have_count(1)
    _fit(page)
    shot("haiku-preset-loaded")


def _anthropic_key_available() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    env_file = REPO / ".env"
    if env_file.exists() and any(
            line.strip().startswith("ANTHROPIC_API_KEY=") and line.split("=", 1)[1].strip()
            for line in env_file.read_text(encoding="utf-8").splitlines()):
        return True
    config = Path.home() / ".ofp-playground" / "config.toml"
    try:
        keys = tomllib.loads(config.read_text(encoding="utf-8")).get("api_keys", {})
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return bool(keys.get("anthropic"))


live = pytest.mark.skipif(
    os.environ.get("OFP_E2E_LIVE") != "1" or not _anthropic_key_available(),
    reason="live model test: set OFP_E2E_LIVE=1 and an Anthropic key (env, .env or config.toml)",
)


def _remote_reachable(slug: str) -> bool:
    """Ask the remote for its manifest; this also wakes a sleeping host."""
    from ofp_playground.agents.remote import KNOWN_REMOTE_AGENTS, OFP_SCHEMA_VERSION

    url = KNOWN_REMOTE_AGENTS[slug][1]
    envelope = {"openFloor": {
        "schema": {"version": OFP_SCHEMA_VERSION},
        "conversation": {"id": f"conv:{uuid.uuid4()}"},
        "sender": {"speakerUri": "tag:ofp-playground.local,2025:e2e-probe"},
        "events": [{"eventType": "getManifests", "to": {"serviceUrl": url},
                    "parameters": {"recommendScope": "internal"}}],
    }}
    try:
        return httpx.post(url, json=envelope, timeout=60, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


@live
def test_haiku_roundtable_live(ui):
    if not _remote_reachable("verity"):
        pytest.skip("the Verity remote agent is unreachable; the preset relies on it")
    page, shot = ui
    _load_preset(page, HAIKU_PRESET, (*HAIKU_AGENTS, REMOTE_AGENT))
    _fit(page)
    shot("haiku-preset-loaded")

    _run(page)
    status = page.locator(".asp-agent-list")
    for name in HAIKU_AGENTS:
        expect(status.locator(".asp-agent", has_text=name)).to_be_visible(timeout=60_000)
    page.wait_for_timeout(4_000)
    _fit(page)
    shot("haiku-session-live")

    # max_turns=8 ends the floor; the bridge then sends session_ended.
    expect(page.get_by_role("button", name="Run session")).to_be_visible(timeout=360_000)
    for name in (*HAIKU_AGENTS, REMOTE_AGENT):
        expect(_agent_node(page, name)).to_contain_text(re.compile(r"\d+ messages?"))
    problems = page.locator(".asp-agent-list .asp-disagreement", has_text="Telemetry error")
    expect(problems).to_have_count(0)
    _expect_offline(page, HAIKU_AGENTS)
    _fit(page)
    shot("haiku-session-finished")
