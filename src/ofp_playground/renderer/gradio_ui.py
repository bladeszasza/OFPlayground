"""Gradio Web UI renderer for OFP Playground conversations.

Targeting Gradio 6.10+.

Each OFP agent gets its own visual identity inside gr.Chatbot using the
metadata.title field — the sender name is placed there instead of being
baked into the message content string.  This produces the same clean
"agent name header above the bubble" look that the terminal renderer
achieves with Rich coloured text.

Media (images / video / audio) are rendered as native Gradio components
(gr.Image, gr.Video, gr.Audio) passed directly as message content, so
Gradio renders them inline rather than as raw file paths.

System events (floor grants, agent joins) use role="system" so Gradio can
display them in a dimmed, centred style.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
import threading
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# ── Agent icon table ──────────────────────────────────────────────────────────
_TASK_ICONS: dict[str, str] = {
    "image":      "🎨",
    "video":      "🎬",
    "music":      "🎵",
    "audio":      "🔊",
    "3d":         "🧊",
    "vision":     "👁️",
    "classifier": "🔍",
    "ocr":        "📄",
    "ner":        "🏷️",
    "web":        "🌐",
    "director":   "🎭",
    "orchestrator": "⚙️",
    "remote":     "🔗",
    "human":      "👤",
}

_AGENT_COLORS = [
    "#4A90D9",   # blue
    "#E8954A",   # orange
    "#5BA85B",   # green
    "#C05C5C",   # red
    "#8B6BB1",   # purple
    "#5BA8A8",   # teal
    "#C8973C",   # gold
    "#8B7355",   # brown
]


def _agent_icon(speaker_uri: str) -> str:
    """Pick an icon based on the URI's task hint."""
    uri_lower = speaker_uri.lower()
    for task, icon in _TASK_ICONS.items():
        if task in uri_lower:
            return icon
    return "💬"


def _agent_color(speaker_uri: str, color_map: dict[str, str]) -> str:
    """Assign a stable colour to this speaker URI, rotating through the palette."""
    if speaker_uri not in color_map:
        idx = len(color_map) % len(_AGENT_COLORS)
        color_map[speaker_uri] = _AGENT_COLORS[idx]
    return color_map[speaker_uri]


# ── Per-envelope conversion ───────────────────────────────────────────────────

