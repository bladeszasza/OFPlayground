"""Check local Markdown destinations in the project's current user documentation."""
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    failures = []
    paths = [ROOT / "README.md", ROOT / "CLAUDE.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        # Ignore fenced code, where example URLs/paths need not exist.
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        for label, target in re.findall(r"\[([^\]]+)\]\(([^\s)]+)(?:\s+[^)]*)?\)", text):
            target = target.strip("<>")
            url = urlsplit(target)
            if url.scheme or target.startswith(("#", "//")):
                continue
            destination = path.parent / unquote(url.path)
            if not destination.exists():
                failures.append(f"{path.relative_to(ROOT)}: [{label}] -> {target}")
    for failure in failures:
        print(failure)
    print(f"Checked {len(paths)} documents; {len(failures)} missing destinations")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
