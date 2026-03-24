# Architecture Overview

## System Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                       OFP PLAYGROUND                            │
└─────────────────────────────────────────────────────────────────┘

  HUMAN INPUT                                WEB UI
      ↓                                       ↓
  HumanAgent                             WebHumanAgent
  (stdin/stdout)                         (Gradio queues)
      │                                       │
      └──────────────────┬────────────────────┘
                         │
            ┌────────────▼────────────┐
            │   MessageBus (Async)    │
            │  - register(uri, queue) │
            │  - send(envelope)       │
            │  - send_private(...)    │
            └────────────┬────────────┘
                         │
    ┌────────────────────┼──────────────────────┬───────────────┐
    │                    │                      │               │
FloorManager        LLM Agents           Remote Agents    Media Agents
 - Coordinates      - Anthropic          - HTTP proxy      - ImageAgent
 - Routes           - OpenAI             - Known slugs     - VideoAgent
 - Tracks floor     - Google               (arxiv, wiki)   - MusicAgent
 - Parses           - HuggingFace                          - WebPageAgent
   directives       - Orchestrators
 - Manages rounds   - Director
 - Memory store     - ShowRunner
 - Breakout
```

## Core Components

### MessageBus (`src/ofp_playground/bus/message_bus.py`)

Pure async message router. All agents register their `asyncio.Queue` on the bus. Routing rules:

| Scenario | Behavior |
|----------|----------|
| Envelope has `event.to.speakerUri` | Deliver to target agent **+ floor manager** |
| Envelope has no `to` field | Broadcast to **all** agents except the sender |
| Floor manager is sender | Do **not** echo back to floor manager |

The floor manager **always** receives a copy of every envelope (unless it sent it).

```python
FLOOR_MANAGER_URI = "tag:ofp-playground.local,2025:floor-manager"
```

### FloorManager (`src/ofp_playground/floor/manager.py`)

Central coordinator. Responsibilities:

1. **Floor control** — delegates to `FloorController` (policy engine)
2. **Envelope dispatch** — receives every envelope, handles each event type
3. **Conversation history** — `ConversationHistory` instance (last 100 entries)
4. **Agent lifecycle** — register/unregister, invite/uninvite
5. **Orchestrator parsing** — `[ASSIGN]`, `[ACCEPT]`, `[REJECT]`, `[KICK]`, `[SPAWN]`, `[BREAKOUT]`, `[TASK_COMPLETE]`
6. **Memory store** — ephemeral `MemoryStore` auto-injected into system prompts
7. **Round tracking** — counts agent utterances, triggers director/showrunner grants at round boundaries
8. **Session output** — `SessionOutputManager` writes manuscripts, memory dumps

### FloorController (`src/ofp_playground/floor/policy.py`)

Implements the 5 floor policies. See [Floor Policies](floor-policies.md) for details.

### AgentRegistry (`src/ofp_playground/agents/registry.py`)

Simple in-memory registry mapping `speaker_uri → agent` instance. Used by the CLI to track spawned agents and resolve by name.

### SessionOutputManager (`src/ofp_playground/config/output.py`)

Creates a per-conversation directory under `result/`:

```
result/
└── 20260324_112523_a1b2c3d4/
    ├── images/      ← generated images (HF FLUX, OpenAI, Gemini)
    ├── videos/      ← generated videos
    ├── music/       ← generated audio (Lyria)
    ├── web/         ← HTML pages (WebPageAgent)
    ├── breakout/    ← breakout session transcripts
    ├── manuscript.txt
    └── memory.json
```

## Message Flow: Detailed

### 1. Agent sends an utterance

```
Agent.send_envelope(envelope)
  → MessageBus.send(envelope)
    → FloorManager.queue (always)
    → If envelope.event.to.speakerUri → target agent queue only
    → Else → all agent queues except sender
