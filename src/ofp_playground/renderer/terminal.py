"""Rich terminal renderer for OFP conversations."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.style import Style
from rich.text import Text
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

# Color palette for agents
AGENT_COLORS = [
    "bright_blue",
    "bright_yellow",
    "bright_magenta",
    "bright_cyan",
    "bright_red",
    "green",
    "orange1",
    "medium_purple1",
]

HUMAN_COLOR = "bright_green"
SYSTEM_COLOR = "dim white"
FLOOR_MANAGER_COLOR = "grey50"

ICONS = {
    "utterance": "💬",
    "grantFloor": "🎤",
    "revokeFloor": "🔇",
    "requestFloor": "✋",
    "invite": "👋",
    "bye": "🚪",
    "system": "⚙️",
    "input": "→",
}


class TerminalRenderer:
    """Renders OFP conversations to the terminal using Rich."""

    def __init__(self, console: Optional[Console] = None, show_floor_events: bool = False):
        self._console = console or Console()
        self._agent_colors: dict[str, str] = {}
        self._color_idx = 0
        self._human_uris: set[str] = set()
        self.show_floor_events = show_floor_events

    def _get_color(self, speaker_uri: str) -> str:
        if "human" in speaker_uri:
            return HUMAN_COLOR
        if "floor-manager" in speaker_uri:
            return FLOOR_MANAGER_COLOR
        if speaker_uri not in self._agent_colors:
            self._agent_colors[speaker_uri] = AGENT_COLORS[
                self._color_idx % len(AGENT_COLORS)
            ]
            self._color_idx += 1
        return self._agent_colors[speaker_uri]

    def add_agent(self, speaker_uri: str, name: str) -> None:
        """Pre-register an agent to assign it a color."""
        if "human" not in speaker_uri:
            self._get_color(speaker_uri)
        else:
            self._human_uris.add(speaker_uri)

    def _timestamp(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def show_utterance(
        self,
        speaker_uri: str,
        speaker_name: str,
        text: str,
        media_key: str | None = None,
        media_path: str | None = None,
    ) -> None:
        color = self._get_color(speaker_uri)
        ts = self._timestamp()
        line = Text()
        line.append(f"[{ts}] ", style=SYSTEM_COLOR)
        line.append(f"{ICONS['utterance']} ", style="")
        line.append(f"{speaker_name}", style=f"bold {color}")
        line.append(": ", style=color)
        line.append(text)
        if media_key == "image" and media_path:
            line.append(f"  🖼  {media_path}", style="dim cyan")
        elif media_key == "video" and media_path:
            line.append(f"  🎬  {media_path}", style="dim magenta")
        elif media_key and media_path:
            line.append(f"  [{media_key}] {media_path}", style="dim")
        self._console.print(line)

    def show_system_event(self, message: str) -> None:
        ts = self._timestamp()
        line = Text()
        line.append(f"[{ts}] ", style=SYSTEM_COLOR)
        line.append(f"{ICONS['system']} ", style="")
        line.append(message, style=SYSTEM_COLOR)
        self._console.print(line)

    def show_input_prompt(self, name: str) -> None:
        self._console.print(
            Text().append(f"\n{ICONS['input']} {name}: ", style=f"bold {HUMAN_COLOR}"),
            end="",
        )

    def show_header(self, conversation_id: str, policy: str, agent_count: int) -> None:
        table = Table.grid(padding=(0, 2))
        table.add_column()
        table.add_column()
        table.add_row(
            f"[bold]OFP Playground[/bold]",
            f"Conv: [dim]{conversation_id[:12]}...[/dim]  "
            f"Policy: [cyan]{policy}[/cyan]  "
            f"Agents: [yellow]{agent_count}[/yellow]",
        )
        self._console.print(Panel(table, style="blue"))

    def show_agents_table(self, agents: dict[str, str], floor_holder: Optional[str] = None) -> None:
        table = Table(title="Active Agents", show_header=True)
        table.add_column("Name", style="bold")
        table.add_column("URI", style="dim")
        table.add_column("Status")

        for uri, name in agents.items():
            status = "🎤 Has Floor" if uri == floor_holder else "Idle"
            color = self._get_color(uri)
            table.add_row(
                Text(name, style=f"bold {color}"),
                Text(uri.split(":")[-1], style="dim"),
                status,
            )
        self._console.print(table)

    def show_help(self) -> None:
        self._console.print(Panel(
            "[bold]Available Commands:[/bold]\n"
            "  [cyan]/spawn[/cyan] <type> <name> [description]  — Spawn LLM agent\n"
            "  [cyan]/connect[/cyan] <url> [name]               — Connect remote OFP agent\n"
            "  [cyan]/agents[/cyan]                             — List active agents\n"
            "  [cyan]/floor[/cyan]                              — Show floor status\n"
            "  [cyan]/history[/cyan] [n]                        — Show message history\n"
            "  [cyan]/grant[/cyan] <name>                       — Grant floor (moderated mode)\n"
            "  [cyan]/kick[/cyan] <name>                        — Remove agent\n"
            "  [cyan]/quit[/cyan]                               — Exit",
            title="Help",
            style="green",
        ))

    def show_manuscript(self, text: str, filepath: Optional[str] = None) -> None:
        """Display the final assembled manuscript in a panel."""
        word_count = len(text.split())
        title = f"[bold green]Final Manuscript[/bold green] [dim]({word_count} words)[/dim]"
        if filepath:
            title += f"  [dim cyan]saved → {filepath}[/dim cyan]"
        self._console.print(Panel(text, title=title, border_style="green", padding=(1, 2)))

    def print(self, *args, **kwargs) -> None:
        self._console.print(*args, **kwargs)
