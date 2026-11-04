# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install for development
uv sync --locked --extra dev --extra canvas --extra web

# Run all tests
pytest

# Run a single test file
pytest tests/test_message_bus.py -v

# Run a specific test
pytest -k "test_name"

# Lint (ruff)
ruff check src/

# Type check
mypy src/
# Function bodies are checked; the untyped external SDK exception is scoped to openfloor.*.

# Run the CLI
ofp-playground start --help
ofp-playground web --help    # Gradio web UI (separate subcommand, not a --web flag)
ofp-playground agents        # List available agent slugs from agents/ library
ofp-playground validate      # Validate configuration
ofp-playground canvas        # Visual canvas UI (FastAPI + React, default port 8765)
```

Canvas dependencies are in a separate optional group:
```bash
pip install -e ".[canvas]"     # adds fastapi + uvicorn
# Build the frontend (only needed after frontend source changes)
cd canvas/frontend && npm install && npm run build
```

Key CLI flags for `start` and `web`: `--policy`, `--agent` (repeatable), `--remote` (remote agent slug/URL, repeatable), `--topic`, `--no-human` (fully autonomous sessions), `--max-turns`, `--human-name` (display name for the human participant), `--show-floor-events` (print grantFloor/revokeFloor/requestFloor protocol events; hidden by default), `-v`/`--verbose`.

Tests use `async def` without `@pytest.mark.asyncio` — `asyncio_mode = "auto"` is set in `pyproject.toml`. Ruff enforces `line-length = 100`, `target-version = "py310"`.

## Architecture

The system implements multi-agent conversations over the [Open Floor Protocol (OFP)](https://github.com/open-voice-interoperability/openfloor-python). All messaging is async and in-process via `asyncio.Queue`.

### Message Flow

```
Human/LLM Agents → MessageBus → target agent queues
                              → FloorManager queue (always receives a copy)
```

- **[MessageBus](src/ofp_playground/bus/message_bus.py)** routes OFP `Envelope` objects. If an event has no `to` field it broadcasts to all registered agents. The floor manager always receives a copy of every message.
- **[FloorManager](src/ofp_playground/floor/manager.py)** is the central coordinator. It holds conversation history, manages which agent holds the floor, and drives turn-taking via `FloorController`. It processes all OFP event types: utterances, floor requests/yields, manifest publications, and invite/bye/decline.
- **[FloorController](src/ofp_playground/floor/policy.py)** implements the 5 floor policies: `SEQUENTIAL`, `ROUND_ROBIN`, `MODERATED`, `FREE_FOR_ALL`, `SHOWRUNNER_DRIVEN`.

### Agent Hierarchy

```
BasePlaygroundAgent
├── HumanAgent          — reads stdin, writes stdout
├── WebHumanAgent       — Gradio web UI counterpart
├── RemoteOFPAgent      — proxies to a remote HTTP OFP endpoint
└── BaseLLMAgent
    ├── AnthropicAgent / AnthropicVisionAgent
    ├── OpenAIAgent / OpenAIImageAgent
    ├── GoogleAgent / GoogleImageAgent / GoogleMusicAgent / GoogleVideoAgent
    ├── HuggingFaceTextAgent / HFImageAgent / HFVideoAgent
    ├── DirectorAgent    — narrative director (ROUND_ROBIN); grants turn per agent each round
    ├── PerceptionBase subclasses (classifier, detector, ocr, ner, multimodal)
    ├── ShowrunnerAgent  — orchestrator that drives SHOWRUNNER_DRIVEN policy
    └── BaseCodingAgent(ABC)  — code generation base; task defaults _timeout=None (unlimited), _max_retries=2
        ├── OpenAICodingAgent   — Responses API + code_interpreter (default: gpt-5.4-long-context)
        ├── AnthropicCodingAgent — code_execution_20250825 beta (default: claude-opus-4-6)
        └── GoogleCodingAgent   — ToolCodeExecution (default: gemini-3-flash-preview)