```

### 2. FloorManager processes envelope

```
FloorManager._process_envelope(envelope)
  ├── UtteranceEvent
  │   ├── Add to ConversationHistory
  │   ├── Parse [REMEMBER] directives → MemoryStore
  │   ├── If SHOWRUNNER_DRIVEN → _handle_orchestrator_directives()
  │   ├── Track round (text agents spoken)
  │   └── If round complete → grant to director/showrunner
  ├── RequestFloorEvent → FloorController.request_floor()
  ├── YieldFloorEvent   → FloorController.yield_floor()
  ├── PublishManifestsEvent → store manifest
  ├── InviteEvent / UninviteEvent → agent lifecycle
  └── DeclineEvent → log warning
```

### 3. Orchestrator directive flow (SHOWRUNNER_DRIVEN)

```
Orchestrator utterance "[ASSIGN Writer]: Write the script"
  → FloorManager._handle_orchestrator_directives()
    ├── Parse "[ASSIGN Writer]"
    ├── Resolve Writer URI via _resolve_agent_uri_by_name()
    ├── Send [DIRECTIVE for Writer]: ... as private utterance
    ├── Inject manuscript context ("--- STORY SO FAR ---")
    ├── Grant floor to Writer
    └── Writer generates response → yields floor
        → FloorManager re-grants to orchestrator
```

## Lifecycle

### Session startup

1. CLI parses arguments, loads `.env` and config
2. `FloorManager` created → `SessionOutputManager` initialized
3. `HumanAgent` (or `WebHumanAgent`) created if not `--no-human`
4. Agent specs parsed → `_spawn_llm_agent()` creates each agent:
   - Agent constructor called
   - `_output_dir` wired from `SessionOutputManager`
   - Name registry, manifest registry, memory store attached
   - Agent registered with floor + registry + bus
   - `InviteEvent` sent, `agent.run()` started as task
5. Topic seeded via `_seed_topic()` if `--topic` provided
6. Memory store seeded with goal
7. Orchestrator/Director granted initial floor

### Session shutdown

1. `[TASK_COMPLETE]` directive or `--max-turns` reached or Ctrl+C
2. `FloorManager.stop()` sets stop event
3. `_output_manuscript()` writes manuscript + memory JSON to session output directory
4. All agent tasks cancelled

## URI Conventions

| Entity | URI Pattern |
|--------|-------------|
| Floor Manager | `tag:ofp-playground.local,2025:floor-manager` |
| Human Agent | `tag:ofp-playground.local,2025:human-{name}` |
| LLM Agent | `tag:ofp-playground.local,2025:llm-{name}` |
| Image Agent (HF) | `tag:ofp-playground.local,2025:image-{name}` |
| Image Agent (OpenAI) | `tag:ofp-playground.local,2025:image-{name}` |
| Image Agent (Gemini) | `tag:ofp-playground.local,2025:gimage-{name}` |
| Video Agent | `tag:ofp-playground.local,2025:video-{name}` |
| Music Agent | `tag:ofp-playground.local,2025:gmusic-{name}` |
| Vision Agent (OpenAI) | `tag:ofp-playground.local,2025:vision-{name}` |
| Vision Agent (Gemini) | `tag:ofp-playground.local,2025:gvision-{name}` |
| Remote Agent | `tag:ofp-playground.local,2025:remote-{name}` |

## Resilience

### API Call Retry (`BasePlaygroundAgent._call_with_retry`)

All external API calls are wrapped with retry logic:

| Error Type | Behavior |
|-----------|----------|
| 429 (rate limit) | Retry with exponential backoff |
| 502, 503, 504 | Retry with exponential backoff |
| Timeout | Retry with exponential backoff |
| Other errors | Raise immediately (no retry) |

Backoff: `2^attempt` seconds, capped at 30 seconds.

### Orchestrator Resilience (SHOWRUNNER_DRIVEN)

| Failure Count | Action |
|--------------|--------|
| 1st failure | `[REJECT AgentName]: clearer instructions` |
| 2nd failure | `[SPAWN]` replacement with different provider |
| 3rd failure | `[SKIP AgentName]: reason` — move on |
