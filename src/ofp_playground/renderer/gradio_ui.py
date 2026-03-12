"""Gradio Web UI renderer for OFP Playground conversations."""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Gradio message format: {"role": "user"|"assistant", "content": str | list}
# For images: content = [{"type": "image", "url": path}, {"type": "text", "text": caption}]


def _envelope_to_chat_message(
    envelope,
    agent_names: dict[str, str],
    human_uris: set[str],
) -> Optional[dict]:
    """Convert an OFP envelope to a Gradio chatbot message dict.

    Returns None if the envelope carries no displayable utterance.
    """
    from openfloor import Envelope  # noqa: F401 — type hint only

    sender_uri: str = envelope.sender.speakerUri if envelope.sender else "unknown"
    sender_name: str = agent_names.get(sender_uri, sender_uri.split(":")[-1])

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

        # Check for media features
        media_key: str | None = None
        media_path: str | None = None
        for key in ("image", "video", "audio", "3d"):
            feat = de.features.get(key)
            if feat and feat.tokens and feat.tokens[0].value:
                media_key = key
                media_path = feat.tokens[0].value
                break

        role = "user" if sender_uri in human_uris else "assistant"

        # Build content
        if media_key == "image" and media_path and Path(media_path).exists():
            content = [
                {"type": "image", "url": media_path},
                {"type": "text", "text": f"**{sender_name}**: {text}"},
            ]
        elif media_key == "video" and media_path and Path(media_path).exists():
            content = [
                {"type": "video", "url": media_path},
                {"type": "text", "text": f"**{sender_name}**: {text}"},
            ]
        else:
            content = f"**{sender_name}**: {text}"

        return {"role": role, "content": content}

    return None


class GradioRenderer:
    """Bridges the OFP output_queue of a WebHumanAgent to a Gradio Chatbot."""

    def __init__(
        self,
        agent_names: dict[str, str],
        human_uris: set[str],
    ):
        self._agent_names = agent_names
        self._human_uris = human_uris
        # Chat history as list of Gradio message dicts
        self._history: list[dict] = []
        self._lock = threading.Lock()

    def add_agent(self, speaker_uri: str, name: str) -> None:
        self._agent_names[speaker_uri] = name

    def add_human(self, speaker_uri: str) -> None:
        self._human_uris.add(speaker_uri)

    def ingest_envelope(self, envelope) -> bool:
        """Process an incoming envelope; returns True if history changed."""
        msg = _envelope_to_chat_message(envelope, self._agent_names, self._human_uris)
        if msg is None:
            return False
        with self._lock:
            self._history.append(msg)
        return True

    def get_history(self) -> list[dict]:
        with self._lock:
            return list(self._history)


async def _drain_output_queue(
    output_queue: asyncio.Queue,
    gradio_renderer: GradioRenderer,
    update_fn,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Continuously drain WebHumanAgent.output_queue and push updates to Gradio."""
    while True:
        try:
            envelope = await asyncio.wait_for(output_queue.get(), timeout=1.0)
            changed = gradio_renderer.ingest_envelope(envelope)
            if changed:
                # Schedule a UI refresh on the event loop (Gradio polls via yield)
                pass  # Gradio's streaming generator polls gradio_renderer.get_history()
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("drain_output_queue error: %s", e)


def launch_web_session(
    floor,
    bus,
    human_agent,
    agent_specs_display: list[str],
    policy_name: str,
    loop: asyncio.AbstractEventLoop,
    host: str = "0.0.0.0",
    port: int = 7860,
    share: bool = False,
) -> None:
    """Launch the Gradio web UI.  Must be called from the main thread after the
    asyncio event loop is already running in a background thread.
    """
    import gradio as gr

    gradio_renderer = GradioRenderer(
        agent_names=dict(floor.active_agents),
        human_uris={human_agent.speaker_uri},
    )

    # Register renderer with floor so newly spawned agents appear in names
    floor._renderer = None  # terminal renderer not used in web mode

    # Drain the WebHumanAgent output queue in the background event loop
    asyncio.run_coroutine_threadsafe(
        _drain_output_queue(human_agent.output_queue, gradio_renderer, None, loop),
        loop,
    )

    def submit_message(user_text: str, history: list[dict]):
        if not user_text.strip():
            return "", history

        # Add user message to local history immediately
        history = history + [{"role": "user", "content": f"**{human_agent.name}**: {user_text}"}]

        # Send into OFP bus via the human agent's input queue
        asyncio.run_coroutine_threadsafe(
            human_agent.input_queue.put(user_text),
            loop,
        )
        return "", history

    def poll_history(history: list[dict]):
        """Merge any new messages from agents into the chat history."""
        full = gradio_renderer.get_history()
        # Only append messages not already in `history`
        existing_count = len(history)
        new_msgs = full[existing_count:]
        # Filter out user messages we already added optimistically
        agent_msgs = [m for m in new_msgs if m["role"] == "assistant"]
        return history + agent_msgs

    with gr.Blocks(title="OFP Playground", theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            f"## OFP Playground\n"
            f"**Policy**: {policy_name}  |  "
            f"**Agents**: {', '.join(agent_specs_display)}"
        )

        chatbot = gr.Chatbot(
            label="Conversation",
            type="messages",
            height=600,
            show_copy_button=True,
        )
        with gr.Row():
            msg_box = gr.Textbox(
                placeholder="Type a message and press Enter...",
                show_label=False,
                scale=8,
                autofocus=True,
            )
            send_btn = gr.Button("Send", scale=1, variant="primary")

        # Submit on Enter or button click
        msg_box.submit(submit_message, [msg_box, chatbot], [msg_box, chatbot])
        send_btn.click(submit_message, [msg_box, chatbot], [msg_box, chatbot])

        # Auto-refresh to pull in agent responses (every 2 s)
        timer = gr.Timer(2.0)
        timer.tick(poll_history, inputs=[chatbot], outputs=[chatbot])

    demo.launch(
        server_name=host,
        server_port=port,
        share=share,
        prevent_thread_lock=True,
    )
    logger.info("Gradio UI launched at http://%s:%d", host, port)
