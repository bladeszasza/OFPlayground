"""Validate MODEL_CATALOG entries."""
from __future__ import annotations
import pytest

VALID_MODALITIES = {"text", "image", "video", "audio"}


def test_import():
    from ofp_playground.agents.llm.model_catalog import MODEL_CATALOG, ModelCaps
    assert MODEL_CATALOG
    assert ModelCaps


def test_expected_models_present():
    from ofp_playground.agents.llm.model_catalog import MODEL_CATALOG
    expected = {
        "gpt-5.4", "gpt-5.4-long-context", "gpt-5.4-nano",
        "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5", "claude-haiku-5-5",
        "gemini-3.1-pro-preview", "gemini-3-flash-preview", "gemini-3.1-flash-lite",
    }
    assert set(MODEL_CATALOG.keys()) == expected


def test_context_windows_positive():
    from ofp_playground.agents.llm.model_catalog import MODEL_CATALOG
    for model, caps in MODEL_CATALOG.items():
        assert caps.context_window > 0, f"{model}: context_window must be > 0"


def test_modalities_in_known():
    from ofp_playground.agents.llm.model_catalog import MODEL_CATALOG
    for model, caps in MODEL_CATALOG.items():
        for m in caps.modalities_in:
            assert m in VALID_MODALITIES, f"{model}: unknown modality_in '{m}'"


def test_modalities_out_non_empty():
    from ofp_playground.agents.llm.model_catalog import MODEL_CATALOG
    for model, caps in MODEL_CATALOG.items():
        assert len(caps.modalities_out) > 0, f"{model}: modalities_out must not be empty"


def test_frozen_dataclass():
    from ofp_playground.agents.llm.model_catalog import MODEL_CATALOG
    from dataclasses import FrozenInstanceError
    caps = MODEL_CATALOG["gpt-5.4"]
    with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
        caps.context_window = 999