def _envelope_to_chat_messages(
    envelope,
    agent_names: dict[str, str],
    human_uris: set[str],
    color_map: dict[str, str],
) -> list[dict]:
    """Convert one OFP envelope to a list of Gradio ChatMessage dicts.

    Returns an empty list if the envelope carries no displayable utterance.
    Multiple dicts are returned when a message contains both text and media —
    text first, then the media component, so they appear as consecutive
    bubbles from the same speaker.
    """
    import gradio as gr

    sender_uri: str = envelope.sender.speakerUri if envelope.sender else "unknown"
    sender_name: str = agent_names.get(sender_uri, sender_uri.split(":")[-1])
    icon = _agent_icon(sender_uri)
    color = _agent_color(sender_uri, color_map)
    is_human = sender_uri in human_uris

    for event in (envelope.events or []):
        event_type = getattr(event, "eventType", type(event).__name__)
        if event_type != "utterance":
            continue

        de = getattr(event, "dialogEvent", None)
        if not de or not de.features:
            continue

        text = ""
        text_feat = de.features.get("text")
        if text_feat and text_feat.tokens:
            text = " ".join(t.value for t in text_feat.tokens if t.value)

        # Detect media features
        media_key: str | None = None
        media_path: str | None = None
        for key in ("image", "video", "audio", "3d"):
            feat = de.features.get(key)
            if feat and feat.tokens and feat.tokens[0].value:
                media_key = key
                media_path = feat.tokens[0].value
                break

        role = "user" if is_human else "assistant"
        # Embed colour hint in title so CSS can target it
        title_with_color = f'<span style="color:{color};font-weight:700">{icon} {sender_name}</span>'

        messages: list[dict] = []

        # Highlight [TASK_COMPLETE] in display text
        task_complete = "[TASK_COMPLETE]" in text
        display_text = (
            text.replace("[TASK_COMPLETE]", "**✅ [TASK_COMPLETE]**")
            if task_complete else text
        )

        # Text message
        if display_text:
            messages.append({
                "role": role,
                "content": display_text,
                "metadata": {"title": title_with_color},
            })

        # Media message — use native Gradio components where possible
        if media_key and media_path:
            path_obj = Path(media_path)
            if path_obj.exists():
                media_content: gr.Image | gr.Video | gr.Audio | str
                if media_key == "image":
                    media_content = gr.Image(value=media_path)
                elif media_key == "video":
                    media_content = gr.Video(value=media_path)
                elif media_key == "audio":
                    media_content = gr.Audio(value=media_path)
                else:
                    # Fallback for 3d or unknown — show as a link
                    media_content = f"📎 [{path_obj.name}]({media_path})"
                messages.append({
                    "role": role,
                    "content": media_content,
                    "metadata": {"title": title_with_color},
                })
            elif text == "":
                # Path not found and no text — at least show the path
                messages.append({
                    "role": role,
                    "content": f"📎 {media_path}",
                    "metadata": {"title": title_with_color},
                })

        # HTML file preview — embed as sandboxed iframe via data URL
        html_match = re.search(r"(/[^\s<>\"']+\.html)", text)
        if html_match:
            html_file = Path(html_match.group(1))
            if html_file.exists():
                try:
                    iframe = (
                        f'<iframe src="/file={html_file.resolve()}" '
                        f'width="100%" height="520" '
                        f'style="border:1px solid #444;border-radius:10px;'
                        f'margin-top:6px;display:block;" '
                        f'sandbox="allow-scripts allow-same-origin allow-forms allow-popups"></iframe>'
                    )
                    messages.append({
                        "role": role,
                        "content": gr.HTML(value=iframe),
                        "metadata": {"title": title_with_color},
                    })
                except Exception:
                    logger.exception("Operation failed at an application error boundary")
                    # silently skip if file can't be read/encoded

        # Task-complete banner
        if task_complete:
            banner = (
                '<div style="'
                "background:linear-gradient(135deg,#0a2e0a,#1a5c1a);"
                "border:2px solid #4caf50;border-radius:14px;"
                "padding:18px 28px;text-align:center;margin:4px 0;"
                '">'
                '<span style="color:#6eff6e;font-size:1.5rem;font-weight:800;">'
                "✅ Task Complete"
                "</span>"
                "</div>"
            )
            messages.append({
                "role": role,
                "content": gr.HTML(value=banner),
                "metadata": {"title": title_with_color},
            })

        return messages

    return []


def _floor_event_message(text: str) -> dict:
    """Produce a system-style message for floor / session events."""
    return {
        "role": "system",
        "content": text,
        "metadata": {"title": "🗂️ Session"},
    }


# ── GradioRenderer ────────────────────────────────────────────────────────────

