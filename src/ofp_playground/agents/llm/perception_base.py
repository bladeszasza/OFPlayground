"""Base classes for HuggingFace perception/analysis agents.

Two bases are provided:
  - BaseImagePerceptionAgent  — reacts to images shared in the conversation
  - BaseTextPerceptionAgent   — analyses text utterances every N turns
"""
from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from typing import Any, Optional

from openfloor import Envelope

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus

logger = logging.getLogger(__name__)


class BaseImagePerceptionAgent(BasePlaygroundAgent):
    """Watches for images in the conversation and runs HF inference on them."""

    TASK: str = ""
    DEFAULT_MODEL: str = ""

    def __init__(
        self,
        name: str,
        synopsis: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str = "",
    ):
        task_slug = self.TASK.lower().replace("-", "")
        speaker_uri = (
            f"tag:ofp-playground.local,2025:{task_slug}-{name.lower().replace(' ', '-')}"
        )
        super().__init__(
            speaker_uri=speaker_uri,
            name=name,
            service_url=f"local://{task_slug}-{name.lower()}",
            bus=bus,
            conversation_id=conversation_id,
        )
        self._synopsis = synopsis
        self._model = model or self.DEFAULT_MODEL
        self._api_key = api_key
        self._has_floor = False
        self._pending_image_path: Optional[str] = None

    def _extract_image_from_envelope(self, envelope: Envelope) -> Optional[str]:
        for event in (envelope.events or []):
            de = getattr(event, "dialogEvent", None)
            if de and hasattr(de, "features") and de.features:
                image_feat = de.features.get("image")
                if image_feat and hasattr(image_feat, "tokens") and image_feat.tokens:
                    return image_feat.tokens[0].value
        return None

    @abstractmethod
    def _call_hf(self, image_path: str) -> Any:
        """Blocking HF inference call to be executed via run_in_executor."""
        ...

    @abstractmethod
    def _format_result(self, result: Any) -> str:
        """Format the inference result into a natural-language string."""
        ...

    async def _run_inference(self, image_path: str) -> str:
        loop = asyncio.get_event_loop()

        def _protected_call(path: str) -> Any:
            try:
                return self._call_hf(path)
            except StopIteration:
                return None

        try:
            result = await loop.run_in_executor(None, _protected_call, image_path)
            return self._format_result(result)
        except Exception as e:
            short = str(e).split("\n")[0][:200]
            logger.error("[%s] Inference error: %s", self._name, e)
            return f"({self.TASK} failed: {short})"

    async def _handle_utterance(self, envelope: Envelope) -> None:
        if self._get_sender_uri(envelope) == self.speaker_uri:
            return
        image_path = self._extract_image_from_envelope(envelope)
        if image_path and not self._has_floor:
            self._pending_image_path = image_path
            await self.request_floor(f"{self.TASK} analysis")

    async def _handle_grant_floor(self) -> None:
        self._has_floor = True
        try:
            if self._pending_image_path:
                response = await self._run_inference(self._pending_image_path)
                if response:
                    await self.send_envelope(self._make_utterance_envelope(response))
        except Exception as e:
            logger.error("[%s] Floor grant error: %s", self._name, e)
        finally:
            self._has_floor = False
            self._pending_image_path = None
            await self.yield_floor()

    async def _dispatch(self, envelope: Envelope) -> None:
        for event in (envelope.events or []):
            event_type = getattr(event, "eventType", type(event).__name__)
            if event_type == "utterance":
                await self._handle_utterance(envelope)
            elif event_type == "grantFloor":
                await self._handle_grant_floor()
            elif event_type == "revokeFloor":
                self._has_floor = False

    async def run(self) -> None:
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)
        try:
            while self._running:
                try:
                    envelope = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    await self._dispatch(envelope)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error("[%s] error: %s", self._name, e, exc_info=True)
        finally:
            self._running = False


class BaseTextPerceptionAgent(BasePlaygroundAgent):
    """Analyses text utterances from the conversation every N turns."""

    TASK: str = ""
    DEFAULT_MODEL: str = ""

    def __init__(
        self,
        name: str,
        synopsis: str,
        bus: MessageBus,
        conversation_id: str,
        api_key: str,
        model: str = "",
        analyze_every: int = 3,
    ):
        task_slug = self.TASK.lower().replace("-", "")
        speaker_uri = (
            f"tag:ofp-playground.local,2025:{task_slug}-{name.lower().replace(' ', '-')}"
        )
        super().__init__(
            speaker_uri=speaker_uri,
            name=name,
            service_url=f"local://{task_slug}-{name.lower()}",
            bus=bus,
            conversation_id=conversation_id,
        )
        self._synopsis = synopsis
        self._model = model or self.DEFAULT_MODEL
        self._api_key = api_key
        self._has_floor = False
        self._analyze_every = analyze_every
        self._turn_counter = 0
        self._recent_texts: list[str] = []

    @abstractmethod
    def _call_hf(self, text: str) -> Any:
        """Blocking HF inference call to be executed via run_in_executor."""
        ...

    @abstractmethod
    def _format_result(self, result: Any) -> str:
        """Format the inference result into a natural-language string."""
        ...

    def _get_context_text(self) -> str:
        return " ".join(self._recent_texts[-3:])[:512]

    async def _run_inference(self, text: str) -> str:
        loop = asyncio.get_event_loop()

        def _protected_call(t: str) -> Any:
            try:
                return self._call_hf(t)
            except StopIteration:
                return None

        try:
            result = await loop.run_in_executor(None, _protected_call, text)
            return self._format_result(result)
        except Exception as e:
            short = str(e).split("\n")[0][:200]
            logger.error("[%s] Inference error: %s", self._name, e)
            return ""

    async def _handle_utterance(self, envelope: Envelope) -> None:
        if self._get_sender_uri(envelope) == self.speaker_uri:
            return
        text = self._extract_text_from_envelope(envelope)
        if not text:
            return
        self._recent_texts.append(text)
        if len(self._recent_texts) > 20:
            self._recent_texts = self._recent_texts[-20:]
        self._turn_counter += 1
        if self._turn_counter % self._analyze_every == 0 and not self._has_floor:
            await self.request_floor(f"{self.TASK} analysis")

    async def _handle_grant_floor(self) -> None:
        self._has_floor = True
        try:
            context = self._get_context_text()
            if context and len(context) >= 10:
                response = await self._run_inference(context)
                if response:
                    await self.send_envelope(self._make_utterance_envelope(response))
        except Exception as e:
            logger.error("[%s] Floor grant error: %s", self._name, e)
        finally:
            self._has_floor = False
            await self.yield_floor()

    async def _dispatch(self, envelope: Envelope) -> None:
        for event in (envelope.events or []):
            event_type = getattr(event, "eventType", type(event).__name__)
            if event_type == "utterance":
                await self._handle_utterance(envelope)
            elif event_type == "grantFloor":
                await self._handle_grant_floor()
            elif event_type == "revokeFloor":
                self._has_floor = False

    async def run(self) -> None:
        self._running = True
        await self._bus.register(self.speaker_uri, self._queue)
        try:
            while self._running:
                try:
                    envelope = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    await self._dispatch(envelope)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error("[%s] error: %s", self._name, e, exc_info=True)
        finally:
            self._running = False
