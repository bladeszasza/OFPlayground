"""Speaker-URI classification shared by floor managers, agents and UIs.

Every local participant gets a URI of the form
``tag:ofp-playground.local,2025:<prefix>-<name>``.  Different agent classes use
different prefixes (``image-``, ``gimage-``, ``gmusic-``, ``sora-`` …), and
the codebase used to sniff them with ad-hoc substring checks such as
``"image" in uri``.  Those checks silently missed Gemini/Sora/Lyria agents
(``gmusic-`` does not contain ``audio``) and misfiled perception agents
(``imageclassification-`` *does* contain ``image``).

Use :func:`uri_kind` instead.  For utterances, prefer the envelope's own
features (:func:`media_key_from_envelope`) — OFP carries the media type on the
wire, so it is the source of truth.
"""
from __future__ import annotations

from typing import Any

URI_PREFIX = "tag:ofp-playground.local,2025:"

FLOOR = "floor"
HUMAN = "human"
REMOTE = "remote"
TEXT = "text"
IMAGE = "image"
VIDEO = "video"
AUDIO = "audio"
VISION = "vision"
PERCEPTION = "perception"
OBSERVER = "observer"

MEDIA_KINDS = frozenset({IMAGE, VIDEO, AUDIO})
MEDIA_FEATURE_KEYS = ("image", "video", "audio", "3d")

# Longest prefixes first so e.g. "imageclassification-" wins over "image-".
_PREFIX_KINDS: tuple[tuple[str, str], ...] = (
    ("breakout-floor-manager", FLOOR),
    ("coding-session-manager", FLOOR),
    ("floor-manager", FLOOR),
    ("web-observer", OBSERVER),
    ("imageclassification-", PERCEPTION),
    ("imagesegmentation-", PERCEPTION),
    ("objectdetection-", PERCEPTION),
    ("tokenclassification-", PERCEPTION),
    ("textclassification-", PERCEPTION),
    ("imagetotext-", PERCEPTION),
    ("summarization-", PERCEPTION),
    ("multimodal-", VISION),
    ("avision-", VISION),
    ("gvision-", VISION),
    ("vision-", VISION),
    ("gimage-", IMAGE),
    ("image-", IMAGE),
    ("gvideo-", VIDEO),
    ("video-", VIDEO),
    ("sora-", VIDEO),
    ("gmusic-", AUDIO),
    ("audio-", AUDIO),
    ("human-", HUMAN),
    ("remote-", REMOTE),
    ("llm-", TEXT),
    ("director-", TEXT),
)


def _tail(uri: str) -> str:
    return uri[len(URI_PREFIX):] if uri.startswith(URI_PREFIX) else uri.split(":")[-1]


def uri_kind(uri: str | None) -> str:
    """Classify a speaker URI. Unknown local prefixes default to ``"text"``."""
    if not uri:
        return TEXT
    tail = _tail(uri)
    for prefix, kind in _PREFIX_KINDS:
        if tail.startswith(prefix):
            return kind
    return TEXT


def is_media_uri(uri: str | None) -> bool:
    return uri_kind(uri) in MEDIA_KINDS


def is_human_uri(uri: str | None) -> bool:
    return uri_kind(uri) == HUMAN


def is_remote_uri(uri: str | None) -> bool:
    return uri_kind(uri) == REMOTE


def is_floor_uri(uri: str | None) -> bool:
    return uri_kind(uri) == FLOOR


def media_key_from_envelope(envelope: Any) -> str | None:
    """Return the first media feature key (image/video/audio/3d) in an utterance."""
    for event in (getattr(envelope, "events", None) or []):
        de = getattr(event, "dialogEvent", None)
        features = getattr(de, "features", None) or {}
        if not isinstance(features, dict):
            continue
        for key in MEDIA_FEATURE_KEYS:
            feat = features.get(key)
            tokens = getattr(feat, "tokens", None) or []
            if tokens and getattr(tokens[0], "value", None):
                return key
    return None


def manifest_output_kinds(manifest: Any) -> set[str]:
    """Collect the output layers advertised by an OFP manifest."""
    outputs: set[str] = set()
    for cap in (getattr(manifest, "capabilities", None) or []):
        layers = getattr(cap, "supportedLayers", None)
        outputs.update(getattr(layers, "output", None) or [])
    return outputs