class GradioRenderer:
    """Bridges the OFP bus to a Gradio Chatbot.

    Maintains a thread-safe chat history list.  The UI polls
    get_history() on a timed tick.  System events (floor grants,
    agent joins) can be injected via add_system_event().
    """

    def __init__(
        self,
        agent_names: dict[str, str],
        human_uris: set[str],
    ):
        self._agent_names = agent_names
        self._human_uris = human_uris
        self._color_map: dict[str, str] = {}
        self._history: list[dict] = []
        self._agent_meta: dict[str, str] = {}  # uri → "type / model"
        self._status_snapshot: dict = {"agents": []}
        self._status_participants: dict[str, str] = {}
        self._participant_choices: list[tuple[str, str]] = []
        self._status_available = False
        self._status_problem: str | None = "Waiting for ASP observation"
        self._lock = threading.Lock()

    def update_status(self, snapshot: dict | None, participants: dict[str, str],
                      choices: list[tuple[str, str]], *, available: bool,
                      problem: str | None = None) -> None:
        with self._lock:
            if snapshot is not None:
                self._status_snapshot = copy.deepcopy(snapshot)
            self._status_participants = dict(participants)
            self._participant_choices = list(choices)
            self._status_available = available
            self._status_problem = problem

    def status_markdown(self) -> str:
        from ofp_playground.renderer.status_text import markdown_status

        with self._lock:
            return markdown_status(self._status_snapshot, self._status_participants,
                                   available=self._status_available, problem=self._status_problem)

    def participant_choices(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._participant_choices)

    def add_user_input(self, name: str, text: str) -> None:
        with self._lock:
            self._history.append({"role": "user", "content": text,
                                  "metadata": {"title": f"{name} · queued input"}})

    def add_agent(self, speaker_uri: str, name: str) -> None:
        with self._lock:
            self._agent_names[speaker_uri] = name

    def add_agent_meta(self, speaker_uri: str, label: str) -> None:
        with self._lock:
            self._agent_meta[speaker_uri] = label

    def add_human(self, speaker_uri: str) -> None:
        with self._lock:
            self._human_uris.add(speaker_uri)

    def add_system_event(self, text: str) -> None:
        with self._lock:
            self._history.append(_floor_event_message(text))

    def ingest_envelope(self, envelope) -> bool:
        """Process an incoming envelope; returns True if history changed."""
        msgs = _envelope_to_chat_messages(
            envelope, self._agent_names, self._human_uris, self._color_map
        )
        if not msgs:
            return False
        with self._lock:
            self._history.extend(msgs)
        return True

    def get_history(self) -> list[dict]:
        with self._lock:
            return list(self._history)


# ── Background queue drainer ──────────────────────────────────────────────────

async def _drain_output_queue(
    output_queue: asyncio.Queue,
    gradio_renderer: GradioRenderer,
) -> None:
    """Continuously drain the output queue and feed the renderer."""
    while True:
        try:
            envelope = await asyncio.wait_for(output_queue.get(), timeout=1.0)
            gradio_renderer.ingest_envelope(envelope)
        except TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception('drain_output_queue error: see traceback')


# ── Custom CSS ────────────────────────────────────────────────────────────────

_CUSTOM_CSS = """
/* System event bubbles — dimmed, centred, smaller text */
.message.system { opacity: 0.65; font-style: italic; text-align: center; }

/* Metadata title — already coloured inline; enforce font size */
.message-metadata .title { font-size: 0.82rem; letter-spacing: 0.02em; line-height: 1.4; }

/* Give the chatbot a bit of breathing room */
.chatbot-container { gap: 6px !important; }

/* Smooth scroll to latest */
.chatbot-container > .wrap { scroll-behavior: smooth; }

/* Agent status panel */
#agent-status { font-size: 0.85rem; line-height: 1.7; padding: 8px 12px;
                border-left: 3px solid #4A90D9; background: rgba(74,144,217,0.06);
                border-radius: 4px; }

/* Spawn feedback message */
#spawn-msg { font-size: 0.82rem; min-height: 1.4em; }
"""


# ── Spawn panel ───────────────────────────────────────────────────────────────