```

**Relevance filter**: In non-SHOWRUNNER policies (FREE_FOR_ALL, ROUND_ROBIN), `BaseLLMAgent` calls `_check_relevance()` before each response — a quick yes/no LLM call asking whether the agent has something relevant to contribute. Orchestrator, coding, and director agents always bypass it (`relevance_filter=False`). Can be disabled globally via `settings.defaults.relevance_filter = false` in `~/.ofp-playground/config.toml`.

All agents extend [`BasePlaygroundAgent`](src/ofp_playground/agents/base.py) which provides:
- `send_envelope()` / `send_private_utterance()` helpers
- `request_floor()` / `yield_floor()` with anti-duplicate logic
- `_call_with_retry()` — timeout + exponential backoff (handles 429s, 5xx, timeouts; non-retryable errors are raised immediately)
- `_build_manifest()` / `_publish_manifest()` for OFP capability advertisement

`BaseLLMAgent._build_manifest()` uses [`model_catalog.py`](src/ofp_playground/agents/llm/model_catalog.py) (`ModelCaps` frozen dataclass) to publish rich OFP manifests (modalities + features + tools keyphrases) for 9 known models (3 OpenAI, 3 Anthropic, 3 Google). Unknown models fall back to a minimal text-in/text-out manifest.

### Coding Agents

CLI subtype `code-generation` routes to provider-specific subclasses:

| CLI | Class | Native tool |
|-----|-------|-------------|
| `openai:code-generation` | `OpenAICodingAgent` | `code_interpreter` via Responses API |
| `anthropic:code-generation` | `AnthropicCodingAgent` | `code_execution_20250825` beta |
| `google:code-generation` | `GoogleCodingAgent` | `ToolCodeExecution` |
| `hf:code-generation` | — | Unsupported (prints message) |

`BaseCodingAgent` is in `codex.py`. The Anthropic and Google subclasses are in `anthropic_coding.py` and `google_coding.py`. `CodingAgent = OpenAICodingAgent` is a backward-compat alias in `codex.py`.

Progress is sent as private utterances to the floor manager during the loop. Final result + `yieldFloor` are bundled in one envelope. Output files are saved to `result/<session>/code/`.

### Showrunner / Orchestrator Pattern

When `SHOWRUNNER_DRIVEN` policy is active, one agent acts as orchestrator. It receives the floor, emits structured directives in its utterance text:

| Directive | Action |
|-----------|--------|
| `[ASSIGN AgentName]: task` | Grant floor to that agent with a task |
| `[ACCEPT]` | Accept the last worker's output into manuscript |
| `[REJECT AgentName]: reason` | Ask agent to redo |
| `[KICK AgentName]` | Remove agent from session |
| `[SPAWN spec]` | Dynamically create a new agent |
| `[SKIP AgentName]: reason` | Record skip in manuscript, return floor to orchestrator |
| `[REMEMBER category]: content` | Write to in-session `MemoryStore` from worker output |
| `[BREAKOUT policy=<p> max_rounds=<n> topic=<t>]` | Spin up an isolated sub-floor; follow with `[BREAKOUT_AGENT]` lines |
| `[BREAKOUT_AGENT -provider <p> -name <n> -system <s>]` | Define participants for the preceding `[BREAKOUT]` |
| `[CODING_SESSION policy=<p> max_rounds=<n> topic=<t>]` | Launch a multi-agent coding sub-floor; follow with `[CODING_AGENT]` lines |
| `[CODING_AGENT -provider <p> -name <n> -system <s> -timeout <s>]` | Define participants for the preceding `[CODING_SESSION]` |
| `[TASK_COMPLETE]` | End the session, output manuscript |

The FloorManager parses these directives in `_handle_orchestrator_directives()` and accumulates accepted outputs into a shared manuscript.

**Media auto-accept**: When a media agent (image/video/audio/3d) produces output in `showrunner_driven` mode, the FloorManager auto-accepts it (appends to manuscript, returns floor to orchestrator) without waiting for `[ACCEPT]`. The orchestrator receives `[auto-accepted {type} output]: {text}` in its context so it doesn't re-issue the same `[ASSIGN]`.

**Remote agent cascade prevention**: `RemoteOFPAgent._should_respond()` blocks responses to other remote agents (`remote-` in sender URI) to prevent exponential cascade loops. It also filters floor manager messages to only react to `[DIRECTIVE for <name>]` directives.

### Agent Library (@slug syntax)

`agents/` at the repo root is a library of 200+ agent personas across 24 domains. Each is a `SOUL.md` file — a structured system prompt — addressable by `@category/agent-name` slug.

```bash
# Use a persona as the system prompt
-provider anthropic -name Alice -system @development/code-reviewer
anthropic:Alice:@creative/brand-designer     # colon format also works

