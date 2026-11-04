"""Floor policies for managing who speaks when.

Each policy maps to a different OFP floor-control pattern.  The
``description`` class-attribute gives a human-readable explanation that
is used in help text, agent tool schemas, and the CLI ``agents`` command.
"""
from __future__ import annotations

from enum import Enum


class FloorPolicy(str, Enum):
    """OFP floor-control policies.

    Every variant carries a *description* and *use_case* that are surfaced
    in tool definitions (so an LLM can choose the right policy for a
    breakout session) and in the CLI ``agents`` list.
    """

    SEQUENTIAL = "sequential"
    ROUND_ROBIN = "round_robin"
    MODERATED = "moderated"
    FREE_FOR_ALL = "free_for_all"
    SHOWRUNNER_DRIVEN = "showrunner_driven"

    @property
    def description(self) -> str:
        return _POLICY_META[self]["description"]

    @property
    def use_case(self) -> str:
        return _POLICY_META[self]["use_case"]


_POLICY_META: dict[FloorPolicy, dict[str, str]] = {
    FloorPolicy.SEQUENTIAL: {
        "description": (
            "First-come, first-served turns in request order. The floor goes "
            "to the first agent that requests it; later requests wait in a "
            "FIFO queue and are served one at a time as each speaker yields."
        ),
        "use_case": (
            "Structured discussions where every voice is heard one at a time "
            "— e.g. panel Q&A, step-by-step reviews."
        ),
    },
    FloorPolicy.ROUND_ROBIN: {
        "description": (
            "Strict rotation through the participants in the order they "
            "joined. When the speaker yields, the next agent in the ring gets "
            "the floor automatically; requests never jump the ring. A director "
            "(if present) opens every round and a show runner (if present) "
            "closes it."
        ),
        "use_case": (
            "Multi-chapter storytelling, debate formats, or any workflow "
            "where equal airtime matters — each agent contributes exactly "
            "once per cycle."
        ),
    },
    FloorPolicy.MODERATED: {
        "description": (
            "A moderator decides who speaks. With a human in the session the "
            "human is the moderator: agent requests are queued and only "
            "served when the moderator calls on an agent (by addressing it by "
            "name, or with /grant). Without a human, the floor manager "
            "moderates and serves queued requests in FIFO order."
        ),
        "use_case": (
            "Expert panels, code reviews, or deliberation where a "
            "chairperson controls who has the mic."
        ),
    },
    FloorPolicy.FREE_FOR_ALL: {
        "description": (
            "Any agent can speak at any time without waiting. Floor requests "
            "are always granted immediately and there is no turn order. LLM "
            "agents run a quick relevance check first so they only jump in "
            "when they have something to add."
        ),
        "use_case": (
            "Brainstorming, rapid-fire ideation, or casual conversations "
            "where spontaneity is more valuable than structure."
        ),
    },
    FloorPolicy.SHOWRUNNER_DRIVEN: {
        "description": (
            "One orchestrator agent controls the entire flow. It speaks "
            "first, assigns tasks to workers via [ASSIGN] (or several workers "
            "at once via [ASSIGN_PARALLEL]), evaluates output with "
            "[ACCEPT]/[REJECT], and can [SPAWN] new agents or [KICK] existing "
            "ones. Workers only speak when assigned; the floor always returns "
            "to the orchestrator, even when a worker fails to deliver."
        ),
        "use_case": (
            "Project management, creative production, or any pipeline "
            "where a single director coordinates specialist workers — "
            "e.g. writing a novella, producing a campaign, building a "
            "research report."
        ),
    },
}


