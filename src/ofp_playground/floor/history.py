"""Conversation history tracking."""
from __future__ import annotations

from ofp_playground.models.artifact import Utterance

# Backward-compat alias — external code that imports HistoryEntry still works
HistoryEntry = Utterance


class ConversationHistory:
    def __init__(self, max_entries: int = 100):
        self._entries: list[Utterance] = []
        self._max = max_entries

    def add(self, entry: Utterance) -> None:
        self._entries.append(entry)
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max:]

    def recent(self, n: int = 20) -> list[Utterance]:
        return self._entries[-n:]

    def all(self) -> list[Utterance]:
        return list(self._entries)

    def to_context_string(self, n: int = 20) -> str:
        """Return recent history as a plain-text string for LLM context.

        Media utterances get a natural-language description so LLMs
        understand what was generated without receiving a raw file path.
        """
        lines = []
        for u in self.recent(n):
            media = u.primary_media
            if media is None:
                lines.append(f"[{u.speaker_name}]: {u.text}")
            elif media.feature_key == "image":
                lines.append(
                    f"[{u.speaker_name}]: {u.text} "
                    f"[generated image: {media.value_url}]"
                )
            elif media.feature_key == "video":
                lines.append(
                    f"[{u.speaker_name}]: {u.text} "
                    f"[generated video: {media.value_url}]"
                )
            else:
                lines.append(
                    f"[{u.speaker_name}]: {u.text} "
                    f"[{media.feature_key}: {media.value_url or media.value}]"
                )
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._entries)
