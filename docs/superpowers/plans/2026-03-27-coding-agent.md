# CodingAgent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `WebPageAgent` with a general-purpose `CodingAgent` (`codex.py`) that uses OpenAI's Responses API + `code_interpreter` tool, holds the floor through a full agentic coding loop, and saves generated files to `ofp-code/`.

**Architecture:** `CodingAgent` extends `BaseLLMAgent`, overriding `_handle_utterance` (directive collection) and `_handle_grant_floor` (full async streaming loop). Progress signals go via `send_private_utterance` to the floor manager. Final result + `yieldFloor` are bundled in one envelope.

**Tech Stack:** Python 3.11+, `openai` (AsyncOpenAI, streaming Responses API), `openfloor`, `pytest`, `pytest-asyncio`.

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Create | `src/ofp_playground/agents/llm/codex.py` | `CodingAgent` class — entire agent implementation |
| Delete | `src/ofp_playground/agents/llm/web_page.py` | Replaced by codex.py |
| Delete | `src/ofp_playground/agents/llm/web_showcase.py` | Was a shim — no longer needed |
| Modify | `src/ofp_playground/agents/llm/__init__.py` | Swap WebPageAgent for CodingAgent |
| Modify | `src/ofp_playground/cli.py` | `web-page-generation` → `code-generation` for all 4 providers |
| Create | `tests/test_coding_agent.py` | All 8 unit tests |
| Modify | `docs/agents.md` | Replace WebPageAgent section |
| Modify | `docs/architecture.md` | Update agent tree + output dir |
| Modify | `docs/cli.md` | Update task type table |
| Modify | `docs/orchestration.md` | Update example table rows |
| Modify | `docs/output.md` | Replace HTML output section |
| Modify | `docs/ofp-protocol.md` | Update keyphrases table |
| Modify | `docs/configuration.md` | Update model defaults table |
| Modify | `CLAUDE.md` | Output dir list + agent hierarchy |
| Modify | `examples/showcase_web.sh` | FrontendDev → openai:code-generation |
| Modify | `examples/sequential_code_review.sh` | Add CodeFixer agent |
| Create | `examples/breakout_code_review.sh` | New BREAKOUT + CodingAgent example |

---

## Task 1: Write all failing tests

**Files:**
- Create: `tests/test_coding_agent.py`

- [ ] **Step 1: Write the test file**

```python
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
    # UtteranceEvent has no eventType attr; Event("yieldFloor") does
    event_type_values = [getattr(e, "eventType", "") for e in sent[0].events]
    assert "yieldFloor" in event_type_values
    assert len(sent[0].events) == 2


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
    mock_stream.__aiter__ = MagicMock(return_value=iter([progress_event, file_done_event]))
    mock_stream.get_final_response = AsyncMock(return_value=mock_final)

    mock_file_content = MagicMock(content=fake_bytes)
    mock_client = AsyncMock()
    mock_client.responses.stream = MagicMock(return_value=mock_stream)
    mock_client.files.content = AsyncMock(return_value=mock_file_content)
    mock_client.aclose = AsyncMock()

    agent.send_private_utterance = AsyncMock()

    with patch("ofp_playground.agents.llm.codex.AsyncOpenAI", return_value=mock_client):
        _, saved = await agent._run_openai_coding_loop("test context")

    assert len(saved) == 1
    assert saved[0].exists()
    assert saved[0].read_bytes() == fake_bytes
    assert "solution" in saved[0].name
```

- [ ] **Step 2: Run tests — confirm all fail with ImportError**

```bash
cd /Users/bolyos/Development/ofpPlaygorund
pytest tests/test_coding_agent.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'ofp_playground.agents.llm.codex'`

---

## Task 2: Create codex.py skeleton (init + task_type + output_dir)

**Files:**
- Create: `src/ofp_playground/agents/llm/codex.py`

- [ ] **Step 1: Create the file**