ofp-playground agents     # list all available slugs
```

Loaded via [`src/ofp_playground/agents/library.py`](src/ofp_playground/agents/library.py), cached per process. Full documentation, category breakdown, and how to add new personas: [`docs/agents-library.md`](docs/agents-library.md).

**`@development/coding-agent`** is auto-loaded by `BaseCodingAgent` when no synopsis is provided. Other key coding personas: `@development/code-reviewer`, `@development/bug-hunter`, `@development/tdd-expert`, `@development/technical-planner`, `@development/tech-lead`, `@development/test-writer`, `@development/qa-tester`, `@development/pr-merger`.

### Artifact Store

In SHOWRUNNER_DRIVEN pipelines, each accepted phase output is persisted to `result/<session>/phases/` as a Markdown file with YAML frontmatter (`ArtifactStore` in `memory/artifact_store.py`). This avoids injecting the full manuscript into every directive (which causes token overflow).

The orchestrator and workers receive a compact artifact index in their context and can call `read_artifact(slug)` to retrieve any prior phase's full content. Workers should call `read_artifact` at the start of their turn to access the outputs they depend on. Accepted outputs are also still accumulated in `_manuscript`, but the index is the preferred reference for coding agents.

**Auto-generated slugs:**
- **`breakout-beat-N`** — after each breakout session completes, the full transcript is automatically persisted under this slug so agents can call `read_artifact('breakout-beat-1')` etc. on retry without needing the ephemeral `_pending_breakout_file`.
- **`character-memory-<name>`** — when an agent outputs blocks delimited by `=== CHARACTER MEMORY: Name ===` / `=== END ===`, `FloorManager._save_character_memory_blocks()` automatically splits them into one artifact per character. Slug is `character-memory-` + slugified name (e.g. `character-memory-raphael`). These are updated every beat and the index always shows only the latest version per slug (deduplication: last write wins).

**HF text agent workaround**: HuggingFace text agents have no tool support. If a task string contains `read_artifact('slug')` calls, `FloorManager._expand_artifact_refs_in_task()` expands them inline before delivery so HF agents receive the actual content.

**Adding new tools**: Define tool dicts in Anthropic format (see `memory/artifact_tools.py` or `memory/tools.py` for examples). Use the provider converters from `agents/llm/spawn_tools.py` — `to_openai_tools(tools)`, `to_google_tools(tools)`, `to_hf_tools(tools)` — to adapt them for other provider APIs. No separate per-provider definitions needed.

### Coding Sessions

An orchestrator can launch a multi-agent coding sub-floor via `[CODING_SESSION]` directives (or `create_coding_session` tool). Key details:

- Agents operate in a shared sandbox directory (`result/<session>/sandbox/`) and are given file-system tools: `list_workspace`, `read_file`, `write_file`, `edit_file`, `update_todo`.
- Each agent's turn begins with a workspace snapshot (file listing + TODO state) injected into context.
- Ends when `max_rounds` is reached or any agent emits `[CODING_COMPLETE]`.
- Results include the full file manifest; output is saved under `result/<session>/sandbox/`.
- Hard timeout: 1800 seconds. One nesting level permitted (no coding sessions inside coding sessions).

### Breakout Sessions

An orchestrator can spin up an isolated sub-floor conversation via the `create_breakout_session` tool. Key details:

- Each breakout gets its own `MessageBus` and `BreakoutFloorManager` (URI: `tag:ofp-playground.local,2025:breakout-floor-manager`).
- Ends when `max_rounds` utterances are exchanged or any agent emits `[BREAKOUT_COMPLETE]`.
- Results are saved under `result/<session>/breakout/` as structured Markdown and a compact notification is returned to the parent orchestrator.
- Hard timeout: 300 seconds. Only one nesting level is permitted.

### Conversation Trace & Timeline

Every session automatically records a full OFP event trace via `trace/`:

- **[EventCollector](src/ofp_playground/trace/collector.py)** — attached to the `MessageBus` via `set_collector()`; normalizes each routed envelope into a `TraceEvent` (sender, recipients, event type, summary, directive detection, media type, breakout scope).
- **[TraceEvent](src/ofp_playground/trace/model.py)** — frozen dataclass with routing metadata: `sender_uri/name`, `route_uris/names`, `explicit_target_uris`, `is_private`, `is_broadcast`, `directive`, `breakout_id`, and the full `envelope_json`.
- **[render_trace_html](src/ofp_playground/trace/renderer.py)** — generates a self-contained D3-based interactive HTML timeline. Written to `result/<session>/trace.html` automatically at session end by `FloorManager._output_trace_timeline()`.

The FloorManager initialises the collector at startup and registers each joining agent. The MessageBus calls `collector.record()` after every routing decision. No CLI flag needed — trace is always on.

### In-Session Memory Store

`memory/store.py` provides an ephemeral `MemoryStore` scoped to one conversation. It is not persisted to disk. Categories (priority order for summaries): `goals`, `tasks`, `breakouts`, `decisions`, `lessons`, `agent_profiles`, `preferences`. The store is injected into LLM system prompts each turn via `get_summary()`.

### Web UI (Gradio)

Use the `ofp-playground web` subcommand (not a flag on `start`) to launch the Gradio interface. Extra flags: `--host`, `--port` (default 7860), `--share`. The `WebHumanAgent` replaces `HumanAgent`; it accepts user input from the browser and streams agent utterances back as chat bubbles. Media (images/video/audio) renders inline.

### Canvas (Visual Floor Builder)

`ofp-playground canvas` launches a FastAPI backend + React/TypeScript frontend at `http://localhost:8765`. Users drag-and-drop `FloorNode` and `AgentNode` nodes, then click Start — the backend translates the graph into a live OFP session.

