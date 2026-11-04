"""Status/discovery journeys never infer healthy from active membership."""
from __future__ import annotations

import json
import time

import httpx
import pytest
from click.testing import CliRunner

from ofp_playground import cli
from ofp_playground.renderer.gradio_ui import GradioRenderer
from ofp_playground.renderer.status_text import operation_text, status_rows


def snapshot():
    def cell(value, source="self", stale=False):
        return {"value": value, "source": source, "stale": stale}
    return {"cursor": 42, "agents": [{
        "agentId": "instance-a", "name": "Alice", "lifecycle": cell("unreachable", "server"),
        "health": cell("healthy"), "governance": cell("quarantined", "operator"),
        "lastSelfEventAt": time.time() - 9, "tasks": {"task-1": cell("working", stale=True)},
        "floors": {"conv-1": cell("has-floor", "supervisor", True)},
        "observations": {"guardrail-1": cell("degraded", "guardrail")},
    }]}


def test_five_status_dimensions_and_independent_observation_survive_web_loss():
    renderer = GradioRenderer({}, set())
    renderer.update_status(snapshot(), {"instance-a": "Alice", "remote-b": "Remote"}, [], available=True)
    value = renderer.status_markdown()
    for expected in ("unreachable", "healthy", "quarantined", "task-1: working [stale]",
                     "conv-1: has-floor [stale]", "guardrail-1", "degraded", "Remote"):
        assert expected in value
    renderer.update_status(None, {"instance-a": "Alice"}, [], available=False)
    assert "retained observations are stale" in renderer.status_markdown()
    assert "unreachable" in renderer.status_markdown()


def test_plain_status_keeps_scope_and_provenance_separate():
    row = status_rows(snapshot())[0]
    assert row["lifecycle"] == "unreachable"
    assert row["health"] == "healthy"
    assert "server" in row["source"] and "operator" in row["source"]
    assert operation_text({"requested": True, "acknowledged": None, "enforced": False, "outcome": "unknown"}) == "requested: True; acknowledged: None; enforced: False; outcome: unknown"


def test_cli_persona_search_preview_and_models_never_emit_keys():
    runner = CliRunner()
    result = runner.invoke(cli.main, ["personas", "open-floor-protocol-expert", "--json", "--preview"])
    assert result.exit_code == 0
    items = json.loads(result.output)
    assert len(items) == 1
    assert items[0]["slug"] == "development/open-floor-protocol-expert"
    assert items[0]["content"]
    result = runner.invoke(cli.main, ["models", "--json"], env={"OPENAI_API_KEY": "secret-not-to-be-printed"})
    assert result.exit_code == 0
    assert json.loads(result.output)["availability"] == "not-checked"
    assert "secret-not-to-be-printed" not in result.output


async def test_cli_watch_json_reports_lost_observation_and_closes(monkeypatch, tmp_path, capsys):
    from ofp_playground.status import client

    class Connection:
        closed = False
        calls = 0

        async def snapshot(self):
            self.calls += 1
            if self.calls > 1:
                raise httpx.ConnectError("disconnected")
            return snapshot()

        async def close(self):
            self.closed = True

    connection = Connection()

    async def connect(**kwargs):
        assert kwargs["auto_start"] is False
        return connection

    monkeypatch.setattr(client, "connect_local_service", connect)
    with pytest.raises(cli.click.ClickException, match="previous observations are stale"):
        await cli._read_status(data_dir=tmp_path, port=1, watch=True, interval=0,
                               count=2, agent="Alice", as_json=True)
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0]["agents"][0]["agentId"] == "instance-a"
    assert output[1] == {"available": False, "stale": True, "problem": "status-service-unavailable"}
    assert connection.closed


def test_web_rejects_public_control_surface_before_start(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["web", "--share"])
    assert result.exit_code == 1
    assert "supports loopback only" in result.output