```python
"""Coding agent — general-purpose code generation via OpenAI Responses API + code_interpreter."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from openfloor import Envelope

from ofp_playground.agents.llm.base import BaseLLMAgent
from ofp_playground.bus.message_bus import FLOOR_MANAGER_URI, MessageBus

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("ofp-code")
DEFAULT_MODEL_OPENAI = "gpt-5.4-long-context"


class CodingAgent(BaseLLMAgent):
    """General-purpose coding agent. OpenAI: Responses API + code_interpreter agentic loop.

    Receives [DIRECTIVE for Name]: tasks from the orchestrator, holds the floor
    through the full coding loop, then yields with saved file paths.
    """

    def __init__(
        self,
        name: str,
        synopsis: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        provider: str,
        model: Optional[str] = None,
    ):
        super().__init__(
            name=name,
            synopsis=synopsis,
            bus=bus,
            conversation_id=conversation_id,
            model=model,
            relevance_filter=False,
            api_key=api_key,
        )
        self._provider = provider.lower()
        if not self._model:
            self._model = DEFAULT_MODEL_OPENAI if self._provider in ("openai", "gpt") else None
        self._task_directive: str = ""
        self._output_dir: Path = OUTPUT_DIR
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def task_type(self) -> str:
        return "code-generation"

    async def _generate_response(self, participants):
        raise NotImplementedError("CodingAgent overrides _handle_grant_floor directly")

    async def _quick_check(self, prompt: str) -> str:
        return "NO"
```

- [ ] **Step 2: Run structural tests**

```bash
pytest tests/test_coding_agent.py::test_task_type tests/test_coding_agent.py::test_output_dir_created -v
```

Expected: both PASS

- [ ] **Step 3: Commit**

```bash
git add src/ofp_playground/agents/llm/codex.py tests/test_coding_agent.py
git commit -m "feat: add CodingAgent skeleton with task_type and output_dir"
```

---

## Task 3: Implement `_handle_utterance` (directive parsing)

**Files:**
- Modify: `src/ofp_playground/agents/llm/codex.py`

- [ ] **Step 1: Add `_handle_utterance` to CodingAgent** (after `_quick_check`)

```python
    async def _handle_utterance(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return

        text = self._extract_text_from_envelope(envelope)
        if not text:
            return

        if "[DIRECTIVE for" in text:
            if self._parse_showrunner_message(text):
                self._task_directive = text
            # FloorManager sends grantFloor directly — never call request_floor()
            return

        # Passive observation only
        sender_name = self._name_registry.get(sender_uri, sender_uri.split(":")[-1])
        self._append_to_context(sender_name, text, is_self=False)
```

- [ ] **Step 2: Run directive tests**

```bash
pytest tests/test_coding_agent.py::test_directive_parsing tests/test_coding_agent.py::test_ignores_own_utterance -v
```

Expected: both PASS

- [ ] **Step 3: Commit**

```bash
git add src/ofp_playground/agents/llm/codex.py
git commit -m "feat: CodingAgent directive parsing in _handle_utterance"
```

---

## Task 4: Implement `_send_progress` and `_send_final_and_yield`

**Files:**
- Modify: `src/ofp_playground/agents/llm/codex.py`

- [ ] **Step 1: Add both methods to CodingAgent** (after `_handle_utterance`)

```python
    async def _send_progress(self, message: str) -> None:
        """Private whisper to floor manager — liveness signal during coding loop."""
        await self.send_private_utterance(
            f"[{self._name}] {message}", FLOOR_MANAGER_URI
        )

    async def _send_final_and_yield(self, result: str) -> None:
        """Bundle final utterance + yieldFloor in one OFP envelope."""
        from openfloor import DialogEvent, Event, TextFeature, Token, UtteranceEvent
        import uuid
        dialog_event = DialogEvent(
            id=str(uuid.uuid4()),
            speakerUri=self._speaker_uri,
            features={
                "text": TextFeature(
                    mimeType="text/plain",
                    tokens=[Token(value=result)],
                )
            },
        )
        envelope = Envelope(
            sender=self._make_sender(),
            conversation=self._make_conversation(),
            events=[
                UtteranceEvent(dialogEvent=dialog_event),
                Event(eventType="yieldFloor", reason="@complete"),
            ],
        )
        await self.send_envelope(envelope)
```