**Key files:**
- [`canvas/backend/main.py`](canvas/backend/main.py) — FastAPI app factory (`build_app()`); REST endpoints: `POST /session/start`, `DELETE /session/stop`, `GET /session/status`, `POST /session/agent/add`, `DELETE /session/agent/{name}`, `POST /session/message`, `GET /trace/live`, `GET /media/{path}`. WebSocket at `/ws/{session_id}`.
- [`canvas/backend/session_bridge.py`](canvas/backend/session_bridge.py) — `SessionBridge` acts as a `MessageBus` collector; serializes OFP envelopes into JSON events (`utterance`, `floor_grant`, `floor_revoke`, `floor_request`) and fans them out to connected WebSocket clients. Late-joining clients receive the full `event_log` replay.
- [`canvas/frontend/src/`](canvas/frontend/src/) — React/TypeScript frontend (Vite build). Pre-built dist is checked in under `canvas/frontend/dist/` and served by FastAPI's `StaticFiles`.

**`StartRequest` shape** — `nodes` array with `FloorNode` (holds `policy`, `topic`, `maxTurns`, `noHuman`) and `AgentNode` items (holds `provider`, `name`, `model`, `systemPrompt`, `slug`); `edges` array (currently informational only).

**Live trace**: `GET /trace/live` generates the D3 timeline HTML with a WebSocket injected (`live_session_id`) so it auto-updates in real time. The `render_trace_html()` function in `trace/renderer.py` now accepts an optional `live_session_id` parameter for this purpose.

