"""Plain, content-free ASP presentation shared by the terminal and web UI."""
from __future__ import annotations

import time
from typing import Any


def status_rows(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """Keep the five dimensions and provenance distinct, including stale values."""
    rows = []
    for agent in snapshot.get("agents", []):
        def cell(value: dict | None) -> str:
            if not value:
                return "unknown"
            return str(value.get("value", "unknown")) + (" [stale]" if value.get("stale") else "")

        task_values = agent.get("tasks", {})
        floor_values = agent.get("floors", {})
        sources = sorted({str(value.get("source", "unknown"))
                          for value in [agent.get("lifecycle", {}), agent.get("health", {}),
                                        agent.get("governance", {}), *task_values.values(), *floor_values.values()]})
        seen = agent.get("lastSelfEventAt")
        rows.append({
            "id": str(agent["agentId"]), "name": str(agent.get("name", agent["agentId"])),
            "lifecycle": cell(agent.get("lifecycle")), "health": cell(agent.get("health")),
            "governance": cell(agent.get("governance")),
            "tasks": ", ".join(f"{key[:8]}: {cell(value)}" for key, value in list(task_values.items())[-5:]) or "none observed",
            "floors": ", ".join(f"{key[:8]}: {cell(value)}" for key, value in list(floor_values.items())[-5:]) or "none observed",
            "source": ", ".join(sources),
            "seen": f"{max(0, time.time() - seen):.0f}s ago" if isinstance(seen, int | float) else "unknown",
        })
    return rows


def markdown_status(snapshot: dict[str, Any], participants: dict[str, str], *,
                    available: bool, problem: str | None = None) -> str:
    """Render observations; active membership alone never implies health."""
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("|", "\\|").replace("<", "&lt;").replace("\n", " ")

    rows = status_rows(snapshot)
    lines = ["ASP observation live." if available else "**ASP observation unavailable — retained observations are stale.**"]
    if problem:
        lines.append(escape(problem))
    if rows:
        lines += ["", "| Agent | Lifecycle | Health | Governance |", "| --- | --- | --- | --- |"]
        for row in rows:
            lines.append("| " + " | ".join(escape(row[key]) for key in ("name", "lifecycle", "health", "governance")) + " |")
        for row in rows:
            lines += ["", f"**{escape(row['name'])}** · {escape(row['source'])} · last self report {row['seen']}",
                      f"Tasks: {escape(row['tasks'])}  ", f"Floors: {escape(row['floors'])}"]
        for agent in snapshot.get("agents", []):
            for reporter, observation in agent.get("observations", {}).items():
                lines.append(f"Independent observation for {escape(str(agent.get('name', agent['agentId'])))}: "
                             f"{escape(str(observation.get('value', 'unknown')))} "
                             f"({escape(str(observation.get('source', 'unknown')))} / {escape(reporter)}).")
    known = {str(agent["agentId"]) for agent in snapshot.get("agents", [])}
    unknown = [name for instance_id, name in participants.items() if instance_id not in known]
    if unknown:
        lines += ["", "Unknown / uninstrumented: " + ", ".join(escape(name) for name in unknown)]
    if not rows and not participants:
        lines.append("No participants yet. Add an agent to start observing.")
    return "\n".join(lines)


def operation_text(operation: dict[str, Any]) -> str:
    """Report control evidence without upgrading a request to an enforced result."""
    return "; ".join(f"{key}: {operation[key]}" for key in
                     ("operationId", "operation", "action", "state", "requested", "acknowledged", "enforced", "outcome", "mode", "fallback", "problem", "error")
                     if key in operation)