class FloorController:
    """Pure floor state machine: who holds the floor, who is waiting, who is next.

    Invariants every floor manager relies on:

    * ``yield_floor`` from an agent that does **not** hold the floor is a no-op,
      so stale or duplicate yields can never hand out a second floor.
    * ROUND_ROBIN is strict: the next holder is always the ring successor of the
      agent that just yielded.  Requests from ring members are not queued (the
      ring reaches them anyway) — they only bootstrap an idle floor.
      ``_round_robin_index`` always points at the most recent holder.
    * MODERATED with ``manual=True`` (a human moderator is present) never
      auto-grants requests; they wait until the moderator calls on someone via
      :meth:`call_on`.
    * The *directed* queue (agents a moderator called on) is served before the
      request queue.
    """

    def __init__(self, policy: FloorPolicy = FloorPolicy.SEQUENTIAL):
        self.policy = policy
        self._current_holder: str | None = None
        self._request_queue: list[tuple[str, str]] = []  # (speaker_uri, reason)
        self._directed_queue: list[str] = []  # agents a moderator called on, in order
        self._round_robin_order: list[str] = []
        self._round_robin_index: int = 0
        self.manual: bool = False  # MODERATED: True while a human moderator is present

    # ── Introspection ────────────────────────────────────────────────────

    @property
    def current_holder(self) -> str | None:
        return self._current_holder

    @property
    def queue(self) -> list[tuple[str, str]]:
        return list(self._request_queue)

    @property
    def directed_queue(self) -> list[str]:
        return list(self._directed_queue)

    @property
    def rotation(self) -> list[str]:
        return list(self._round_robin_order)

    def is_last_in_rotation(self, speaker_uri: str) -> bool:
        return bool(self._round_robin_order) and self._round_robin_order[-1] == speaker_uri

    # ── Requests / yields ────────────────────────────────────────────────

    def request_floor(self, speaker_uri: str, reason: str = "") -> bool:
        """Returns True if the floor is (already or immediately) granted."""
        if self._current_holder == speaker_uri:
            return True
        if self.policy == FloorPolicy.FREE_FOR_ALL:
            self.grant_to(speaker_uri)
            return True

        if self.policy == FloorPolicy.MODERATED and self.manual:
            self._enqueue_request(speaker_uri, reason)
            return False

        if self._current_holder is None:
            self.grant_to(speaker_uri)
            return True

        if self.policy == FloorPolicy.ROUND_ROBIN and speaker_uri in self._round_robin_order:
            return False  # the ring will reach this agent — requests never jump it

        self._enqueue_request(speaker_uri, reason)
        return False

    def yield_floor(self, speaker_uri: str) -> str | None:
        """Agent yields the floor. Returns the next holder, if any.

        Yields from agents that do not currently hold the floor are ignored.
        """
        if self._current_holder != speaker_uri:
            return None
        self._current_holder = None
        return self._advance(after=speaker_uri)

    def call_on(self, speaker_uri: str) -> bool:
        """A moderator calls on an agent. Returns True if granted immediately."""
        if self._current_holder is None:
            self.grant_to(speaker_uri)
            return True
        if self._current_holder != speaker_uri and speaker_uri not in self._directed_queue:
            self._directed_queue.append(speaker_uri)
            self._request_queue = [(u, r) for u, r in self._request_queue if u != speaker_uri]
        return False

    def successor(self, speaker_uri: str | None) -> str | None:
        """Ring member after *speaker_uri*; after the last holder if it left the ring."""
        order = self._round_robin_order
        if not order:
            return None
        if speaker_uri in order:
            return order[(order.index(speaker_uri) + 1) % len(order)]
        return order[(self._round_robin_index + 1) % len(order)]

    def _advance(self, after: str | None) -> str | None:
        """Pick and grant the next holder after *after* released the floor."""
        nxt: str | None = None
        if self._directed_queue:
            nxt = self._directed_queue.pop(0)
        elif self.policy == FloorPolicy.MODERATED and self.manual:
            return None  # wait for the moderator
        elif self._request_queue:
            nxt, _ = self._request_queue.pop(0)
        elif self.policy == FloorPolicy.ROUND_ROBIN:
            nxt = self.successor(after)
        if nxt is not None:
            self.grant_to(nxt)
        return nxt

    def revoke_floor(self) -> str | None:
        """Revoke the floor from the current holder."""
        prev = self._current_holder
        self._current_holder = None
        return prev

    def grant_to(self, speaker_uri: str) -> None:
        """Grant the floor to a specific agent (moderator / orchestrator / ring)."""
        self._current_holder = speaker_uri
        if speaker_uri in self._round_robin_order:
            self._round_robin_index = self._round_robin_order.index(speaker_uri)
        self._drop_from_queues(speaker_uri)

    def clear_requests(self) -> None:
        """Discard all pending (undirected) floor requests."""
        self._request_queue.clear()

    # ── Membership ───────────────────────────────────────────────────────

    def add_to_rotation(self, speaker_uri: str) -> None:
        if speaker_uri not in self._round_robin_order:
            self._round_robin_order.append(speaker_uri)

    def suspend(self, speaker_uri: str) -> None:
        """Reject future turns while preserving an already active turn for draining."""
        self._drop_from_queues(speaker_uri)
        if speaker_uri in self._round_robin_order:
            index = self._round_robin_order.index(speaker_uri)
            self._round_robin_order.remove(speaker_uri)
            if self._current_holder == speaker_uri:
                self._round_robin_index = index - 1
            elif index <= self._round_robin_index:
                self._round_robin_index -= 1

    def remove_from_rotation(self, speaker_uri: str) -> str | None:
        """Remove all membership and transfer a departing holder's floor."""
        was_holder = self._current_holder == speaker_uri
        self._drop_from_queues(speaker_uri)
        if speaker_uri in self._round_robin_order:
            idx = self._round_robin_order.index(speaker_uri)
            if was_holder:
                # Make the departing holder's successor the next turn.
                self._round_robin_index = idx - 1
            self._round_robin_order.remove(speaker_uri)
            if not was_holder:
                if self._round_robin_index >= len(self._round_robin_order):
                    self._round_robin_index = 0
                elif idx < self._round_robin_index:
                    self._round_robin_index -= 1
        if not was_holder:
            return None
        self._current_holder = None
        return self._advance(after=speaker_uri)

    def _enqueue_request(self, speaker_uri: str, reason: str) -> None:
        if speaker_uri in self._directed_queue:
            return
        if not any(uri == speaker_uri for uri, _ in self._request_queue):
            self._request_queue.append((speaker_uri, reason))

    def _drop_from_queues(self, speaker_uri: str) -> None:
        self._request_queue = [(u, r) for u, r in self._request_queue if u != speaker_uri]
        self._directed_queue = [u for u in self._directed_queue if u != speaker_uri]
