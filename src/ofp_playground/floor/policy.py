"""Floor policies for managing who speaks when."""
from __future__ import annotations

from enum import Enum
from typing import Optional


class FloorPolicy(str, Enum):
    SEQUENTIAL = "sequential"
    ROUND_ROBIN = "round_robin"
    MODERATED = "moderated"
    FREE_FOR_ALL = "free_for_all"


class FloorController:
    """Manages floor state: who has it, who's waiting, policy enforcement."""

    def __init__(self, policy: FloorPolicy = FloorPolicy.SEQUENTIAL):
        self.policy = policy
        self._current_holder: Optional[str] = None
        self._request_queue: list[tuple[str, str]] = []  # (speaker_uri, reason)
        self._round_robin_order: list[str] = []
        self._round_robin_index: int = 0

    @property
    def current_holder(self) -> Optional[str]:
        return self._current_holder

    def request_floor(self, speaker_uri: str, reason: str = "") -> bool:
        """Returns True if floor is immediately granted, False if queued."""
        if self.policy == FloorPolicy.FREE_FOR_ALL:
            self._current_holder = speaker_uri
            return True

        if self._current_holder is None:
            self._current_holder = speaker_uri
            return True

        # Queue if not already in queue
        if not any(uri == speaker_uri for uri, _ in self._request_queue):
            self._request_queue.append((speaker_uri, reason))
        return False

    def yield_floor(self, speaker_uri: str) -> Optional[str]:
        """Agent yields floor. Returns the next holder if any."""
        if self._current_holder == speaker_uri:
            self._current_holder = None

        if self._request_queue:
            next_uri, _ = self._request_queue.pop(0)
            self._current_holder = next_uri
            return next_uri

        if self.policy == FloorPolicy.ROUND_ROBIN and self._round_robin_order:
            self._round_robin_index = (self._round_robin_index + 1) % len(self._round_robin_order)
            next_uri = self._round_robin_order[self._round_robin_index]
            self._current_holder = next_uri
            return next_uri

        return None

    def revoke_floor(self) -> Optional[str]:
        """Revoke the floor from the current holder."""
        prev = self._current_holder
        self._current_holder = None
        return prev

    def grant_to(self, speaker_uri: str) -> None:
        """Manually grant floor to a specific agent (moderated mode)."""
        self._current_holder = speaker_uri
        # Remove from queue if present
        self._request_queue = [(uri, r) for uri, r in self._request_queue if uri != speaker_uri]

    def add_to_rotation(self, speaker_uri: str) -> None:
        if speaker_uri not in self._round_robin_order:
            self._round_robin_order.append(speaker_uri)

    def remove_from_rotation(self, speaker_uri: str) -> None:
        if speaker_uri in self._round_robin_order:
            idx = self._round_robin_order.index(speaker_uri)
            self._round_robin_order.remove(speaker_uri)
            if self._round_robin_index >= len(self._round_robin_order):
                self._round_robin_index = 0
            elif idx < self._round_robin_index:
                self._round_robin_index -= 1

    @property
    def queue(self) -> list[tuple[str, str]]:
        return list(self._request_queue)