def _build_spawn_panel(gr, loop, spawn_fn, gradio_renderer):
    """Build the 'Spawn Agent' accordion inside the right column.

    Loads the agent library (SOUL.md index) and wires:
      - Category dropdown  →  filters agent picker
      - Agent picker       →  auto-fills system prompt + name
      - Spawn button       →  calls spawn_fn(spec) on the event loop
    """
    from ofp_playground.agents import library as _lib
    from ofp_playground.agents.llm.model_catalog import MODEL_CATALOG
    from ofp_playground.config.settings import Settings

    agents_library = _lib.load()
    defaults = Settings.load().defaults

    def model_choices(provider):
        prefix = {"openai": "gpt", "anthropic": "claude", "google": "gemini"}.get(provider)
        catalog = [model for model in MODEL_CATALOG if prefix and model.startswith(prefix)]
        configured = getattr(defaults, "llm_model_" + ("huggingface" if provider == "hf" else provider), None)
        return sorted(set(catalog + ([configured] if configured else [])))
    categories = sorted({v["category"] for v in agents_library.values()})
    _all_agent_choices = sorted(
        [(v["display_name"], k) for k, v in agents_library.items()]
    )

    with gr.Accordion("Spawn Agent", open=False):
        # ── Library picker ─────────────────────────────────────────────
        if agents_library:
            gr.Markdown(
                f"*{len(agents_library)} agents across {len(categories)} categories*",
                elem_id="spawn-lib-hint",
            )
            category_dd = gr.Dropdown(
                choices=["(all)"] + categories,
                value="(all)",
                label="Category",
            )
            agent_dd = gr.Dropdown(
                choices=_all_agent_choices,
                value=None,
                label="Pick from library",
            )

        # ── Config ─────────────────────────────────────────────────────
        name_box = gr.Textbox(label="Name", placeholder="e.g. Copywriter")
        provider_dd = gr.Dropdown(
            choices=["demo", "demo:rate-limit", "demo:input-wait", "demo:silent", "demo:stalled",
                     "hf", "anthropic", "openai", "google"],
            value="hf",
            label="Provider",
        )
        model_box = gr.Dropdown(
            label="Model (optional)",
            choices=model_choices("hf"),
            value=None,
            allow_custom_value=True,
        )
        gr.Markdown("Search model choices or enter a provider model ID. Empty uses your configured default; availability is checked by the provider on use.")
        system_box = gr.Textbox(
            label="System prompt",
            lines=7,
            placeholder="Describe the agent, or pick one from the library above…",
        )
        spawn_btn = gr.Button("Spawn", variant="primary")
        spawn_msg = gr.Markdown("", elem_id="spawn-msg")

        provider_dd.change(lambda provider: gr.Dropdown(choices=model_choices(provider), value=None,
                                                       allow_custom_value=True), [provider_dd], [model_box])

        # ── Event handlers ─────────────────────────────────────────────
        if agents_library:
            def _filter_by_category(category):
                if category == "(all)":
                    choices = _all_agent_choices
                else:
                    choices = sorted(
                        [(v["display_name"], k) for k, v in agents_library.items()
                         if v["category"] == category]
                    )
                return gr.Dropdown(choices=choices, value=None)

            def _load_agent(slug):
                if not slug or slug not in agents_library:
                    return "", ""
                entry = agents_library[slug]
                # CamelCase name from first two words of display_name
                suggested = "".join(
                    w.capitalize() for w in entry["display_name"].split()[:2]
                )
                return entry["content"], suggested

            category_dd.change(_filter_by_category, [category_dd], [agent_dd])
            agent_dd.change(_load_agent, [agent_dd], [system_box, name_box])

        def _do_spawn(provider, name, system, model):
            name = name.strip()
            system = system.strip()
            if not name:
                return "**Name is required.**"
            if not system:
                return "**System prompt is required.**"
            parts = [f"-provider {provider}", f"-name {name}", f"-system {system}"]
            if model and model.strip():
                parts.append(f"-model {model.strip()}")
            spec = " ".join(parts)
            future = asyncio.run_coroutine_threadsafe(spawn_fn(spec), loop)
            try:
                future.result(timeout=30)
            except TimeoutError:
                future.cancel()
                return "**Adding the agent timed out. Check session status before retrying.**"
            except Exception:
                logger.exception("Web participant add failed")
                return "**Agent could not be added. Check the session messages for details.**"
            return f"Added **{name}** ({provider})."

        spawn_btn.click(
            _do_spawn,
            [provider_dd, name_box, system_box, model_box],
            [spawn_msg],
        )


# ── Main entry-point ──────────────────────────────────────────────────────────