- [ ] **Step 2: Run progress and final envelope tests**

```bash
pytest tests/test_coding_agent.py::test_progress_utterance_is_private tests/test_coding_agent.py::test_final_envelope_bundles_yield -v
```

Expected: both PASS

- [ ] **Step 3: Commit**

```bash
git add src/ofp_playground/agents/llm/codex.py
git commit -m "feat: CodingAgent progress whisper and bundled final-yield envelope"
```

---

## Task 5: Implement `_build_context` and `_run_openai_coding_loop`

**Files:**
- Modify: `src/ofp_playground/agents/llm/codex.py`

- [ ] **Step 1: Add both methods** (after `_send_final_and_yield`)

```python
    def _build_context(self) -> str:
        parts = ["=== CODING TASK CONTEXT ===\n"]
        if self._task_directive:
            parts.append(f"## TASK DIRECTIVE\n{self._task_directive}\n")
        parts.append(
            "\n=== OUTPUT CONTRACT ===\n"
            "Implement the task completely using code_interpreter. "
            "Save all output files. "
            "Return ONLY the implementation — no preamble, no explanation unless requested.\n"
        )
        return "\n".join(parts)

    async def _run_openai_coding_loop(self, context: str) -> tuple[str, list[Path]]:
        """Run OpenAI Responses API streaming with code_interpreter.

        Returns (output_text, saved_files).
        NOTE: event attribute paths (item.outputs, output.files, etc.) reflect the
        openai SDK as of 2026-03. Verify against sdk changelog if the API changes.
        """
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self._api_key)
        output_text = ""
        file_ids: list[tuple[str, str]] = []  # (file_id, filename)

        try:
            async with client.responses.stream(
                model=self._model,
                instructions=self._synopsis,
                input=context,
                tools=[{"type": "code_interpreter"}],
                reasoning={"effort": "high"},
                max_output_tokens=32000,
            ) as stream:
                async for event in stream:
                    event_type = getattr(event, "type", "")

                    if event_type == "response.output_item.added":
                        item_type = getattr(getattr(event, "item", None), "type", "")
                        if item_type == "code_interpreter_call":
                            await self._send_progress("Running code_interpreter...")

                    elif event_type == "response.output_item.done":
                        item = getattr(event, "item", None)
                        if item and getattr(item, "type", "") == "code_interpreter_call":
                            for out in (getattr(item, "outputs", None) or []):
                                if getattr(out, "type", "") == "files":
                                    for f in (getattr(out, "files", None) or []):
                                        file_ids.append((f.file_id, f.filename))
                                elif getattr(out, "type", "") == "logs":
                                    logs = (getattr(out, "logs", "") or "").strip()
                                    if logs:
                                        await self._send_progress(f"Output: {logs[:120]}")

                final = await stream.get_final_response()
                for item in (final.output or []):
                    if getattr(item, "type", "") == "message":
                        for part in (getattr(item, "content", None) or []):
                            if getattr(part, "type", "") == "output_text":
                                output_text += getattr(part, "text", "")

        finally:
            await client.aclose()

        # Download files produced by code_interpreter
        saved_files: list[Path] = []
        dl_client = AsyncOpenAI(api_key=self._api_key)
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = re.sub(r"[^\w]+", "_", self._name.lower())
            for file_id, filename in file_ids:
                try:
                    content = await dl_client.files.content(file_id)
                    stem = Path(filename).stem
                    ext = Path(filename).suffix or ".bin"
                    out_path = self._output_dir / f"{ts}_{slug}_{stem}{ext}"
                    out_path.write_bytes(content.content)
                    saved_files.append(out_path)
                    await self._send_progress(f"Saved {out_path.name}")
                except Exception as e:
                    logger.error("[%s] Failed to save file %s: %s", self._name, file_id, e)
        finally:
            await dl_client.aclose()

        return output_text, saved_files
```

