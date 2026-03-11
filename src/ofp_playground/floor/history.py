"""Conversation history tracking."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HistoryEntry:
    speaker_uri: str
    speaker_name: str
    text: str
    timestamp: float = field(default_factory=time.time)
    event_type: str = "utterance"


class ConversationHistory:
    def __init__(self, max_entries: int = 100):
        self._entries: list[HistoryEntry] = []
        self._max = max_entries

    def add(self, entry: HistoryEntry) -> None:
        self._entries.append(entry)
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max:]

    def recent(self, n: int = 20) -> list[HistoryEntry]:
        return self._entries[-n:]

    def all(self) -> list[HistoryEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)
