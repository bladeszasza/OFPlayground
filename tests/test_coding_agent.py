"""Tests for CodingAgent (codex.py)."""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ofp_playground.bus.message_bus import FLOOR_MANAGER_URI, MessageBus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(tmp_path, provider="openai"):
    from ofp_playground.agents.llm.codex import CodingAgent
    a = CodingAgent(
        name="Coder",
        synopsis="You are a coding agent.",
        bus=MessageBus(),
        conversation_id="test-conv-1",
        api_key="test-key",
        provider=provider,
    )
    a._output_dir = tmp_path / "ofp-code"
    a._output_dir.mkdir()
    return a


def _make_utterance_envelope(text: str, sender_uri: str):
    from openfloor import DialogEvent, Envelope, Sender, TextFeature, Token, UtteranceEvent
    from openfloor import Conversation
    return Envelope(
        sender=Sender(speakerUri=sender_uri, serviceUrl="local://test"),
        conversation=Conversation(id="test-conv-1"),
        events=[
            UtteranceEvent(
                dialogEvent=DialogEvent(
                    id=str(uuid.uuid4()),
                    speakerUri=sender_uri,
                    features={
                        "text": TextFeature(
                            mimeType="text/plain",
                            tokens=[Token(value=text)],
                        )
                    },
                )
            )
        ],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_task_type(tmp_path):
    agent = _make_agent(tmp_path)
    assert agent.task_type == "code-generation"


def test_output_dir_created(tmp_path, monkeypatch):
    from ofp_playground.agents.llm import codex as codex_module
    target = tmp_path / "ofp-code-new"
    monkeypatch.setattr(codex_module, "OUTPUT_DIR", target)
    from ofp_playground.agents.llm.codex import CodingAgent
    CodingAgent(
        name="C", synopsis="s", bus=MessageBus(),
        conversation_id="c", api_key="k", provider="openai",
    )
    assert target.exists()


@pytest.mark.asyncio
async def test_directive_parsing(tmp_path):
    agent = _make_agent(tmp_path)
    text = "[DIRECTIVE for Coder]: Write a Python hello world script."
    envelope = _make_utterance_envelope(text, sender_uri=FLOOR_MANAGER_URI)

    with patch.object(agent, "request_floor", new_callable=AsyncMock) as mock_rf:
        await agent._handle_utterance(envelope)
        mock_rf.assert_not_called()

    # _task_directive stores the full raw directive text (incl. "[DIRECTIVE for Coder]:" prefix),
    # NOT just the parsed instruction extracted by _parse_showrunner_message.
    assert "DIRECTIVE for Coder" in agent._task_directive


@pytest.mark.asyncio
async def test_ignores_own_utterance(tmp_path):
    agent = _make_agent(tmp_path)
    envelope = _make_utterance_envelope("hello", sender_uri=agent.speaker_uri)
    await agent._handle_utterance(envelope)
    assert agent._task_directive == ""


@pytest.mark.asyncio
async def test_unsupported_provider_raises(tmp_path):
    agent = _make_agent(tmp_path, provider="anthropic")
    with pytest.raises(NotImplementedError, match="not yet implemented"):
        await agent._handle_grant_floor()


@pytest.mark.asyncio
async def test_progress_utterance_is_private(tmp_path):
    agent = _make_agent(tmp_path)
    sent = []
    agent._bus.send_private = AsyncMock(side_effect=lambda env, uri: sent.append((env, uri)))
    await agent._send_progress("Working on it...")
    assert len(sent) == 1
    _, target_uri = sent[0]
    assert target_uri == FLOOR_MANAGER_URI


@pytest.mark.asyncio
async def test_final_envelope_bundles_yield(tmp_path):
    agent = _make_agent(tmp_path)
    sent = []
    agent._bus.send = AsyncMock(side_effect=lambda env: sent.append(env))
    await agent._send_final_and_yield("Task complete.")
    assert len(sent) == 1
    assert len(sent[0].events) == 2
    # UtteranceEvent has no eventType attr; Event("yieldFloor") does
    event_type_values = [getattr(e, "eventType", "") for e in sent[0].events]
    assert "yieldFloor" in event_type_values
    # OFP: utterance must precede yieldFloor in bundled envelope
    assert event_type_values[0] != "yieldFloor"  # first event is the utterance
    assert event_type_values[1] == "yieldFloor"  # second event is the yield


@pytest.mark.asyncio
async def test_files_saved_to_ofp_code(tmp_path):
    agent = _make_agent(tmp_path)
    fake_bytes = b"print('hello')"

    # Build mock streaming events sequence
    progress_event = MagicMock()
    progress_event.type = "response.output_item.added"
    progress_event.item = MagicMock(type="code_interpreter_call")

    file_done_event = MagicMock()
    file_done_event.type = "response.output_item.done"
    mock_file = MagicMock(file_id="file-abc123", filename="solution.py")
    mock_output = MagicMock(type="files", files=[mock_file])
    file_done_event.item = MagicMock(type="code_interpreter_call", outputs=[mock_output])

    mock_final = MagicMock()
    mock_final.output = []

    mock_stream = AsyncMock()
    mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
    mock_stream.__aexit__ = AsyncMock(return_value=False)
    mock_stream.__aiter__.return_value = iter([progress_event, file_done_event])
    mock_stream.get_final_response = AsyncMock(return_value=mock_final)

    mock_file_content = MagicMock(content=fake_bytes)
    mock_client = AsyncMock()
    mock_client.responses.stream = MagicMock(return_value=mock_stream)
    mock_client.files.content = AsyncMock(return_value=mock_file_content)
    mock_client.aclose = AsyncMock()

    agent.send_private_utterance = AsyncMock()

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        _, saved = await agent._run_openai_coding_loop("test context")

    assert len(saved) == 1
    assert saved[0].exists()
    assert saved[0].read_bytes() == fake_bytes
    assert "solution" in saved[0].name
