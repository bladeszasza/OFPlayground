"""Web page generation agent — generates a self-contained HTML page from run artifacts."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from openfloor import Envelope

from ofp_playground.agents.llm.base import BaseLLMAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("ofp-web")
DEFAULT_MODEL_ANTHROPIC = "claude-sonnet-4-6"
DEFAULT_MODEL_OPENAI = "gpt-5.4-long-context"
DEFAULT_MODEL_GOOGLE = "gemini-3.1-pro-preview"
DEFAULT_MODEL_HF = "deepseek-ai/DeepSeek-V3.2"


class WebPageAgent(BaseLLMAgent):
    """HTML page generator. Works with any text-generation provider.

    Model-agnostic: Anthropic, OpenAI, Google, or HuggingFace.
    Passively collects media artifacts (images, audio, video) during the
    conversation, then generates a single HTML page when granted the floor —
    whether assigned mid-pipeline by an orchestrator or used as a
    post-production step. The system prompt (synopsis) defines what kind of
    page to produce; the runtime task directive defines the specific content.
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
        self._collected_images: list[tuple[str, Path]] = []
        self._collected_audio: list[tuple[str, Path]] = []
        self._collected_video: list[tuple[str, Path]] = []
        self._floor_log: list[str] = []
        self._page_directive: str = ""  # full directive text incl. FloorManager-injected manuscript
        self._output_dir: Path = OUTPUT_DIR
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def task_type(self) -> str:
        return "web-page-generation"

    # ------------------------------------------------------------------
    # Passive artifact collection
    # ------------------------------------------------------------------

    def _collect_media_from_envelope(self, envelope: Envelope) -> None:
        """Scan envelope events for media features (image/audio/video paths)."""
        sender_uri = self._get_sender_uri(envelope)
        sender_name = self._name_registry.get(sender_uri, sender_uri.split(":")[-1])

        for event in (envelope.events or []):
            de = getattr(event, "dialogEvent", None)
            if not de or not de.features:
                continue
            text_feat = de.features.get("text")
            text = ""
            if text_feat and text_feat.tokens:
                text = " ".join(t.value for t in text_feat.tokens if t.value)

            for media_key in ("image", "audio", "video"):
                feat = de.features.get(media_key)
                if feat and feat.tokens and feat.tokens[0].value:
                    path = Path(feat.tokens[0].value)
                    if path.exists():
                        desc = f"{sender_name}: {text[:80]}" if text else path.stem
                        if media_key == "image":
                            self._collected_images.append((desc, path))
                        elif media_key == "audio":
                            self._collected_audio.append((desc, path))
                        else:
                            self._collected_video.append((desc, path))

    async def _handle_utterance(self, envelope: Envelope) -> None:
        sender_uri = self._get_sender_uri(envelope)
        if sender_uri == self.speaker_uri:
            return

        # Always collect media regardless of utterance type
        self._collect_media_from_envelope(envelope)

        text = self._extract_text_from_envelope(envelope)
        sender_name = self._name_registry.get(sender_uri, sender_uri.split(":")[-1])

        ts = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{ts}] {sender_name}: {text[:120]}" if text else f"[{ts}] {sender_name}: (media)"
        self._floor_log.append(log_entry)

        if not text:
            return

        # Handle directive — save full text (includes FloorManager-injected manuscript)
        if "[DIRECTIVE for" in text:
            if self._parse_showrunner_message(text):
                self._page_directive = text
            # FloorManager sends grantFloor directly — do NOT call request_floor()
            return

        # Passive observation: accumulate context but never proactively request floor
        self._append_to_context(sender_name, text, is_self=False)

    # ------------------------------------------------------------------
    # HTML generation
    # ------------------------------------------------------------------

    def _build_context(self) -> str:
        """Assemble all collected artifacts into the LLM prompt context."""
        parts = ["=== PAGE GENERATION CONTEXT ===\n"]

        # Full directive contains the task + manuscript injected by FloorManager
        if self._page_directive:
            parts.append(f"## TASK DIRECTIVE & CONTEXT\n{self._page_directive}\n")

        # Images — reference by filename only; base64 is inlined by Python after generation
        for i, (desc, path) in enumerate(self._collected_images):
            parts.append(
                f"## IMAGE {i + 1}: {desc}\n"
                f"Filename: {path.name}\n"
                f"Reference as: <img src=\"{path.name}\" alt=\"{desc}\">\n"
            )

        # Audio — sibling file reference
        for i, (desc, path) in enumerate(self._collected_audio):
            parts.append(
                f"## AUDIO {i + 1}: {desc}\n"
                f"Filename: {path.name}\n"
                f"Embed as: <audio controls src=\"{path.name}\"></audio>\n"
            )

        # Video — sibling file reference
        for i, (desc, path) in enumerate(self._collected_video):
            parts.append(
                f"## VIDEO {i + 1}: {desc}\n"
                f"Filename: {path.name}\n"
                f"Embed as: <video controls src=\"{path.name}\"></video>\n"
            )

        # Floor log (last 15 entries — directive already has full manuscript context)
        if self._floor_log:
            log_text = "\n".join(self._floor_log[-15:])
            parts.append(f"## FLOOR LOG (last 15 events)\n```\n{log_text}\n```\n")

        parts.append(
            "\n=== INSTRUCTION ===\n"
            "Generate the complete HTML page now. "
            "Use the full base64 strings provided for <img> embeds. "
            "Audio and video files should be referenced by filename only (sibling of the HTML file). "
            "Return ONLY the HTML document (<!DOCTYPE html> through </html>). "
            "No markdown fences, no preamble, no explanation."
        )
        return "\n".join(parts)

    def _strip_markdown_fences(self, text: str) -> str:
        text = text.strip()
        text = re.sub(r"^```html?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        return text.strip()

    def _extract_file_directive(self, text: str) -> tuple[str, str]:
        """Extract === FILE: name.html === wrapper from LLM output.

        Returns (filename, html_content).  If no wrapper is found, returns
        ('', text) so the caller falls back to the timestamp slug.
        """
        m = re.search(
            r"===\s*FILE:\s*([\w.\-]+\.html)\s*===\s*\n(.*?)(?:===\s*END FILE\s*===|$)",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return "", text

    def _postprocess_inline_images(self, html: str) -> str:
        """Replace filename-based img src references with inline base64 data URIs.

        The LLM is given filenames (e.g. <img src="chapter_01.png">) to keep the
        prompt small.  After generation we inline the actual bytes here so the
        final HTML file is fully self-contained.
        """
        for _desc, path in self._collected_images:
            try:
                b64 = base64.b64encode(path.read_bytes()).decode()
                # Match both single and double-quoted src attributes
                html = re.sub(
                    rf'src=(["\'])({re.escape(path.name)})\1',
                    f'src="data:image/png;base64,{b64}"',
                    html,
                )
            except OSError:
                pass  # image unreadable — leave src as filename ref
        return html

    async def _generate_html(self, context: str) -> Optional[str]:
        """Dispatch HTML generation to the configured provider."""
        loop = asyncio.get_event_loop()
        system = self._synopsis

        if self._provider in ("anthropic", "claude"):
            def _call():
                import anthropic
                client = anthropic.Anthropic(api_key=self._api_key)
                resp = client.messages.create(
                    model=self._model or DEFAULT_MODEL_ANTHROPIC,
                    max_tokens=16000,
                    system=system,
                    messages=[{"role": "user", "content": context}],
                )
                return resp.content[0].text if resp.content else None
            return await loop.run_in_executor(None, _call)

        elif self._provider in ("openai", "gpt"):
            def _call():
                from openai import OpenAI
                client = OpenAI(api_key=self._api_key)
                resp = client.chat.completions.create(
                    model=self._model or DEFAULT_MODEL_OPENAI,
                    max_tokens=16000,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": context},
                    ],
                )
                return resp.choices[0].message.content
            return await loop.run_in_executor(None, _call)

        elif self._provider in ("google", "gemini"):
            def _call():
                from google import genai
                client = genai.Client(api_key=self._api_key)
                resp = client.models.generate_content(
                    model=self._model or DEFAULT_MODEL_GOOGLE,
                    contents=f"{system}\n\n{context}",
                )
                return resp.text
            return await loop.run_in_executor(None, _call)

        elif self._provider in ("hf", "huggingface"):
            def _call():
                from huggingface_hub import InferenceClient
                client = InferenceClient(token=self._api_key)
                resp = client.chat.completions.create(
                    model=self._model or DEFAULT_MODEL_HF,
                    max_tokens=16000,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": context},
                    ],
                )
                return resp.choices[0].message.content
            return await loop.run_in_executor(None, _call)

        logger.error("[%s] Unknown provider: %s", self._name, self._provider)
        return None

    # ------------------------------------------------------------------
    # Floor grant — generate and save the page
    # ------------------------------------------------------------------

    async def _handle_grant_floor(self) -> None:
        """Floor granted — build context, call LLM, save HTML to ofp-web/."""
        self._has_floor = True
        try:
            context = self._build_context()
            html = await self._call_with_retry(lambda: self._generate_html(context))

            if html:
                html = self._strip_markdown_fences(html)
                # Extract === FILE: name.html === directive if the LLM wrapped its output
                filename, html = self._extract_file_directive(html)
                html = self._postprocess_inline_images(html)  # inline base64 after generation
                if not filename:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    slug = re.sub(r"[^\w]+", "_", self._name.lower())
                    filename = f"{ts}_{slug}.html"
                path = self._output_dir / filename
                path.write_text(html, encoding="utf-8")

                # Copy audio/video files alongside the HTML so relative refs work
                import shutil
                for _, src in self._collected_audio + self._collected_video:
                    dest = self._output_dir / src.name
                    if not dest.exists():
                        shutil.copy2(src, dest)

                msg = f"Page generated: {path.resolve()}"
                await self.send_envelope(self._make_utterance_envelope(msg))
                logger.info("[%s] Page saved: %s", self._name, path)
            else:
                await self.send_envelope(
                    self._make_utterance_envelope("Page generation returned empty output.")
                )
        except Exception as e:
            logger.error("[%s] Page generation error: %s", self._name, e, exc_info=True)
            await self.send_envelope(
                self._make_utterance_envelope(f"Page generation failed: {str(e)[:200]}")
            )
        finally:
            self._has_floor = False
            self._current_director_instruction = ""
            self._collected_images.clear()  # reset per-chapter so images don't accumulate
            await self.yield_floor()

    # ------------------------------------------------------------------
    # Unused base methods (relevance_filter=False, _handle_grant_floor overridden)
    # ------------------------------------------------------------------

    async def _generate_response(self, participants):
        raise NotImplementedError("WebPageAgent overrides _handle_grant_floor directly")

    async def _quick_check(self, prompt: str) -> str:
        return "NO"
