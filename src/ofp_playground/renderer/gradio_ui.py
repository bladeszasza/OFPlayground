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
import logging
import threading
from pathlib import Path
from typing import Optional

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
        title = f"{icon} {sender_name}"
        # Embed colour hint in title so CSS can target it
        title_with_color = f'<span style="color:{color};font-weight:700">{icon} {sender_name}</span>'

        messages: list[dict] = []

        # Text message
        if text:
            messages.append({
                "role": role,
                "content": text,
                "metadata": {"title": title_with_color},
            })

        # Media message — use native Gradio components where possible
        if media_key and media_path:
            path_obj = Path(media_path)
            if path_obj.exists():
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
        self._lock = threading.Lock()

    def add_agent(self, speaker_uri: str, name: str) -> None:
        with self._lock:
            self._agent_names[speaker_uri] = name

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
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("drain_output_queue error: %s", e)


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
"""


# ── Main entry-point ──────────────────────────────────────────────────────────

def launch_web_session(
    floor,
    bus,
    human_agent,  # WebHumanAgent | None
    agent_specs_display: list[str],
    policy_name: str,
    loop: asyncio.AbstractEventLoop,
    host: str = "0.0.0.0",
    port: int = 7860,
    share: bool = False,
) -> None:
    """Launch the Gradio web UI.  Must be called from the main thread after the
    asyncio event loop is already running in a background thread.

    When human_agent is None the UI is watch-only (autonomous agent mode).
    """
    import gradio as gr

    human_uris = {human_agent.speaker_uri} if human_agent else set()
    gradio_renderer = GradioRenderer(
        agent_names=dict(floor.active_agents),
        human_uris=human_uris,
    )

    if human_agent:
        asyncio.run_coroutine_threadsafe(
            _drain_output_queue(human_agent.output_queue, gradio_renderer),
            loop,
        )
    else:
        observer_queue: asyncio.Queue = asyncio.Queue()

        async def _register_and_drain():
            observer_uri = "tag:ofp-playground.local,2025:web-observer"
            await bus.register(observer_uri, observer_queue)
            await _drain_output_queue(observer_queue, gradio_renderer)

        asyncio.run_coroutine_threadsafe(_register_and_drain(), loop)

    # ── Helper: build agent-status markdown ──────────────────────────────────
    def _build_status_md() -> str:
        agents = getattr(floor, "active_agents", {})
        holder = getattr(floor, "floor_holder", None)
        lines = []
        for uri, name in agents.items():
            icon = _agent_icon(uri)
            if uri == holder:
                lines.append(f"**{icon} {name}** 🎤 ← *has floor*")
            else:
                lines.append(f"{icon} {name}")
        if not lines:
            lines = ["*No agents yet*"]
        return "\n".join(lines)

    # ── UI submit helper ──────────────────────────────────────────────────────
    def submit_message(user_text: str, history: list[dict]):
        if not user_text or not user_text.strip():
            return "", history, _build_status_md()
        icon = "👤"
        name = human_agent.name if human_agent else "User"
        history = history + [{
            "role": "user",
            "content": user_text,
            "metadata": {"title": f'<span style="color:#888;font-weight:700">{icon} {name}</span>'},
        }]
        asyncio.run_coroutine_threadsafe(
            human_agent.input_queue.put(user_text),
            loop,
        )
        return "", history, _build_status_md()

    # ── Timer tick: merge new messages + update status ────────────────────────
    def poll_updates(history: list[dict]):
        full = gradio_renderer.get_history()
        existing_count = len(history)
        new_msgs = full[existing_count:]
        if human_agent:
            # Suppress echoes of the user's own text we already added optimistically
            new_msgs = [m for m in new_msgs if m.get("role") != "user"]
        return history + new_msgs, _build_status_md()

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

            # ── Right: agent status panel ──────────────────────────────────
            with gr.Column(scale=1, min_width=200):
                gr.Markdown("### Active Agents")
                status_md = gr.Markdown(
                    _build_status_md(),
                    elem_id="agent-status",
                )
                gr.Markdown(
                    f"**Session**: {', '.join(agent_specs_display)}",
                    elem_id="agent-list",
                )

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