- [ ] **Step 2: Run the file-save test**

```bash
pytest tests/test_coding_agent.py::test_files_saved_to_ofp_code -v
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/ofp_playground/agents/llm/codex.py
git commit -m "feat: CodingAgent OpenAI streaming loop with code_interpreter and file download"
```

---

## Task 6: Implement `_handle_grant_floor`

**Files:**
- Modify: `src/ofp_playground/agents/llm/codex.py`

- [ ] **Step 1: Add `_handle_grant_floor`** (after `_run_openai_coding_loop`)

```python
    async def _handle_grant_floor(self) -> None:
        """Floor granted — run agentic coding loop, save files, yield when complete."""
        self._has_floor = True
        try:
            if self._provider not in ("openai", "gpt"):
                raise NotImplementedError(
                    f"CodingAgent provider '{self._provider}' is not yet implemented. Use 'openai'."
                )

            await self._send_progress("Starting coding task...")
            context = self._build_context()
            output_text, saved_files = await self._run_openai_coding_loop(context)

            lines: list[str] = []
            if output_text:
                lines.append(output_text.strip())
            for path in saved_files:
                lines.append(f"File saved: {path.resolve()}")
            result = "\n".join(lines) if lines else "Coding task complete (no file output)."

            await self._send_final_and_yield(result)

        except NotImplementedError:
            raise
        except Exception as e:
            logger.error("[%s] Coding loop error: %s", self._name, e, exc_info=True)
            await self._send_final_and_yield(f"Coding task failed: {str(e)[:200]}")
        finally:
            self._has_floor = False
            self._task_directive = ""
```

- [ ] **Step 2: Run the unsupported-provider test**

```bash
pytest tests/test_coding_agent.py::test_unsupported_provider_raises -v
```

Expected: PASS

- [ ] **Step 3: Run the full test suite**

```bash
pytest tests/test_coding_agent.py -v
```

Expected: all 8 tests PASS

- [ ] **Step 4: Run existing tests to check for regressions**

```bash
pytest tests/ -v --ignore=tests/test_coding_agent.py -x -q 2>&1 | tail -20
```

Expected: all pass (no regressions)

- [ ] **Step 5: Commit**

```bash
git add src/ofp_playground/agents/llm/codex.py
git commit -m "feat: CodingAgent _handle_grant_floor with agentic loop and NotImplementedError stub"
```

---

## Task 7: Update `__init__.py`

**Files:**
- Modify: `src/ofp_playground/agents/llm/__init__.py`

Current content to replace:

```python
# (no WebPageAgent import currently — it's imported inline in cli.py)
# __init__.py does not currently export WebPageAgent
```

- [ ] **Step 1: Add CodingAgent export**

Open `src/ofp_playground/agents/llm/__init__.py`. Add at the end of imports and `__all__`:

```python
from .codex import CodingAgent
```

And add `"CodingAgent"` to `__all__`.

Full file after edit:

```python
from .anthropic import AnthropicAgent
from .anthropic_vision import AnthropicVisionAgent
from .openai import OpenAIAgent
from .openai_image import OpenAIImageAgent, OpenAIVisionAgent
from .google import GoogleAgent
from .google_image import GeminiImageAgent, GeminiVisionAgent
from .google_music import GeminiMusicAgent
from .huggingface import HuggingFaceAgent
from .multimodal import MultimodalAgent
from .classifier import ImageClassificationAgent
from .detector import ObjectDetectionAgent
from .segmenter import ImageSegmentationAgent
from .ocr import OCRAgent
from .text_classifier import TextClassificationAgent
from .ner import NERAgent
from .summarizer import SummarizationAgent
from .codex import CodingAgent

__all__ = [
    "AnthropicAgent",
    "AnthropicVisionAgent",
    "OpenAIAgent",
    "OpenAIImageAgent",
    "OpenAIVisionAgent",
    "GoogleAgent",
    "GeminiImageAgent",
    "GeminiVisionAgent",
    "GeminiMusicAgent",
    "HuggingFaceAgent",
    "MultimodalAgent",
    "ImageClassificationAgent",
    "ObjectDetectionAgent",
    "ImageSegmentationAgent",
    "OCRAgent",
    "TextClassificationAgent",
    "NERAgent",
    "SummarizationAgent",
    "CodingAgent",
]
```

