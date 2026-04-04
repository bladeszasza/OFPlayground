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
CODE_INTERPRETER_CONTAINER = {"type": "auto"}


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

    def _build_context(self) -> str:
        parts = ["=== CODING TASK CONTEXT ===\n"]
        if self._task_directive:
            parts.append(f"## TASK DIRECTIVE\n{self._task_directive}\n")
        parts.append(
            "\n=== OUTPUT CONTRACT ===\n"
            "Implement the task completely using code_interpreter. "
            "Save all output files to disk inside the code_interpreter session. "
            "Return your full output text INLINE — do NOT include markdown download links like "
            "[Download ...](sandbox:/mnt/data/...) because those paths are inaccessible to other agents. "
            "Return ONLY the implementation — no preamble, no explanation unless requested.\n"
        )
        return "\n".join(parts)

    def _tools_disabled_for_directive(self) -> bool:
        """Return True when directive explicitly requests text-only execution."""
        directive = (self._task_directive or "").lower()
        disable_markers = (
            "[retry_no_tools]",
            "retry without tools",
            "without tools",
            "no code execution",
            "architecture bullets only",
            "deliver architecture bullets only",
        )
        return any(marker in directive for marker in disable_markers)

    @staticmethod
    def _is_tool_configuration_error(err: Exception) -> bool:
        low = str(err).lower()
        return "tools[0].container" in low or "code_interpreter" in low and "missing required parameter" in low

    async def _run_openai_coding_loop(self, context: str) -> tuple[str, list[Path]]:
        """Run OpenAI Responses API streaming with code_interpreter.

        Returns (output_text, saved_files).
        NOTE: event attribute paths (item.outputs, output.files, etc.) reflect the
        openai SDK as of 2026-03. Verify against sdk changelog if the API changes.
        """
        from openai import AsyncOpenAI
        tools_disabled = self._tools_disabled_for_directive()
        if tools_disabled:
            await self._send_progress("Directive requested text-only mode (tools disabled).")

        async def _stream_once() -> tuple[str, list[tuple[str, str]]]:
            client = AsyncOpenAI(api_key=self._api_key)
            local_output_text = ""
            local_file_ids: list[tuple[str, str]] = []
            request_kwargs = {
                "model": self._model,
                "instructions": self._synopsis,
                "input": context,
                "reasoning": {"effort": "high"},
                "max_output_tokens": 32000,
            }
            if not tools_disabled:
                request_kwargs["tools"] = [{
                    "type": "code_interpreter",
                    "container": CODE_INTERPRETER_CONTAINER,
                }]

            try:
                async with client.responses.stream(**request_kwargs) as stream:  # type: ignore[call-overload]
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
                                            local_file_ids.append((f.file_id, f.filename))
                                    elif getattr(out, "type", "") == "logs":
                                        logs = (getattr(out, "logs", "") or "").strip()
                                        if logs:
                                            await self._send_progress(f"Output: {logs[:120]}")

                    final = await stream.get_final_response()
                    for item in (final.output or []):
                        if getattr(item, "type", "") == "message":
                            for part in (getattr(item, "content", None) or []):
                                if getattr(part, "type", "") == "output_text":
                                    local_output_text += getattr(part, "text", "")
            finally:
                await client.close()

            return local_output_text, local_file_ids

        try:
            output_text, file_ids = await self._call_with_retry(_stream_once)
        except Exception as e:
            if tools_disabled or not self._is_tool_configuration_error(e):
                raise
            await self._send_progress("Tool mode unavailable, retrying without tools.")
            tools_disabled = True
            output_text, file_ids = await self._call_with_retry(_stream_once)

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
            await dl_client.close()

        return output_text, saved_files

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

            import re as _re
            cleaned_output = _re.sub(
                r"\[(?:Download [^\]]*|[^\]]+)\]\(sandbox:/mnt/data/[^)]*\)",
                "",
                (output_text or ""),
            ).strip()
            lines: list[str] = []
            if cleaned_output:
                lines.append(cleaned_output)
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
            self._pending_floor_request = False
            self._task_directive = ""
