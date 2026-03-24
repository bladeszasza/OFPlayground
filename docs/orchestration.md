# Orchestration Patterns

This document covers the advanced orchestration features available in SHOWRUNNER_DRIVEN mode, including the directive protocol, breakout sessions, session memory, and production pipeline patterns.

## SHOWRUNNER_DRIVEN Pipeline

In SHOWRUNNER_DRIVEN mode, one agent (the **orchestrator**) controls the entire production. Workers only speak when explicitly assigned.

### Pipeline Lifecycle

```
1. Session starts → orchestrator gets floor
2. Orchestrator emits directives → FloorManager parses and executes
3. Workers produce output → floor returns to orchestrator
4. Orchestrator accepts/rejects/reassigns
5. [TASK_COMPLETE] → manuscript output + session end
```

### Directive Protocol

All directives are embedded inline in the orchestrator's utterance text. FloorManager parses them via `_handle_orchestrator_directives()`.

#### [ASSIGN Name]: task

Grant the floor to a named agent with a task description.

```
[ASSIGN Writer]: Synthesize the breakout room output into a polished script
```

FloorManager actions:
1. Resolve `Writer` → URI via `_resolve_agent_uri_by_name()`
2. Build directive text: `[DIRECTIVE for Writer]: task description`
3. Inject manuscript context: `--- STORY SO FAR ---\n{accumulated text}`
4. Inject breakout results if pending
5. Inject session memory summary
6. Send as private utterance to Writer
7. Grant floor to Writer

#### [ACCEPT]

Accept the last worker's output into the manuscript.

```
[ACCEPT]
```

FloorManager actions:
1. Append `_last_worker_text` to `_manuscript` list
2. Log acceptance
3. Floor remains with orchestrator

#### [REJECT Name]: reason

Ask an agent to redo their work with feedback.

```
[REJECT Writer]: needs more dialogue and character voice consistency
```

FloorManager actions:
1. Resolve agent URI
2. Send rejection reason as directive
3. Re-grant floor to rejected agent

#### [KICK Name]

Remove an agent from the session permanently.

```
[KICK SlowAgent]
```

FloorManager actions:
1. Resolve agent URI
2. Send `UninviteEvent`
3. Unregister from floor + bus

#### [SPAWN spec]

Dynamically create a new agent. Uses the same spec format as `--agent` CLI flag.

```
[SPAWN -provider openai -name Editor -system "You are a meticulous editor"]
```

FloorManager actions:
1. Call `_spawn_callback(spec_str)` (set by CLI)
2. CLI's `_floor_spawn_callback` parses spec → creates agent → registers
3. Duplicate detection: checks manifests and floor registry before spawning

#### [SKIP Name]: reason

Record a skip in the manuscript and move on (e.g., when an agent is unavailable).

```
[SKIP Painter]: HuggingFace API returned 503 after all retries
```

FloorManager actions:
1. Append skip note to manuscript
2. Floor remains with orchestrator

#### [BREAKOUT ...]

