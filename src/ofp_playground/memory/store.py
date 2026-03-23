"""Ephemeral, categorized memory store for showrunner_driven conversations.

All state is in-process and does not persist beyond the conversation.
Agents can write memories during a session; the store is automatically
summarized and injected into system prompts each turn.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class MemoryCategory(str, Enum):
    DECISIONS    = "decisions"     # Key choices made during the workflow
    AGENT_PROFILES = "agent_profiles"  # Agent strengths, weaknesses, reliability
    TASKS        = "tasks"         # Task progress, dependencies, and status
    LESSONS      = "lessons"       # What went wrong and how to avoid repeating mistakes
    PREFERENCES  = "preferences"   # User/topic style preferences observed
    GOALS        = "goals"         # Original mission and high-level objectives


# Priority order for summary output — most important first
_SUMMARY_PRIORITY = [
    MemoryCategory.GOALS,
    MemoryCategory.TASKS,
    MemoryCategory.DECISIONS,
    MemoryCategory.LESSONS,
    MemoryCategory.AGENT_PROFILES,
    MemoryCategory.PREFERENCES,
]


@dataclass
class MemoryEntry:
    id: str
    category: MemoryCategory
    key: str
    content: str
    author: str
    timestamp: datetime = field(default_factory=datetime.now)


class MemoryStore:
    """Ephemeral key/category memory store for one conversation session.

    Thread-safety note: operations are synchronous and single-threaded
    (asyncio cooperative scheduling); no locking is required.
    """

    def __init__(self) -> None:
        self._entries: list[MemoryEntry] = []

    # ── Write ────────────────────────────────────────────────────────────

    def store(
        self,
        category: str | MemoryCategory,
        key: str,
        content: str,
        author: str = "system",
    ) -> str:
        """Create or update an entry.

        If an entry with the same (category, key) already exists it is
        updated in-place (content, author, timestamp).  Returns the entry id.
        """
        cat = MemoryCategory(category) if isinstance(category, str) else category
        key_lower = key.lower()
        for entry in self._entries:
            if entry.category == cat and entry.key.lower() == key_lower:
                entry.content = content
                entry.author = author
                entry.timestamp = datetime.now()
                return entry.id
        entry = MemoryEntry(
            id=uuid.uuid4().hex[:8],
            category=cat,
            key=key,
            content=content,
            author=author,
        )
        self._entries.append(entry)
        return entry.id

    def seed_goal(self, goal_text: str) -> None:
        """Seed the store with the session's original mission/topic."""
        self.store(MemoryCategory.GOALS, "original_goal", goal_text, author="system")

    # ── Read ─────────────────────────────────────────────────────────────

    def recall(
        self,
        category: Optional[str | MemoryCategory] = None,
        key: Optional[str] = None,
    ) -> list[MemoryEntry]:
        """Return entries filtered by category and/or key (case-insensitive substring)."""
        results = self._entries
        if category is not None:
            cat = MemoryCategory(category) if isinstance(category, str) else category
            results = [e for e in results if e.category == cat]
        if key is not None:
            key_lower = key.lower()
            results = [e for e in results if key_lower in e.key.lower()]
        return list(results)

    def list_categories(self) -> dict[str, int]:
        """Return {category_name: entry_count} for non-empty categories."""
        counts: dict[str, int] = {}
        for entry in self._entries:
            counts[entry.category.value] = counts.get(entry.category.value, 0) + 1
        return counts

    def is_empty(self) -> bool:
        return len(self._entries) == 0

    def all_entries(self) -> list[MemoryEntry]:
        return list(self._entries)

    # ── Summary ──────────────────────────────────────────────────────────

    def get_summary(self, max_chars: int = 2000) -> str:
        """Return a condensed, priority-ordered summary of memory contents.

        Priority: goals → tasks → decisions → lessons → agent_profiles → preferences.
        Truncates gracefully when budget is tight.
        """
        if not self._entries:
            return ""

        lines: list[str] = []
        for cat in _SUMMARY_PRIORITY:
            entries = [e for e in self._entries if e.category == cat]
            if not entries:
                continue
            lines.append(f"[{cat.value.upper()}]")
            for e in entries:
                lines.append(f"  {e.key}: {e.content}")

        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars].rsplit("\n", 1)[0] + "\n  ...(truncated)"
        return text

    # ── Serialization ────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialize all entries to a plain dict (for JSON file dump)."""
        return {
            "entries": [
                {
                    "id": e.id,
                    "category": e.category.value,
                    "key": e.key,
                    "content": e.content,
                    "author": e.author,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in self._entries
            ]
        }
