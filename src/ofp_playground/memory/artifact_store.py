"""Phase Artifact Store — externalized interlinked markdown memory for orchestrated pipelines.

Each accepted phase output is saved as a markdown file with YAML-style frontmatter
under ``result/<session>/phases/``.  The orchestrator and worker agents receive
compact index references instead of full text, and can retrieve specific artifacts
on demand via the ``read_artifact`` / ``list_artifacts`` tools.

This eliminates the unbounded context growth that previously caused token overflow
when the full manuscript was injected into every directive.
"""
from __future__ import annotations

import logging
import re
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class PhaseArtifact:
    """A single phase output persisted as a markdown file."""

    phase_num: int
    slug: str
    agent_name: str
    timestamp: datetime
    summary: str
    content: str
    tokens_approx: int
    file_path: Path
    depends_on: list[str] = field(default_factory=list)


def _slugify(text: str) -> str:
    """Convert text like 'GeomBuilder' or 'Asset Director' into 'geom-builder'."""
    # Insert hyphens before uppercase runs (camelCase → camel-case)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", text)
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "phase"


def _generate_summary(content: str, max_chars: int = 120) -> str:
    """Heuristic one-line summary: first meaningful sentence or line, truncated."""
    # Strip markdown headers and leading whitespace
    lines = content.strip().splitlines()
    for line in lines:
        stripped = line.strip()
        # Skip empty, headers, and horizontal rules
        if not stripped or stripped.startswith(("#", "---")):
            continue
        # Skip table header separators
        if re.match(r"^\|[-\s|:]+\|$", stripped):
            continue
        # Take first content line, strip markdown formatting
        clean = re.sub(r"[*_`]", "", stripped)
        # Remove leading list markers
        clean = re.sub(r"^[-•]\s+", "", clean)
        if len(clean) > max_chars:
            return clean[: max_chars - 3] + "..."
        return clean
    # Fallback: first N chars of raw content
    flat = " ".join(content.split())[:max_chars]
    return flat + "..." if len(flat) >= max_chars else flat


