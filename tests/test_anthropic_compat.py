"""Claude Haiku 5.5 compatibility: thinking-first responses, no prefill, effort gating."""
from __future__ import annotations

from types import SimpleNamespace

from ofp_playground.agents.llm.anthropic import DEFAULT_MODEL, AnthropicAgent
from ofp_playground.agents.llm.anthropic_compat import (
    CONTEXT_CUE,
    CONTINUE_CUE,
    effort_kwargs,
    prepare_messages,
    response_text,
    supports_effort,
)
from ofp_playground.agents.llm.anthropic_vision import DEFAULT_VISION_MODEL
from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.config.settings import DefaultsConfig


def _block(kind: str, **fields):
    return SimpleNamespace(type=kind, **fields)


def _thinking_first(text: str):
    return SimpleNamespace(content=[
        _block("thinking", thinking="", signature="sig"),
        _block("text", text=text),
    ])


class _FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _agent(model: str, response) -> tuple[AnthropicAgent, _FakeMessages]:
    agent = AnthropicAgent(name="Ada", synopsis="tester", bus=MessageBus(),
                           conversation_id="conv:test", api_key="k", model=model)
    messages = _FakeMessages(response)
    agent._client = SimpleNamespace(messages=messages)
    return agent, messages


def test_defaults_use_latest_haiku() -> None:
    assert DEFAULT_MODEL == "claude-haiku-5-5"
    assert DEFAULT_VISION_MODEL == "claude-haiku-5-5"
    defaults = DefaultsConfig()
    assert defaults.llm_model_anthropic == "claude-haiku-5-5"
    assert defaults.vision_model_anthropic == "claude-haiku-5-5"


def test_response_text_skips_thinking_and_tool_blocks() -> None:
    response = SimpleNamespace(content=[
        _block("thinking", thinking="", signature="s"),
        _block("text", text="Hello"),
        _block("tool_use", id="t1", name="x", input={}),
        _block("text", text="world"),
    ])
    assert response_text(response) == "Hello\nworld"
    assert response_text(SimpleNamespace(content=[_block("thinking", thinking="")])) == ""
    assert response_text(SimpleNamespace(content=None)) == ""


def test_prepare_messages_never_ends_on_assistant() -> None:
    assert prepare_messages([], opening="Hi") == [{"role": "user", "content": "Hi"}]
    history = [
        {"role": "assistant", "content": "my earlier turn"},
        {"role": "user", "content": "Bob: one"},
        {"role": "user", "content": "Cy: two"},
        {"role": "assistant", "content": "my last turn"},
    ]
    messages = prepare_messages(history, opening="Hi")
    assert messages[0] == {"role": "user", "content": CONTEXT_CUE}
    assert messages[2] == {"role": "user", "content": "Bob: one\n\nCy: two"}
    assert messages[-1] == {"role": "user", "content": CONTINUE_CUE}
    assert history[1]["content"] == "Bob: one"  # input is not mutated


def test_prepare_messages_keeps_structured_content() -> None:
    blocks = [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]
    messages = prepare_messages([{"role": "user", "content": "go"},
                                 {"role": "user", "content": blocks}], opening="Hi")
    assert messages == [{"role": "user", "content": "go"}, {"role": "user", "content": blocks}]


def test_effort_is_only_sent_to_models_that_accept_it() -> None:
    assert supports_effort("claude-haiku-5-5")
    assert supports_effort("claude-opus-5-5")
    assert not supports_effort("claude-haiku-4-5")
    assert not supports_effort("claude-haiku-4-5-20251001")
    assert not supports_effort(None)
    assert effort_kwargs("claude-haiku-5-5", "low") == {"output_config": {"effort": "low"}}
    assert effort_kwargs("claude-haiku-4-5-20251001", "low") == {}


async def test_generate_reads_text_after_thinking_and_ends_on_user_turn() -> None:
    agent, fake = _agent("claude-haiku-5-5", _thinking_first("Here is my answer."))
    agent._append_to_context("Bob", "What do you think?", is_self=False)
    agent._append_to_context("Ada", "Earlier reply.", is_self=True)
    assert await agent._generate_response(["Bob"]) == "Here is my answer."
    sent = fake.calls[0]
    assert sent["model"] == "claude-haiku-5-5"
    assert sent["messages"][-1]["role"] == "user"
    assert "temperature" not in sent and "thinking" not in sent


async def test_quick_check_leaves_room_for_thinking() -> None:
    agent, fake = _agent("claude-haiku-5-5", _thinking_first("YES"))
    assert await agent._check_relevance("hello Ada") is True
    sent = fake.calls[0]
    assert sent["max_tokens"] >= 1024
    assert sent["output_config"] == {"effort": "low"}


async def test_quick_check_on_older_haiku_sends_no_effort() -> None:
    response = SimpleNamespace(content=[_block("text", text="NO")])
    agent, fake = _agent("claude-haiku-4-5-20251001", response)
    assert await agent._check_relevance("unrelated") is False
    assert "output_config" not in fake.calls[0]


async def test_quick_check_with_only_thinking_counts_as_no() -> None:
    response = SimpleNamespace(content=[_block("thinking", thinking="", signature="s")])
    agent, _ = _agent("claude-haiku-5-5", response)
    assert await agent._check_relevance("hello") is False
