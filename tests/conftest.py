"""Shared pytest fixtures for OFP Playground tests."""
from __future__ import annotations

import asyncio
import pytest

from openfloor import (
    Conversation,
    DialogEvent,
    Envelope,
    Sender,
    TextFeature,
    Token,
    UtteranceEvent,
)

from ofp_playground.agents.base import BasePlaygroundAgent
from ofp_playground.bus.message_bus import MessageBus, FLOOR_MANAGER_URI
from ofp_playground.models.artifact import Utterance


# ---------------------------------------------------------------------------
# Minimal concrete agent (no-op run) for testing base logic
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_background_status(monkeypatch):
    """Unit sessions do not launch persistent services; ASP tests inject/connect explicitly."""
    monkeypatch.setenv("OFP_STATUS_DISABLED", "1")

class ConcreteAgent(BasePlaygroundAgent):
    """Minimal subclass that satisfies the abstract run() requirement."""
    async def run(self):
        self._running = True


# ---------------------------------------------------------------------------
# Envelope factory
# ---------------------------------------------------------------------------

def make_envelope(
    sender_uri: str,
    text: str = "",
    conv_id: str = "conv:test",
    media_key: str | None = None,
    media_path: str | None = None,
) -> Envelope:
    """Build a minimal OFP Envelope with an utterance event.

    If media_key + media_path are provided, a second feature is added to the
    dialogEvent so tests can exercise media-handling code paths.
    """
    features: dict = {
        "text": TextFeature(
            mimeType="text/plain",
            tokens=[Token(value=text)] if text else [],
        )
    }
    if media_key and media_path:
        features[media_key] = TextFeature(
            mimeType="application/octet-stream",
            tokens=[Token(value=media_path)],
        )
    return Envelope(
        sender=Sender(speakerUri=sender_uri, serviceUrl="local://test"),
        conversation=Conversation(id=conv_id),
        events=[
            UtteranceEvent(
                dialogEvent=DialogEvent(
                    id="test-de-1",
                    speakerUri=sender_uri,
                    features=features,
                )
            )
        ],
    )


# ---------------------------------------------------------------------------
# Simple utterance factory
# ---------------------------------------------------------------------------

def make_utterance(
    name: str = "TestAgent",
    text: str = "Hello",
    uri: str = "tag:test:agent",
) -> Utterance:
    return Utterance.from_text(uri, name, text)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def bus():
    """A fresh MessageBus with the floor manager queue pre-registered."""
    b = MessageBus()
    fm_q: asyncio.Queue = asyncio.Queue()
    # Pre-register the floor manager so send() doesn't error on broadcast
    asyncio.get_event_loop().run_until_complete(b.register(FLOOR_MANAGER_URI, fm_q))
    return b


@pytest.fixture
def agent(bus):
    """A ready ConcreteAgent wired up to the bus fixture."""
    a = ConcreteAgent(
        speaker_uri="tag:test:agent",
        name="TestAgent",
        service_url="local://test",
        bus=bus,
        conversation_id="conv:test",
    )
    return a


@pytest.fixture
def sample_utterance():
    return make_utterance()


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    """A Settings object whose config file lives in tmp_path."""
    from ofp_playground.config.settings import Settings, CONFIG_DIR, CONFIG_FILE
    monkeypatch.setattr("ofp_playground.config.settings.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("ofp_playground.config.settings.CONFIG_FILE", tmp_path / "config.toml")
    return Settings()


@pytest.fixture
def mock_env_keys(monkeypatch):
    """Set all API key env vars to fictional values."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
    monkeypatch.setenv("HF_API_KEY", "hf_test")