Spin up a temporary sub-floor session. See [Breakout Sessions](#breakout-sessions).

```
[BREAKOUT policy=round_robin max_rounds=12 topic=Scene development]
[BREAKOUT_AGENT -provider anthropic -name PlotWriter -system "Write plot structure"]
[BREAKOUT_AGENT -provider openai -name DialogueWriter -system "Write dialogue"]
```

#### [TASK_COMPLETE]

End the session. FloorManager outputs the manuscript and stops.

```
[TASK_COMPLETE]
```

FloorManager actions:
1. Call `_output_manuscript()` → saves to `result/<session>/manuscript.txt`
2. Save memory dump → `result/<session>/memory.json`
3. Render manuscript to terminal
4. Call `stop()` → set stop event
5. All agent tasks are cancelled

---

## Media Auto-Accept

When a media agent (image, video, audio, 3D) produces output in SHOWRUNNER_DRIVEN mode, the FloorManager handles it automatically:

1. Media agent speaks (utterance with image/video/audio feature)
2. FloorManager detects media content in envelope
3. Output text is appended to manuscript
4. Floor returns to orchestrator immediately
5. Orchestrator receives context note: `[auto-accepted image output]: Storyboard panel 1...`

This prevents the orchestrator from needing to explicitly `[ACCEPT]` media outputs and avoids re-issuing the same `[ASSIGN]`.

**Detection**: FloorManager checks the utterance text for media path patterns (`ofp-images/`, `ofp-videos/`, `ofp-music/`, `result/`) and the envelope features for non-text content.

---

## Breakout Sessions

Breakout sessions are self-contained sub-floor conversations that run temporarily within the main session. They allow focused, multi-agent discussions on a specific topic.

### Architecture

```
Main Floor (SHOWRUNNER_DRIVEN)
└── Breakout Session (any policy, typically ROUND_ROBIN)
    ├── Isolated MessageBus
    ├── BreakoutFloorManager (lightweight)
    ├── Temporary agents (fresh instances)
    └── Returns BreakoutResult to parent
```

### Creating a Breakout

The orchestrator can create breakouts in two ways:

**1. Tool calling** (preferred — structured):
```python
create_breakout_session(
    topic="Develop the comedy scene structure",
    policy="round_robin",
    max_rounds=12,
    agents=[
        {"name": "PlotWriter", "provider": "anthropic", "system": "..."},
        {"name": "DialogueWriter", "provider": "openai", "system": "..."},
    ]
)
```

**2. Text directives** (fallback):
```
[BREAKOUT policy=round_robin max_rounds=12 topic=Scene development]
[BREAKOUT_AGENT -provider anthropic -name PlotWriter -system "Write plot"]
[BREAKOUT_AGENT -provider openai -name DialogueWriter -system "Write dialogue"]
```

### Breakout Lifecycle

1. Orchestrator requests breakout (tool call or directive)
2. FloorManager calls `_breakout_callback(topic, policy, max_rounds, agent_specs)`
3. CLI creates temporary agents with `_create_breakout_agent()`
4. `run_breakout_session()` creates:
   - Isolated `MessageBus`
   - `BreakoutFloorManager` with bounded rounds
   - Seeds topic as opening utterance
5. Agents discuss for `max_rounds` or until `[BREAKOUT_COMPLETE]`
6. Returns `BreakoutResult(history, topic, agent_names, round_count)`
7. Full transcript saved to `result/<session>/breakout/`
8. Compact summary (~200 words) injected into orchestrator's next `[ASSIGN]` context
9. Breakout recorded in MemoryStore under `tasks` category

### Constraints

- **One level deep** — breakouts cannot spawn other breakouts
- **Hard timeout**: 300 seconds
- **Max rounds**: 2–20 (clamped)
- **Fresh agents**: New temporary instances, not reused from main floor

---

## Session Memory

### MemoryStore

Ephemeral key-value memory that persists for the entire conversation session.

**Categories** (priority-ordered for summary generation):

| Category | Priority | Description |
|----------|----------|-------------|
| `goals` | 1 (highest) | Original mission/goal text |
| `tasks` | 2 | Active task tracking |
| `decisions` | 3 | Key decisions made |
| `lessons` | 4 | Lessons learned during session |
| `agent_profiles` | 5 | Agent capability/behavior notes |
| `preferences` | 6 (lowest) | Style/format preferences |

### Writing to Memory

**Tool calling** (orchestrator agents):
```python
store_memory(category="decisions", key="art_style", content="Use flat animation style")
```

**Text directives** (any agent):
```
[REMEMBER decisions]: Art style should be flat animation, bold outlines
[REMEMBER lessons:api_failures]: HF FLUX returns 503 during peak hours
```

### Memory Injection

Memory summaries are automatically injected into:

1. **Orchestrator system prompts** — full summary on every turn
2. **Worker directive context** — `--- SESSION MEMORY ---` section
3. **Director/ShowRunner prompts** — summary appended to base system prompt

Priority-ordered: Goals first, then tasks, then decisions, down to preferences. Truncated to `max_chars` (default 600).

### Memory Persistence

- Stored as `result/<session>/memory.json` alongside manuscript
- Loaded from `MemoryStore.to_dict()` → JSON serialization
- **Not** persisted across sessions (ephemeral by design)

---

## Production Pipeline Example

The `examples/showcase.sh` demonstrates a full 8-step pipeline:

```
Step 1: [BREAKOUT] → Writers Room (3 agents, 23 rounds)
Step 2: [ASSIGN Writer] → Harvest & polish into final script
Step 3: [ASSIGN Painter] → Shot 1 (establishing)
Step 4: [ASSIGN Painter] → Shot 2 (mid-scene reaction)
Step 5: [ASSIGN Painter] → Shot 3 (closing gag)
Step 6: [ASSIGN Composer] → Music cue
Step 7: [ASSIGN WebShowcase] → HTML showcase page
Step 8: [TASK_COMPLETE]
```

### Agent Lineup

| Agent | Provider | Role |
|-------|----------|------|
| Director | Anthropic (orchestrator) | Drives the 8-step pipeline |
| Writer | OpenAI | Synthesizes Writers Room output |
| Painter | HuggingFace FLUX | Generates 3 storyboard images |
| Composer | Google Lyria | Creates comedic music cue |
| WebShowcase | OpenAI | Generates HTML showcase page |

### Writers Room Breakout

| Agent | Provider | Role |
|-------|----------|------|
| PlotWriter | Anthropic | Scene structure, beats, timing |
| DialogueWriter | OpenAI | Character voice, dialogue lines |
| GagWriter | Google | Cutaway gags, visual comedy |

### Orchestrator Resilience

The orchestrator follows these rules when agents fail:

| Attempt | Action |
|---------|--------|
| 1st failure | `[REJECT AgentName]: clearer instructions` |
| 2nd failure | `[SPAWN]` replacement agent with different provider |
| 3rd failure | `[SKIP AgentName]: reason` — move on |

---

## Tool Definitions

### Spawn Tools (`spawn_tools.py`)

Enabled for all orchestrator agents:

| Tool | Description |
|------|-------------|
| `spawn_orchestrator` | Create a new orchestrator agent |
| `spawn_text_agent` | Create a text-generation agent |
| `spawn_image_agent` | Create an image-generation agent |
| `spawn_video_agent` | Create a video-generation agent |

### Memory Tools (`memory/tools.py`)

| Tool | Description |
|------|-------------|
| `store_memory` | Record a decision/task/lesson/etc. |
| `recall_memory` | Retrieve entries by category/key |

### Breakout Tools (`breakout_tools.py`)

| Tool | Description |
|------|-------------|
| `create_breakout_session` | Spin up a temporary sub-floor discussion |

Parameters: `topic`, `policy`, `max_rounds`, `agents[]` (each with `name`, `system`, `provider`, optional `model`).