- [ ] **Step 2: Verify import works**

```bash
python -c "from ofp_playground.agents.llm import CodingAgent; print(CodingAgent)"
```

Expected: `<class 'ofp_playground.agents.llm.codex.CodingAgent'>`

- [ ] **Step 3: Commit**

```bash
git add src/ofp_playground/agents/llm/__init__.py
git commit -m "feat: export CodingAgent from agents.llm package"
```

---

## Task 8: Update `cli.py` — replace `web-page-generation` with `code-generation`

**Files:**
- Modify: `src/ofp_playground/cli.py`

There are 4 blocks — one per provider. All follow the same pattern.
Find each block with `elif task in ("web-page", "web-page-generation", "web-showcase"):` and replace it.

- [ ] **Step 1: Replace Anthropic block** (~line 537)

Old:
```python
        elif task in ("web-page", "web-page-generation", "web-showcase"):
            from ofp_playground.agents.llm.web_page import WebPageAgent
            agent = WebPageAgent(
                name=name, synopsis=description, bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key, provider="anthropic",
                model=model_override or None,
            )
```

New:
```python
        elif task == "code-generation":
            from ofp_playground.agents.llm.codex import CodingAgent
            agent = CodingAgent(
                name=name, synopsis=description, bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key, provider="anthropic",
                model=model_override or None,
            )
```

Also update the `else` error message on the line after to include `code-generation` instead of `web-page-generation`.

- [ ] **Step 2: Replace OpenAI block** (~line 612)

Old:
```python
        elif task in ("web-page", "web-page-generation", "web-showcase"):
            from ofp_playground.agents.llm.web_page import WebPageAgent
            agent = WebPageAgent(
                name=name, synopsis=description, bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key, provider="openai",
                model=model_override or None,
            )
```

New:
```python
        elif task == "code-generation":
            from ofp_playground.agents.llm.codex import CodingAgent
            agent = CodingAgent(
                name=name, synopsis=description, bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key, provider="openai",
                model=model_override or None,
            )
```

- [ ] **Step 3: Replace Google block** (~line 697)

Old:
```python
        elif task in ("web-page", "web-page-generation", "web-showcase"):
            from ofp_playground.agents.llm.web_page import WebPageAgent
            agent = WebPageAgent(
                name=name, synopsis=description, bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key, provider="google",
                model=model_override or None,
            )
```

New:
```python
        elif task == "code-generation":
            from ofp_playground.agents.llm.codex import CodingAgent
            agent = CodingAgent(
                name=name, synopsis=description, bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key, provider="google",
                model=model_override or None,
            )
```

- [ ] **Step 4: Replace HuggingFace block** (~line 863)

Old:
```python
        elif task in ("web-page", "web-page-generation", "web-showcase"):
            from ofp_playground.agents.llm.web_page import WebPageAgent
            agent = WebPageAgent(
                name=name, synopsis=description, bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key, provider="hf",
                model=model_override or None,
            )
```

