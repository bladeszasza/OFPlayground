"""Per-conversation output directory manager.

Each conversation session gets its own folder under ``result/``::

    result/
    └── 20260324_112523_a1b2c3d4/
        ├── images/
        ├── videos/
        ├── music/
        ├── web/
        ├── breakout/
        ├── manuscript.txt
        └── memory.json
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


RESULT_ROOT = Path("result")


class SessionOutputManager:
    """Creates and exposes per-conversation output directories."""

    def __init__(self, conversation_id: str):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        conv_slug = conversation_id.split(":")[-1][:8]
        # Sanitise to filesystem-safe chars
        conv_slug = re.sub(r"[^\w-]", "", conv_slug) or "session"
        self._root = RESULT_ROOT / f"{ts}_{conv_slug}"
        self._root.mkdir(parents=True, exist_ok=True)

    # ---- directory accessors (lazy-create) ----

    @property
    def root(self) -> Path:
        return self._root

    @property
    def images(self) -> Path:
        d = self._root / "images"
        d.mkdir(exist_ok=True)
        return d

    @property
    def videos(self) -> Path:
        d = self._root / "videos"
        d.mkdir(exist_ok=True)
        return d

    @property
    def music(self) -> Path:
        d = self._root / "music"
        d.mkdir(exist_ok=True)
        return d

    @property
    def web(self) -> Path:
        d = self._root / "web"
        d.mkdir(exist_ok=True)
        return d

    @property
    def breakout(self) -> Path:
        d = self._root / "breakout"
        d.mkdir(exist_ok=True)
        return d
