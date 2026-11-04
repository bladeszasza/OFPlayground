"""Agent library — scans agents/ directory and exposes SOUL.md content by slug.

Slugs follow the pattern  category/agent-name  (e.g. "creative/brand-designer").
The library is loaded once per process; call load() to get the full index.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

# agents/ lives four levels up from this file:
# src/ofp_playground/agents/library.py  →  repo-root/agents/
_AGENTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "agents"


def _extract_display_name(content: str, fallback: str) -> str:
    """Pull a human-readable name from the first meaningful heading."""
    for line in content.splitlines():
        text = line.strip()
        if not text.startswith("#"):
            continue
        text = re.sub(r"^#+\s*", "", text)
        # Drop "SOUL.md — " prefix
        text = re.sub(r"^SOUL\.md\s*[—\-]\s*", "", text, flags=re.IGNORECASE)
        # Drop "Agent: " prefix
        text = re.sub(r"^Agent:\s*", "", text, flags=re.IGNORECASE)
        if text and text.lower() not in ("soul.md", ""):
            return text
    # Fallback to YAML-style name: field
    m = re.search(r'^name:\s*["\']?([^"\'\\n]+)["\']?', content, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return fallback.replace("-", " ").title()


@lru_cache(maxsize=1)
def load() -> dict[str, dict]:
    """Return the full agent library indexed by slug.

    Shape::

        {
          "creative/brand-designer": {
            "slug":         "creative/brand-designer",
            "category":     "creative",
            "name":         "brand-designer",
            "display_name": "Brand Designer",
            "content":      "<full SOUL.md text>",
          },
          ...
        }
    """
    result: dict[str, dict] = {}
    if not _AGENTS_DIR.exists():
        return result
    for soul_path in sorted(_AGENTS_DIR.glob("*/*/SOUL.md")):
        category = soul_path.parent.parent.name
        agent_slug_name = soul_path.parent.name
        slug = f"{category}/{agent_slug_name}"
        content = soul_path.read_text(encoding="utf-8")
        display_name = _extract_display_name(content, agent_slug_name)
        result[slug] = {
            "slug": slug,
            "category": category,
            "name": agent_slug_name,
            "display_name": display_name,
            "content": content,
        }
    return result


def resolve_slug(ref: str) -> str:
    """Resolve an @-prefixed slug to its SOUL.md content.

    ``ref`` may be ``"@creative/brand-designer"`` or just ``"creative/brand-designer"``.
    Raises ``ValueError`` if not found.
    """
    slug = ref.lstrip("@").strip()
    entry = load().get(slug)
    if entry is not None:
        return entry["content"]
    raise ValueError(
        f"Agent not found: @{slug}\n"
        f"  Expected: agents/{slug}/SOUL.md\n"
        f"  Available categories: {', '.join(sorted({p.parent.name for p in _AGENTS_DIR.glob('*/*/SOUL.md')}))}"
    )