New:
```python
        elif task == "code-generation":
            from ofp_playground.agents.llm.codex import CodingAgent
            agent = CodingAgent(
                name=name, synopsis=description, bus=bus,
                conversation_id=floor.conversation_id,
                api_key=api_key, provider="hf",
                model=model_override or None,
            )
```

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/ -v -x -q 2>&1 | tail -20
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/ofp_playground/cli.py
git commit -m "feat: cli web-page-generation → code-generation, wired to CodingAgent"
```

---

## Task 9: Delete `web_page.py` and `web_showcase.py`

**Files:**
- Delete: `src/ofp_playground/agents/llm/web_page.py`
- Delete: `src/ofp_playground/agents/llm/web_showcase.py`

- [ ] **Step 1: Delete both files**

```bash
git rm src/ofp_playground/agents/llm/web_page.py src/ofp_playground/agents/llm/web_showcase.py
```

- [ ] **Step 2: Verify no remaining references**

```bash
grep -r "web_page\|web_showcase\|WebPageAgent\|WebShowcaseAgent" src/ tests/ --include="*.py"
```

Expected: no output

- [ ] **Step 3: Run tests one more time**

```bash
pytest tests/ -v -x -q 2>&1 | tail -10
```

Expected: all pass

- [ ] **Step 4: Commit**

```bash
git commit -m "refactor: delete WebPageAgent and WebShowcaseAgent (replaced by CodingAgent)"
```

---

## Task 10: Update docs

**Files:**
- Modify: `docs/agents.md`, `docs/architecture.md`, `docs/cli.md`, `docs/orchestration.md`, `docs/output.md`, `docs/ofp-protocol.md`, `docs/configuration.md`, `CLAUDE.md`

- [ ] **Step 1: `docs/agents.md`** — replace lines ~260–282

Find the `### WebPageAgent` section and replace it entirely:

```markdown
### CodingAgent

**File**: `src/ofp_playground/agents/llm/codex.py`
**CLI type**: `code-generation`
**Providers**: OpenAI (full), Anthropic / Google / HuggingFace (stub — `NotImplementedError`)

General-purpose coding agent. Receives `[DIRECTIVE for Name]:` task assignments from the ShowRunner/FloorManager, holds the floor through a full OpenAI Responses API + `code_interpreter` agentic loop, and saves generated files to `ofp-code/`.

Progress signals are sent as private utterances to the floor manager during the loop (liveness signal). Final result utterance and `yieldFloor` are bundled in one envelope (`reason: "@complete"`).

**Output directory**: `ofp-code/`
**Default model (OpenAI)**: `gpt-5.4-long-context`
**Task type keyphrase**: `code-generation`
```

- [ ] **Step 2: `docs/architecture.md`** — two changes

1. In the agent tree, replace `WebPageAgent — HTML page generator` with:
   ```
   └── CodingAgent         — code generation agent (OpenAI code_interpreter)
   ```
2. In the output directories section, replace `web/   ← HTML pages (WebPageAgent)` with:
   ```
   ├── code/        ← generated code files (CodingAgent)
   ```

- [ ] **Step 3: `docs/cli.md`** — three changes

1. Replace lines 135–137 (task type table):
   ```markdown
   | `code-generation` | General-purpose coding agent | OpenAI (full), others stub |
   ```

2. Replace the code example at line 83:
   ```bash
   --agent "openai:code-generation:CodeFixer:You are a coding agent. Implement the assigned task."
   ```

3. Remove any mention of `web-page`, `web-showcase` aliases.

- [ ] **Step 4: `docs/orchestration.md`** — lines 288–289

Replace:
```
| ChapterBuilder | HuggingFace (web-page-generation, DeepSeek) | HTML chapter pages |
| IndexBuilder | Anthropic (web-page-generation, Haiku) | Master index.html |
```
With:
```
| ChapterBuilder | OpenAI (code-generation) | HTML chapter pages |
| IndexBuilder | OpenAI (code-generation) | Master index.html |
```

- [ ] **Step 5: `docs/output.md`** — replace HTML output section

Find the section that starts "Self-contained HTML pages from `WebPageAgent`..." (line ~93) and replace:

```markdown
Generated code files from `CodingAgent`. Files are saved to `ofp-code/` with the naming pattern `<timestamp>_<agentname>_<filename>`. The agent reports saved file paths in its final utterance back to the conversation.
```

- [ ] **Step 6: `docs/ofp-protocol.md`** — keyphrases table

Find the row `| WebPageAgent | ["web-page-generation"] |` and replace:
```markdown
| CodingAgent | `["code-generation"]` |
```

- [ ] **Step 7: `docs/configuration.md`** — model defaults table (lines 93–96)