**Parser fix**: `_parse_agent_spec()` in `cli.py` now ignores flag-like tokens inside `[...]` brackets, so orchestrator mission text containing `[BREAKOUT_AGENT -provider ...]` examples is no longer mis-parsed as agent spec flags.

### Remote Agents

Pass `--remote <slug-or-url>` (repeatable) to proxy an external HTTP OFP endpoint as a participant. Known slugs: `polly` (echo), `arxiv`, `github`, `sec`, `web-search`, `wikipedia`/`wiki`, `stella` (NASA), `verity` (hallucination detector), `profanity`. `RemoteOFPAgent._should_respond()` blocks responses to other remote agents to prevent cascade loops and only reacts to `[DIRECTIVE for <name>]` messages from the floor manager.

### Configuration & API Keys

API keys are resolved from `.env` (project root) or `~/.ofp-playground/config.toml`. Relevant vars: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `HF_API_KEY`.

### Agent Spec Formats (CLI)

Two formats are accepted by `--agent`:

```bash
# Colon format
type:name
type:name:description
type:subtype:name:description:model

# Flag format
-provider anthropic -name Alice -system "You are..." -model claude-opus-4-6 -timeout 30 -max-retries 2
```

`type` maps to a provider (`anthropic`, `openai`, `google`, `huggingface`) and optionally a task subtype. Full subtype list (from `cli.py` `TASK_SUBTYPES`):

```
text-generation, text-to-image, image-to-text, text-to-video, text-to-music,
image-text-to-text, image-classification, object-detection, image-segmentation,
token-classification, text-classification, summarization,
showrunner, orchestrator, code-generation
```

### URIs (constants)

- Floor manager: `tag:ofp-playground.local,2025:floor-manager`
- Human agents: `tag:ofp-playground.local,2025:human-{name}`
- LLM agents: `tag:ofp-playground.local,2025:llm-{name}`

### In-conversation slash commands

`/help`, `/agents`, `/floor`, `/history`, `/spawn <spec>`, `/kick <name>`, `/quit`

### Session Output

All generated media is saved under a session-scoped directory:

```
result/<timestamp>_<session-id>/
├── images/     — generated images
├── videos/     — generated videos
├── music/      — generated audio
├── code/       — generated code files (CodingAgents)
├── breakout/   — breakout session transcripts
├── phases/     — ArtifactStore phase Markdown files (SHOWRUNNER_DRIVEN)
├── sandbox/    — shared workspace for coding sessions
├── trace.html  — interactive D3 timeline (auto-generated)
├── manuscript.txt
└── memory.json
```

The `examples/` directory contains runnable reference pipelines:

| Script | What it demonstrates |
|--------|---------------------|
| `example_novel.sh` | 20-phase illustrated novel (showrunner + breakouts + character memory + image gen) |
| `example_platformer.sh` | 3D browser game via multi-agent coding session |
| `example_song_production.sh` | Music production pipeline (lyrics → music gen) |
| `free_for_all_brainstorm.sh` | FREE_FOR_ALL policy, relevance filter in action |
| `moderated_investment_committee.sh` | MODERATED policy |
| `round_robin_novel.sh` | ROUND_ROBIN creative writing |
| `sequential_code_review.sh` | SEQUENTIAL code review |
| `breakout_code_review.sh` | Orchestrator spawning breakout review sessions |
| `showcase.sh` / `showcase_web.sh` | Feature showcase (CLI and Gradio) |

### Artifact Model

`models/artifact.py` defines `Utterance` and `ArtifactFeature` — the internal typed representation of OFP `dialogEvent.features`. Every utterance has a mandatory `text` feature (verbalizable fallback) plus optional extended keys (`image`, `video`, `3d`, `audio`). Factory methods: `Utterance.from_text()`, `.from_image()`, `.from_video()`.
