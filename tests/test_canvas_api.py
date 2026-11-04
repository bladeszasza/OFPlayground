from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from canvas.backend.main import build_app
from canvas.backend.main import _safe_media_path
from fastapi import HTTPException


@pytest.fixture
def app():
    return build_app(authenticate=False)


async def test_session_start_and_stop(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        payload = {
            "nodes": [
                {
                    "id": "floor-1",
                    "type": "FloorNode",
                    "data": {
                        "policy": "SEQUENTIAL",
                        "topic": "test topic",
                        "maxTurns": 1,
                        "showFloorEvents": False,
                        "noHuman": True,
                    },
                }
            ],
            "edges": [],
        }

        response = await client.post("/session/start", json=payload)
        assert response.status_code == 200
        assert response.json()["session_id"]

        status_response = await client.get("/session/status")
        assert status_response.status_code == 200
        assert status_response.json()["running"] is True

        stop_response = await client.delete("/session/stop")
        assert stop_response.status_code == 200

        final_status_response = await client.get("/session/status")
        assert final_status_response.status_code == 200
        assert final_status_response.json()["running"] is False


async def test_media_path_traversal_rejected(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/media/../../../etc/passwd")
        assert response.status_code in (400, 404)


async def test_session_status_no_session(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/session/status")
        assert response.status_code == 200
        assert response.json()["running"] is False


async def test_agents_list_returns_sorted_entries(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/agents/list")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) > 0
    first = data[0]
    assert {"slug", "display_name", "category"} <= first.keys()
    slugs = [item["slug"] for item in data]
    assert slugs == sorted(slugs)


async def test_models_list_returns_grouped_providers(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/models/list")
    assert response.status_code == 200
    data = response.json()
    assert {"anthropic", "openai", "google", "huggingface"} <= data.keys()
    assert any("claude" in m for m in data["anthropic"])
    assert any("gpt" in m for m in data["openai"])
    assert any("gemini" in m for m in data["google"])
    assert data["huggingface"] == []


async def test_persona_preview_uses_same_resolver_as_runtime(app) -> None:
    from ofp_playground.cli import _resolve_agent_slug

    reference = "@development/code-reviewer. Focus on lifecycle ownership."
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/agents/preview", json={"reference": reference})
        assert response.status_code == 200
        assert response.json()["prompt"] == _resolve_agent_slug(reference)
        assert response.headers["cache-control"] == "no-store"
        for invalid in ["/etc/passwd", "@../../private", "@missing/not-real"]:
            assert (await client.post("/agents/preview", json={"reference": invalid})).status_code == 422


async def test_provider_configuration_exposes_choices_without_credentials(monkeypatch) -> None:
    from ofp_playground.config.settings import Settings

    settings = Settings()
    settings.api_keys.openai = "TEST-SECRET-DO-NOT-EXPOSE"
    settings.defaults.llm_model_openai = "custom/account-model"
    monkeypatch.setattr(Settings, "load", lambda: settings)
    app = build_app(authenticate=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/session/configuration")
    assert response.status_code == 200
    assert "TEST-SECRET" not in response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["availability"] == "not-checked"
    assert result["providers"]["openai"]["credentialConfigured"] is True
    assert result["providers"]["openai"]["defaults"]["text-generation"] == "custom/account-model"
    assert "text-to-music" not in result["providers"]["openai"]["tasks"]
    assert "code-generation" not in result["providers"]["huggingface"]["tasks"]
    assert result["providers"]["demo"]["credentialConfigured"] is True


async def test_agent_type_field_accepted_in_start(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        payload = {
            "nodes": [
                {
                    "id": "floor-1",
                    "type": "FloorNode",
                    "data": {
                        "policy": "SEQUENTIAL",
                        "topic": "",
                        "maxTurns": 1,
                        "showFloorEvents": False,
                        "noHuman": True,
                        "agentType": "",
                    },
                },
                {
                    "id": "agent-1",
                    "type": "AgentNode",
                    "data": {
                        "provider": "anthropic",
                        "name": "Writer",
                        "system": "You are a writer.",
                        "model": "",
                        "agentType": "code-generation",
                    },
                },
            ],
            "edges": [],
        }
        response = await client.post("/session/start", json=payload)
    # Just check it doesn't 422 — agentType must be accepted
    assert response.status_code != 422


async def test_session_initial_returns_none_when_no_config() -> None:
    app = build_app(authenticate=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/session/initial")
    assert response.status_code == 200
    assert response.json() is None


async def test_session_initial_returns_config_when_provided() -> None:
    config = {
        "nodes": [
            {"id": "floor-main", "type": "FloorNode", "data": {"policy": "SEQUENTIAL"}},
        ],
        "edges": [],
    }
    app = build_app(initial_config=config, authenticate=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/session/initial")
    assert response.status_code == 200
    result = response.json()
    assert result is not None
    assert result["nodes"][0]["data"]["policy"] == "SEQUENTIAL"


async def test_presets_list_returns_list() -> None:
    app = build_app(authenticate=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/presets/list")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    # _fixture_test.json must appear
    names = [p["name"] for p in data]
    assert "_fixture_test" in names
    first = next(p for p in data if p["name"] == "_fixture_test")
    assert first["title"] == "Test Fixture"
    assert first["description"] == "Used by tests only."
    assert first["policy"] == "SEQUENTIAL"


async def test_presets_get_returns_config() -> None:
    app = build_app(authenticate=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/presets/_fixture_test")
    assert response.status_code == 200
    data = response.json()
    assert data["nodes"][0]["type"] == "FloorNode"


async def test_presets_get_unknown_returns_404() -> None:
    app = build_app(authenticate=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/presets/does_not_exist")
    assert response.status_code == 404


@pytest.mark.parametrize("path", [
    "../result-backup/secret.txt", "..\\result-backup\\secret.txt",
    "nested/../../secret.txt", "C:\\private\\secret.txt", "C:secret.txt",
    "\\\\server\\share\\secret.txt", "/etc/passwd",
])
def test_media_path_rejects_cross_platform_escapes(path):
    with pytest.raises(HTTPException) as exc:
        _safe_media_path(path)
    assert exc.value.status_code == 400


def test_media_path_accepts_descendant(tmp_path, monkeypatch):
    monkeypatch.setattr("canvas.backend.main.RESULT_DIR", tmp_path)
    assert _safe_media_path("session\\images\\one.png") == tmp_path / "session/images/one.png"