Replace:
```markdown
| WebPageAgent (Anthropic) | `claude-sonnet-4-6` |
| WebPageAgent (OpenAI) | `gpt-5.4-long-context` |
| WebPageAgent (Google) | `gemini-3.1-pro-preview` |
| WebPageAgent (HF) | `deepseek-ai/DeepSeek-V3.2` |
```
With:
```markdown
| CodingAgent (OpenAI) | `gpt-5.4-long-context` |
| CodingAgent (Anthropic / Google / HF) | stub — not yet implemented |
```

- [ ] **Step 8: `CLAUDE.md`** — two changes

1. In the output directories section, replace `ofp-web/` with `ofp-code/`.
2. In the agent hierarchy diagram, replace `WebPageAgent — HTML page generator` with `CodingAgent — code generation agent`.

- [ ] **Step 9: Commit docs**

```bash
git add docs/ CLAUDE.md
git commit -m "docs: replace WebPageAgent references with CodingAgent across all docs"
```

---

## Task 11: Update `examples/showcase_web.sh`

**Files:**
- Modify: `examples/showcase_web.sh`

- [ ] **Step 1: Update FrontendDev agent line** (~line 424)

Old:
```bash
  --agent "anthropic:web-page-generation:FrontendDev:${FRONTEND_DEV_PROMPT}" \
```

New:
```bash
  --agent "openai:code-generation:FrontendDev:${FRONTEND_DEV_PROMPT}" \
```

- [ ] **Step 2: Update comment header** (lines 1–4)

Change the comment line that lists agent providers:
```bash
# Keys: ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, HF_API_KEY
```
(no change needed — already lists OPENAI_API_KEY)

Also update the SPAWN CAPABILITIES section (line 106–110) in `DIRECTOR_MISSION`:

Old:
```
WEB PAGE GENERATION (output: HTML files in session folder)
  hf:web-page-generation     — HTML builder via HF (recommended: moonshotai/Kimi-K2.5)
  anthropic:web-page-generation
  openai:web-page-generation
  google:web-page-generation
```

New:
```
CODE GENERATION (output: files in ofp-code/)
  openai:code-generation     — coding agent via OpenAI code_interpreter (default: gpt-5.4-long-context)
```

- [ ] **Step 3: Commit**

```bash
git add examples/showcase_web.sh
git commit -m "feat: showcase_web.sh FrontendDev → openai:code-generation"
```

---

## Task 12: Update `examples/sequential_code_review.sh` and create `examples/breakout_code_review.sh`

**Files:**
- Modify: `examples/sequential_code_review.sh`
- Create: `examples/breakout_code_review.sh`

- [ ] **Step 1: Add CodeFixer to `sequential_code_review.sh`**

Add `CODEFIXER_PROMPT` variable before the `if [ -n "$SNIPPET" ]` block:

```bash
CODEFIXER_PROMPT="You are CodeFixer, a senior software engineer.
You will receive the original code snippet plus consolidated review findings (security, performance, style).
Implement ALL suggested fixes in one shot using code_interpreter.
Output the corrected file via code_interpreter. Do not explain — just produce the fixed code."
```

Then add `--agent "openai:code-generation:CodeFixer:\${CODEFIXER_PROMPT}"` to both `ofp-playground start` calls (automatic and interactive mode).

Also add `OPENAI_API_KEY` to the Requirements comment.

- [ ] **Step 2: Create `examples/breakout_code_review.sh`**

