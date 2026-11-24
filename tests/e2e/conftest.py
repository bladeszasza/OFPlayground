"""Canvas browser tests: a real `ofp-playground canvas` process driven by Playwright.

Opt in with ``OFP_E2E=1`` (and ``uv sync --extra e2e``); plain ``pytest`` skips
them. ``OFP_E2E_LIVE=1`` also runs the tests that call a model provider.

Numbered screenshots and a Playwright trace (``trace.zip``, with screencast;
open it with ``playwright show-trace``) per test go to
``result/ui-tests/<test name>/`` (gitignored), overwritten on every run, so
later tooling (demo videos, docs) can rely on stable paths.

Environment:
    OFP_E2E_CHANNEL   installed browser channel (default: msedge on Windows, chrome elsewhere)
    OFP_E2E_HEADED=1  show the browser window
    OFP_E2E_SLOWMO    milliseconds between browser actions (default 0)
    OFP_E2E_VIDEO=1   also record ``session.webm`` (needs ``playwright install ffmpeg``)
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
ARTIFACTS = REPO / "result" / "ui-tests"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def canvas_url() -> Iterator[str]:
    """Start the Canvas server (status instrumentation on, as for a user)."""
    port = _free_port()
    # Status instrumentation stays on, but tests never mirror synthetic agents
    # (or their quarantine) to a shared ASP server.
    dropped = {"OFP_STATUS_DISABLED", "OFP_ASP_MIRROR", "ASP_SERVER", "ASP_TOKEN"}
    env = {key: value for key, value in os.environ.items() if key not in dropped}
    env["OFP_ASP_MIRROR"] = "0"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    log = (ARTIFACTS / "canvas-server.log")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "ofp_playground", "canvas", "--no-open", "--host", "127.0.0.1",
             "--port", str(port)],
            cwd=REPO, env=env, stdout=output, stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Canvas exited early; see {log}")
                try:
                    if httpx.get(url + "/", timeout=2, trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError(f"Canvas did not start; see {log}")
                time.sleep(0.25)
            yield url
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


@pytest.fixture(scope="session")
def browser() -> Iterator[object]:
    from playwright.sync_api import sync_playwright

    channel = os.environ.get("OFP_E2E_CHANNEL") or ("msedge" if sys.platform == "win32" else "chrome")
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(
            channel=channel,
            headless=os.environ.get("OFP_E2E_HEADED") != "1",
            slow_mo=float(os.environ.get("OFP_E2E_SLOWMO", "0")),
        )
        yield instance
        instance.close()


class Shots:
    """Numbered screenshots for one test."""

    def __init__(self, page, folder: Path):
        self.page = page
        self.folder = folder
        self.count = 0

    def __call__(self, name: str, *, full_page: bool = False) -> Path:
        self.count += 1
        path = self.folder / f"{self.count:02d}-{name}.png"
        self.page.screenshot(path=str(path), full_page=full_page)
        return path


@pytest.fixture
def ui(request, browser, canvas_url):
    """A fresh 1600x900 page with video, plus a screenshot helper."""
    # Parametrized names may contain URLs; keep the folder name valid on Windows.
    folder = ARTIFACTS / re.sub(r"[^\w.\-\[\]]+", "_", request.node.name).strip("_")
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    size = {"width": 1600, "height": 900}
    video = ({"record_video_dir": str(folder), "record_video_size": size}
             if os.environ.get("OFP_E2E_VIDEO") == "1" else {})
    context = browser.new_context(viewport=size, color_scheme="dark", **video)
    context.tracing.start(title=request.node.name, screenshots=True, snapshots=True)
    page = context.new_page()
    page.set_default_timeout(30_000)
    try:
        page.goto(canvas_url + "/")
        page.get_by_label("Load preset").wait_for()
        yield page, Shots(page, folder)
    finally:
        recording = page.video
        context.tracing.stop(path=str(folder / "trace.zip"))
        context.close()
        if recording is not None:
            recorded = Path(recording.path())
            if recorded.exists():
                recorded.replace(folder / "session.webm")