class WebServerHandle(Protocol):
    def close(self) -> None: ...


def launch_web_session(
    floor,
    bus,
    human_agent,  # WebHumanAgent | None
    agent_specs_display: list[str],
    policy_name: str,
    loop: asyncio.AbstractEventLoop,
    host: str = "127.0.0.1",
    port: int = 7860,
    share: bool = False,
    gradio_renderer: GradioRenderer | None = None,
    spawn_fn=None,  # Optional async coroutine: (spec: str) -> None
    remove_fn=None,
    governance_fn=None,
    input_fn=None,
    stop_fn=None,
) -> WebServerHandle:
    """Launch the Gradio web UI and return its server handle to the session owner.

    When human_agent is None the UI is watch-only (autonomous agent mode).
    When gradio_renderer is provided (pre-created with spawn events), it is used
    directly instead of creating a fresh one.
    """
    import gradio as gr

    if gradio_renderer is None:
        human_uris = {human_agent.speaker_uri} if human_agent else set()
        gradio_renderer = GradioRenderer(
            agent_names=dict(floor.active_agents),
            human_uris=human_uris,
        )

    # ── Helper: build actual multidimensional ASP observations ──────────────
    def _build_status_md() -> str:
        return gradio_renderer.status_markdown()

    # ── UI submit helper ──────────────────────────────────────────────────────
    def submit_message(user_text: str, history: list[dict]):
        if not user_text or not user_text.strip():
            return "", history, _build_status_md()
        if input_fn is None:
            raise gr.Error("Input is unavailable for this session.")
        future = asyncio.run_coroutine_threadsafe(input_fn(user_text), loop)
        try:
            future.result(timeout=3)
        except TimeoutError:
            future.cancel()
            raise gr.Error("Input acknowledgement timed out. Check the conversation before retrying.") from None
        except Exception:
            logger.exception("Web input rejected")
            raise gr.Error("Input was rejected. The participant or session may have ended.") from None
        return "", gradio_renderer.get_history(), _build_status_md()

    # ── Timer tick: merge new messages + update status ────────────────────────
    def poll_updates(history: list[dict]):
        return gradio_renderer.get_history(), _build_status_md()

    # ── Layout ────────────────────────────────────────────────────────────────
    mode_label = "Autonomous (watch-only)" if not human_agent else "Interactive"

    with gr.Blocks(
        title="OFP Playground",
        fill_width=True,
    ) as demo:
        with gr.Row(equal_height=False):
            # ── Left: chat area ────────────────────────────────────────────
            with gr.Column(scale=4):
                gr.Markdown(
                    f"## OFP Playground\n"
                    f"**Mode**: {mode_label}  ·  **Policy**: `{policy_name}`"
                )

                chatbot = gr.Chatbot(
                    label="Conversation",
                    height=620,
                    elem_classes=["chatbot-container"],
                )

                if human_agent:
                    with gr.Row():
                        msg_box = gr.Textbox(
                            placeholder="Type a message and press Enter…",
                            show_label=False,
                            scale=8,
                            autofocus=True,
                            submit_btn=False,
                        )
                        send_btn = gr.Button("Send ↵", scale=1, variant="primary")
                else:
                    gr.Markdown("*Watch-only mode — agents are talking autonomously.*")

            # ── Right: agent status + spawn panel ─────────────────────────
            with gr.Column(scale=1, min_width=240):
                gr.Markdown("### Agent Status")
                status_md = gr.Markdown(
                    _build_status_md(),
                    elem_id="agent-status",
                )

                if spawn_fn:
                    _spawn_ui_components = _build_spawn_panel(
                        gr, loop, spawn_fn, gradio_renderer
                    )

                participant = gr.Dropdown(label="Participant", choices=gradio_renderer.participant_choices())
                removal_mode = gr.Radio(choices=["graceful", "force"], value="graceful", label="Removal mode")
                gr.Markdown("Graceful lets current work finish for up to 10s; force cancels local work. Submitted provider jobs may continue remotely.")
                remove_btn = gr.Button("Remove participant", variant="stop", interactive=remove_fn is not None)
                governance = gr.Radio(choices=["under-review", "quarantined", "normal"], value="under-review", label="Governance")
                gr.Markdown("Quarantine removes and cancels the participant. Clearing a restriction does not restart it.")
                governance_btn = gr.Button("Apply governance", interactive=governance_fn is not None)
                control_msg = gr.Markdown("")
                stop_btn = gr.Button("Stop session", variant="stop", interactive=stop_fn is not None)

                def _control(uri, value, kind):
                    if not uri:
                        return "Select a participant first."
                    from ofp_playground.renderer.status_text import operation_text
                    fn = remove_fn if kind == "remove" else governance_fn
                    if fn is None:
                        return "Participant controls are unavailable for this embedded session view."
                    future = asyncio.run_coroutine_threadsafe(fn(uri, value), loop)
                    try:
                        return operation_text(future.result(timeout=15))
                    except TimeoutError:
                        # Do not cancel a potentially enforced restriction. Observation
                        # can recover its final result without repeating the command.
                        return "Control acknowledgement timed out; enforcement is unknown. Check Agent Status before retrying."
                    except Exception:
                        logger.exception("Web participant control failed")
                        return "Control failed. Check participant membership and session messages."

                remove_btn.click(lambda uri, mode: _control(uri, mode, "remove"),
                                 [participant, removal_mode], [control_msg])
                governance_btn.click(lambda uri, value: _control(uri, value, "governance"),
                                     [participant, governance], [control_msg])

                def _stop():
                    if stop_fn is None:
                        return "Stopping is unavailable for this embedded session view."
                    loop.call_soon_threadsafe(stop_fn)
                    return "Stop requested; the session owner will join all local tasks."

                stop_btn.click(_stop, outputs=[control_msg])

                def _refresh_choices(selected):
                    choices = gradio_renderer.participant_choices()
                    return gr.Dropdown(choices=choices, value=selected if selected in {uri for _, uri in choices} else None)

                participant_timer = gr.Timer(2)
                participant_timer.tick(_refresh_choices, inputs=[participant], outputs=[participant],
                                       show_progress="hidden", api_visibility="private")

        # Wire submit handlers now that all components exist
        if human_agent:
            msg_box.submit(
                submit_message,
                [msg_box, chatbot],
                [msg_box, chatbot, status_md],
            )
            send_btn.click(
                submit_message,
                [msg_box, chatbot],
                [msg_box, chatbot, status_md],
            )

        # Timer — ticks every 0.5 s, hidden progress indicator
        timer = gr.Timer(0.5)
        timer.tick(
            poll_updates,
            inputs=[chatbot],
            outputs=[chatbot, status_md],
            show_progress="hidden",
            api_visibility="private",
        )

    # ── Allowed media paths ───────────────────────────────────────────────────
    output_obj = getattr(floor, "_output", None)
    if output_obj is not None:
        result_root = Path(str(output_obj.root)).resolve()
    else:
        result_root = Path("result").resolve()
    result_root.mkdir(parents=True, exist_ok=True)

    # Legacy flat dirs still supported alongside per-session result/
    legacy_images = Path("ofp-images").resolve()
    legacy_videos = Path("ofp-videos").resolve()
    legacy_music = Path("ofp-music").resolve()
    for d in (legacy_images, legacy_videos, legacy_music):
        d.mkdir(parents=True, exist_ok=True)

    demo.launch(
        server_name=host,
        server_port=port,
        share=share,
        prevent_thread_lock=True,
        theme=gr.themes.Soft(),
        css=_CUSTOM_CSS,
        allowed_paths=[
            str(result_root),
            str(legacy_images),
            str(legacy_videos),
            str(legacy_music),
        ],
        footer_links=[],
    )
    logger.info("Gradio UI launched at http://%s:%d", host, port)
    return demo