class ArtifactStore:
    """Manages phase artifacts as interlinked markdown files on disk.

    Usage::

        store = ArtifactStore(session_dir)
        art = store.save(
            agent_name="GeomBuilder",
            content="function buildHero() { ... }",
            summary="9 buildXxx() Three.js functions",
        )
        index = store.get_index()       # compact table for system prompt injection
        full  = store.read("geometry")  # retrieve by slug substring
    """

    def __init__(self, session_dir: Path) -> None:
        self._session_dir = session_dir
        self._phases_dir = session_dir / "phases"
        self._phases_dir.mkdir(parents=True, exist_ok=True)
        self._artifacts: list[PhaseArtifact] = []
        self._phase_counter = 0

    @property
    def phases_dir(self) -> Path:
        return self._phases_dir

    # ── Write ────────────────────────────────────────────────────────────

    def save(
        self,
        agent_name: str,
        content: str,
        summary: str = "",
        slug: str = "",
        depends_on: list[str] | None = None,
    ) -> PhaseArtifact:
        """Save a phase artifact to disk and return the artifact metadata.

        Args:
            agent_name: Name of the agent that produced this output.
            content: Full accepted output text (verbatim, no truncation).
            summary: One-line summary.  Auto-generated if empty.
            slug: Filename slug.  Auto-generated from agent_name if empty.
            depends_on: List of prior artifact slugs this phase depends on.

        Returns:
            The saved PhaseArtifact.
        """
        self._phase_counter += 1
        num = self._phase_counter

        if not slug:
            slug = _slugify(agent_name)
        if not summary:
            summary = _generate_summary(content)
        if depends_on is None:
            depends_on = []

        tokens_approx = len(content) // 4
        now = datetime.now()
        filename = f"{num:02d}_{slug}.md"
        file_path = self._phases_dir / filename

        # Write markdown with YAML-style frontmatter
        depends_str = ", ".join(depends_on) if depends_on else "none"
        header = textwrap.dedent(f"""\
            ---
            phase: {num}
            slug: {slug}
            agent: {agent_name}
            timestamp: {now.isoformat(timespec='seconds')}
            depends_on: [{depends_str}]
            tokens_approx: {tokens_approx}
            summary: "{summary}"
            ---

            # Phase {num}: {slug.replace('-', ' ').title()} ({agent_name})

        """)
        file_path.write_text(header + content, encoding="utf-8")

        artifact = PhaseArtifact(
            phase_num=num,
            slug=slug,
            agent_name=agent_name,
            timestamp=now,
            summary=summary,
            content=content,
            tokens_approx=tokens_approx,
            file_path=file_path,
            depends_on=depends_on,
        )
        self._artifacts.append(artifact)
        logger.info(
            "Phase artifact saved: %s (%s, ~%d tokens)",
            filename, agent_name, tokens_approx,
        )
        return artifact

    # ── Read ─────────────────────────────────────────────────────────────

    def get_index(self, max_summary_chars: int = 120) -> str:
        """Return a compact artifact index for injection into system prompts.

        Only the latest artifact per slug is shown — earlier saves with the same
        slug (e.g. stale character-memory-* from a prior beat) are hidden so agents
        always see current state.

        Format::

            --- PHASE ARTIFACTS (3 unique) ---
            01_asset-manifest.md  | AssetDirector | 9 assets defined for hero, obstacles, ...
            02_char-design.md     | CharDesigner  | Parts tables for all 9 assets
            03_geometry-code.md   | GeomBuilder   | 9 buildXxx() Three.js functions (~5200 tok)
            --- END PHASE ARTIFACTS ---
        """
        if not self._artifacts:
            return ""

        # Deduplicate: keep only the latest artifact per slug (last write wins).
        latest_by_slug: dict[str, PhaseArtifact] = {}
        for art in self._artifacts:
            latest_by_slug[art.slug] = art

        lines = [f"--- PHASE ARTIFACTS ({len(latest_by_slug)} unique) ---"]
        for art in self._artifacts:
            if latest_by_slug[art.slug] is not art:
                continue  # skip superseded versions
            filename = f"{art.phase_num:02d}_{art.slug}.md"
            summary = art.summary[:max_summary_chars]
            lines.append(
                f"  {filename:<28s} | {art.agent_name:<16s} | {summary} (~{art.tokens_approx} tok)"
            )
        lines.append("--- END PHASE ARTIFACTS ---")
        lines.append("Use read_artifact(slug) to retrieve the full content of any phase.")
        return "\n".join(lines)

    def read(self, query: str) -> str | None:
        """Retrieve full content of an artifact by slug, phase number, or substring match.

        Args:
            query: Slug name, phase number (as string), or substring to match.

        Returns:
            Full artifact content (without frontmatter), or None if not found.
        """
        art = self._resolve(query)
        return art.content if art else None

    def resolve(self, query: str) -> PhaseArtifact | None:
        """Return the artifact metadata for *query* if it can be resolved."""
        return self._resolve(query)

    def read_with_header(self, query: str) -> str | None:
        """Retrieve full artifact including the markdown frontmatter."""
        art = self._resolve(query)
        if art is None:
            return None
        try:
            return art.file_path.read_text(encoding="utf-8")
        except OSError:
            return art.content

    def latest(self) -> PhaseArtifact | None:
        """Return the most recently saved artifact, or None."""
        return self._artifacts[-1] if self._artifacts else None

    def all_artifacts(self) -> list[PhaseArtifact]:
        """Return all artifacts in phase order."""
        return list(self._artifacts)

    @property
    def phase_count(self) -> int:
        return self._phase_counter

    # ── Internal ─────────────────────────────────────────────────────────

    def _resolve(self, query: str) -> PhaseArtifact | None:
        """Find an artifact by exact slug, phase number, or substring.

        For slug and substring matches the *latest* saved artifact wins so that
        updated character memories (e.g. after Beat 2) are returned instead of
        the stale Beat 0 version saved earlier under the same slug.
        """
        q = query.strip().lower()

        # Try exact phase number — phase numbers are unique, return on first hit.
        try:
            num = int(q)
            for art in self._artifacts:
                if art.phase_num == num:
                    return art
        except ValueError:
            pass

        # Remove .md extension and leading digits for slug matching
        q_clean = re.sub(r"^\d+_", "", q.replace(".md", ""))

        # Exact slug match — keep iterating to find the LAST (most recent) match.
        result: PhaseArtifact | None = None
        for art in self._artifacts:
            if art.slug == q_clean:
                result = art
        if result:
            return result

        # Substring match on slug or agent name — last match wins.
        for art in self._artifacts:
            if q_clean in art.slug or q_clean in art.agent_name.lower():
                result = art
        return result
