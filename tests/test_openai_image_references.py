from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from ofp_playground.agents.llm.openai_image import (
    OpenAIImageAgent,
    _extract_reference_image_paths,
)
from ofp_playground.bus.message_bus import MessageBus


def test_extract_reference_image_paths_removes_marker_block() -> None:
    clean_prompt, paths = _extract_reference_image_paths(
        "REFERENCE_IMAGES:\n"
        "- /tmp/repa.png\n"
        "- result/session/images/oru.jpg\n"
        "PROMPT: Repa and Oru clash in a rain-bright tournament arena."
    )

    assert [path.as_posix() for path in paths] == [
        "/tmp/repa.png",
        "result/session/images/oru.jpg",
    ]
    assert "REFERENCE_IMAGES" not in clean_prompt
    assert "repa.png" not in clean_prompt
    assert "PROMPT: Repa and Oru clash" in clean_prompt


@pytest.mark.asyncio
async def test_openai_image_generation_uses_edit_endpoint_for_reference_images(
    tmp_path, monkeypatch
) -> None:
    reference = tmp_path / "repa.png"
    reference.write_bytes(b"\x89PNG\r\n\x1a\nreference")

    class FakeImages:
        def __init__(self) -> None:
            self.edit_calls = []
            self.generate_calls = []

        def edit(self, **kwargs):
            self.edit_calls.append(kwargs)
            assert len(kwargs["image"]) == 1
            assert kwargs["image"][0].read().startswith(b"\x89PNG")
            return SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(b"\xff\xd8\xffimage").decode())]
            )

        def generate(self, **kwargs):
            self.generate_calls.append(kwargs)
            raise AssertionError("reference image prompts must use images.edit")

    fake_images = FakeImages()
    fake_client = SimpleNamespace(images=fake_images)

    agent = OpenAIImageAgent(
        name="IllustrationGen",
        style="ink wash",
        bus=MessageBus(),
        conversation_id="conv:test",
        api_key="test-key",
        model="gpt-image-1",
    )
    agent._output_dir = tmp_path
    monkeypatch.setattr(agent, "_get_client", lambda: fake_client)

    async def no_retry(coro_fn):
        return await coro_fn()

    monkeypatch.setattr(agent, "_call_with_retry", no_retry)

    output, response_id = await agent._generate_image(
        f"REFERENCE_IMAGES: {reference}\n"
        "PROMPT: Draw Repa in a low-angle tournament pose."
    )

    assert output is not None
    assert output.exists()
    assert response_id is None  # Images API doesn't produce a Responses API chain ID.
    assert fake_images.edit_calls
    assert not fake_images.generate_calls
    assert "REFERENCE_IMAGES" not in fake_images.edit_calls[0]["prompt"]
    assert "Draw Repa" in fake_images.edit_calls[0]["prompt"]
