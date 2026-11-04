"""Helpers for mirroring orchestrator context into the shared coding sandbox."""
from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

from ofp_playground.memory.artifact_store import ArtifactStore
from ofp_playground.memory.store import MemoryStore

PHASES_DIRNAME = "phases"
BROWSER_MEMORY_RELATIVE_PATH = "memory/browser-sandbox-delivery.md"
SESSION_MEMORY_RELATIVE_PATH = "memory/session-memory.md"

_BROWSER_SANDBOX_DELIVERY_MD = textwrap.dedent(
    """\
    # Browser Sandbox Delivery

    - Browser sandboxes intended to open via file:// must not use local ES module graphs, importmaps, or bare imports like `from 'three'`.
    - Preferred delivery shape for generated Three.js demos: `index.html` + root `main.js` + optional `style.css`, with Three.js loaded as a classic CDN script and accessed via `window.THREE`.
    - Common visibility bug: appending canvas after a full-height HUD/root container pushes the 3D view below the fold; append canvas inside the fixed app root or position it fixed at z-index 0.
    - The Digimon World example script and coding-session prompt were updated to enforce file://-safe runtime output.
    """
)


@dataclass(frozen=True)
class SandboxContextFiles:
    """Relative sandbox paths for mirrored context files."""

    phase_files: tuple[str, ...]
    memory_files: tuple[str, ...]


def _render_session_memory(memory_store: MemoryStore) -> str:
    summary = memory_store.get_summary(max_chars=4000).strip()
    if not summary:
        summary = "No session memory recorded yet."
    return f"# Session Memory\n\n{summary}\n"


def sync_sandbox_context(
    sandbox_dir: Path,
    artifact_store: ArtifactStore | None,
    memory_store: MemoryStore | None,
) -> SandboxContextFiles:
    """Mirror accepted phase docs and selected memory notes into *sandbox_dir*.

    The coding workspace is sandboxed, so agents must receive any relevant
    context as files inside that workspace.
    """
    sandbox_dir.mkdir(parents=True, exist_ok=True)

    phase_files: list[str] = []
    if artifact_store is not None:
        phases_dir = sandbox_dir / PHASES_DIRNAME
        phases_dir.mkdir(parents=True, exist_ok=True)
        for art in artifact_store.all_artifacts():
            target = phases_dir / art.file_path.name
            try:
                content = art.file_path.read_text(encoding="utf-8")
            except OSError:
                content = art.content
            target.write_text(content, encoding="utf-8")
            phase_files.append(target.relative_to(sandbox_dir).as_posix())

    memory_dir = sandbox_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    memory_files: list[str] = []
    browser_memory = sandbox_dir / BROWSER_MEMORY_RELATIVE_PATH
    browser_memory.write_text(_BROWSER_SANDBOX_DELIVERY_MD, encoding="utf-8")
    memory_files.append(browser_memory.relative_to(sandbox_dir).as_posix())

    if memory_store is not None and not memory_store.is_empty():
        session_memory = sandbox_dir / SESSION_MEMORY_RELATIVE_PATH
        session_memory.write_text(_render_session_memory(memory_store), encoding="utf-8")
        memory_files.append(session_memory.relative_to(sandbox_dir).as_posix())

    return SandboxContextFiles(
        phase_files=tuple(phase_files),
        memory_files=tuple(memory_files),
    )


def build_sandbox_context_note(context: SandboxContextFiles) -> str:
    """Render a short prompt block that points agents at mirrored sandbox files."""
    if not context.phase_files and not context.memory_files:
        return ""

    lines = [
        "--- SANDBOX CONTEXT FILES ---",
        "If you can access the shared sandbox, read these files before making changes:",
    ]
    if context.phase_files:
        lines.append("Phase artifacts:")
        lines.extend(f"- {path}" for path in context.phase_files)
    if context.memory_files:
        lines.append("Memory notes:")
        lines.extend(f"- {path}" for path in context.memory_files)
    lines.append("Use the sandbox-relative paths exactly as written above.")
    lines.append("--- END SANDBOX CONTEXT FILES ---")
    return "\n".join(lines)