```bash
#!/usr/bin/env bash
# OFP Playground — Breakout Code Review + CodingAgent Fix
#
# Policy: showrunner_driven
# Flow:
#   1. Human submits a code snippet
#   2. Director fires a BREAKOUT with 3 specialist reviewers (isolated sub-floor)
#   3. Breakout summary (~200 words) is injected into Director's next context
#   4. Director assigns CodeFixer (CodingAgent) with snippet + review summary
#   5. CodeFixer holds floor, runs code_interpreter, implements all fixes
#   6. Director accepts and ends session
#
# Keys: ANTHROPIC_API_KEY, OPENAI_API_KEY

DIRECTOR_MISSION="You are Director — orchestrator of a code review and fix pipeline.

YOUR TEAM:
- CodeFixer  — OpenAI coding agent that implements fixes

WORKFLOW (follow this exact order):

PHASE 1 — Review
  1. When the human submits code, launch a breakout review:
     [BREAKOUT policy=sequential max_rounds=3 topic=Code review for the submitted snippet]
     [BREAKOUT_AGENT -provider anthropic -name SecurityReviewer -system \"You are SecurityReviewer. Review strictly for OWASP Top 10, hardcoded secrets, unsafe API usage. Format: SEVERITY / FINDINGS / RECOMMENDATION.\"]
     [BREAKOUT_AGENT -provider anthropic -name PerformanceReviewer -system \"You are PerformanceReviewer. Review strictly for algorithmic complexity, memory, blocking calls. Format: COMPLEXITY / HOT PATHS / OPTIMIZATION.\"]
     [BREAKOUT_AGENT -provider openai -name StyleReviewer -system \"You are StyleReviewer. Review strictly for naming, readability, idioms. Format: READABILITY / ISSUES / SUGGESTION.\"]

PHASE 2 — Fix
  2. After the breakout, assign CodeFixer with the original snippet AND the full review summary:
     [ASSIGN CodeFixer]: ORIGINAL CODE:
     <paste original code verbatim>
     ---
     REVIEW SUMMARY:
     <paste full breakout summary>
     ---
     Implement ALL fixes. Save the corrected file.

PHASE 3 — Done
  3. [ACCEPT] CodeFixer's output, then [TASK_COMPLETE].

RULES:
- Never produce code yourself — always delegate to CodeFixer.
- If CodeFixer fails once, [REJECT CodeFixer]: specific feedback.
- If CodeFixer fails twice, [SKIP CodeFixer]: unable to fix — move on."

CODEFIXER_PROMPT="You are CodeFixer, a senior software engineer.
You receive an ORIGINAL CODE snippet and a REVIEW SUMMARY.
Implement ALL suggested fixes using code_interpreter.
Save the corrected file. Return the file path only."

ofp-playground web \
  --human-name Developer \
  --policy showrunner_driven \
  --max-turns 200 \
  --agent "anthropic:orchestrator:Director:${DIRECTOR_MISSION}" \
  --agent "openai:code-generation:CodeFixer:${CODEFIXER_PROMPT}" \
  --port 7861
```

- [ ] **Step 3: Make both scripts executable**

```bash
chmod +x examples/sequential_code_review.sh examples/breakout_code_review.sh
```

- [ ] **Step 4: Commit**

```bash
git add examples/sequential_code_review.sh examples/breakout_code_review.sh
git commit -m "feat: add CodeFixer to sequential_code_review.sh, add breakout_code_review.sh"
```

---

## Task 13: Final verification

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/ -v -q 2>&1 | tail -20
```

Expected: all pass, 0 failures

- [ ] **Step 2: Verify no dead references to old agent**

```bash
grep -r "web_page\|web_showcase\|WebPageAgent\|WebShowcaseAgent\|web-page-generation\|web-showcase" \
  src/ tests/ docs/ examples/ CLAUDE.md --include="*.py" --include="*.md" --include="*.sh"
```

Expected: no output

- [ ] **Step 3: Lint check**

```bash
ruff check src/ofp_playground/agents/llm/codex.py src/ofp_playground/cli.py
```

Expected: no errors

- [ ] **Step 4: Type check**

```bash
mypy src/ofp_playground/agents/llm/codex.py --ignore-missing-imports
```

Expected: no errors (or only stubs-related warnings for `openai`)

- [ ] **Step 5: Verify CLI help reflects new task type**

```bash
ofp-playground start --help 2>&1 | grep -i "code"
```

Expected: `code-generation` appears in help output

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "chore: final verification pass — CodingAgent implementation complete"
```
