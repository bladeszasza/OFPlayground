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

FloorManager actions (`_dispatch_assignment` / `_build_directive`):
1. Resolve `Writer` → URI via `_resolve_agent_uri_by_name()`
2. Collect the task. It may span several lines, up to the next directive line. Expand `read_artifact('slug')` references inline.
3. Build `[DIRECTIVE for Writer]: task`. For **text** workers, append the compact phase-artifact index, the sandbox note, the session-memory summary and, once, the pending breakout transcript. **Media** generators and **remote** agents get the bare task, because those blocks would end up inside the image/music prompt.
4. Send the directive privately, **then** grant the floor, so the worker reads its task before it speaks.

The worker's reply returns the floor to the orchestrator. If the worker yields **without** replying (error, timeout, empty answer, drained), the orchestrator still gets the floor back, plus `[SYSTEM] Writer yielded the floor without delivering any output…`. Utterances from anyone other than the assigned worker, such as a human interjection or a remote agent answering a broadcast, are kept as context and never count as the delivery.

#### [ASSIGN_PARALLEL A, B, …]: task

Fan the same task out to several agents at once. Each gets a normal `[DIRECTIVE for X]` (plus a note naming its peers) and a grant. The orchestrator regains the floor only after **every** member delivered or failed. `[ACCEPT]` then stores all answers, each labelled `[Name]:`.

```
[ASSIGN_PARALLEL Critic, Editor]: Review chapter 3 and list the three biggest problems.
```

#### [ACCEPT]

Accept the last worker's output into the manuscript and phase artifact store.

```
[ACCEPT]
```

FloorManager actions:
1. Append `_last_worker_text` to `_manuscript` list
2. Save to `ArtifactStore` → persisted as `result/<session>/phases/NN_<slug>.md`
3. Log acceptance
4. Floor remains with orchestrator

#### [REJECT Name]: reason

Ask an agent to redo their work with feedback.

```
[REJECT Writer]: needs more dialogue and character voice consistency
```

FloorManager actions:
1. Resolve agent URI and mark it as the assigned worker; the rejected draft can no longer be `[ACCEPT]`ed.
2. Send `Revision requested — reason` **and** `Original task: …` as the directive. Media agents get the original prompt first and the notes after it, because they build their prompt from the first line.
3. Grant the floor, after the directive. Previously the grant went first and the agent regenerated before it saw the reason.

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

#### [REMEMBER category]: content

Write a note to the in-session `MemoryStore` directly from an agent's utterance text. The `[REMEMBER]` line is stripped from the manuscript before acceptance.

```
[REMEMBER decisions]: Use flat animation style with bold outlines
[REMEMBER lessons:api_failures]: HF FLUX returns 503 during peak hours
```

Categories: `goals`, `tasks`, `decisions`, `lessons`, `agent_profiles`, `preferences`.

#### [BREAKOUT ...]

Spin up a temporary sub-floor session. See [Breakout Sessions](#breakout-sessions).

```
[BREAKOUT policy=round_robin max_rounds=12 topic=Scene development]
[BREAKOUT_AGENT -provider anthropic -name PlotWriter -system "Write plot structure"]
[BREAKOUT_AGENT -provider openai -name DialogueWriter -system "Write dialogue"]
```

#### [CODING_SESSION ...] / [CODING_AGENT ...]

Launch a multi-agent coding sub-floor with shared file-system access. See [Coding Sessions](#coding-sessions).

```
[CODING_SESSION policy=round_robin max_rounds=6 topic=Refine the Three.js platformer]
[CODING_AGENT -provider anthropic -name DevAlpha -system @development/threejs-developer -timeout 1200]
[CODING_AGENT -provider openai -name DevBeta -system @development/geometry-builder -timeout 1200]
```

Workers emit `[CODING_COMPLETE]` when the project is playable/complete, ending the session early.

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

The `examples/showcase.sh` demonstrates a full 10-chapter illustrated story pipeline (topic is passed as `$1`):

```
STEP 0: create_breakout_session → Story Brainstorm (6 agents, 16 rounds, free_for_all)
        → Director extracts arc, characters, world

For each of 10 chapters:
  STEP A: [ASSIGN StoryWriter] → Write chapter (text + scene description)
  STEP B: [ACCEPT] + create_breakout_session → Peer review (2 agents, 2 rounds, round_robin)
  STEP B½: create_breakout_session → Cutscene (2 dark-comedy agents, optional, ≥3 chapters)
  STEP C: [REJECT StoryWriter] only if both reviewers say REVISE
  STEP D: [ASSIGN NanoBananPainter] → Chapter illustration (auto-accepted)
  STEP E: [ASSIGN ChapterBuilder] → HTML chapter page

After chapter 10:
  [ASSIGN Composer] → 30-second loopable ambient music (auto-accepted)
  [ASSIGN IndexBuilder] → master index.html (cover + TOC + character cards + music player)
  [ACCEPT] → [TASK_COMPLETE]
```

### Agent Lineup

| Agent | Provider | Role |
|-------|----------|------|
| Director | Anthropic (orchestrator) | Drives the full pipeline, spawns all breakouts |
| StoryWriter | Anthropic (Sonnet) | Writes one chapter per assignment |
| NanoBananPainter | HuggingFace (text-to-image) | Illustrates each chapter |
| Composer | Google Lyria (text-to-music) | Ambient loopable background music |
| ChapterBuilder | OpenAI (code-generation) | HTML chapter pages |
| IndexBuilder | OpenAI (code-generation) | Master index.html |

### Breakout Sessions

**Story Brainstorm** (STEP 0, `free_for_all`, 16 rounds):

| Agent | Role |
|-------|------|
| YouthfulVoice | Emotional core — instinct, wonder, what the story must feel like |
| HeartVoice | Narrative will — spine, stakes, what cannot be cut |
| CriticalVoice | Editor/ironist — sharp alternatives, high standards |
| DarkHumor | Absurdist undercurrent — adult humour hiding in the warmth |
| EmotionalDepth | Subtext excavation — what the story means below the waterline |
| NarrativeArchitect | Structure — arc shape, chapter payoffs, escalation curve |

**Peer Review** (per chapter, `round_robin`, 2 rounds):

| Agent | Role |
|-------|------|
| LiteraryReviewer | Children's book editor — character consistency, resonance |
| ChildExperience | Child development specialist — vocabulary, engagement |

**Cutscene** (optional per chapter, `round_robin`, 2 rounds):

| Agent | Role |
|-------|------|
| PeterGriffin | Family Guy-style dark-comedy cutaway setup |
| StewieGriffin | Escalates with twist/callback |

### Orchestrator Resilience

The orchestrator follows these rules when agents fail:

| Attempt | Action |
|---------|--------|
| 1st failure | `[REJECT AgentName]: clearer instructions` |
| 2nd failure | `[SPAWN]` replacement agent with different provider |
| 3rd failure | `[SKIP AgentName]: reason` — move on |

---

---

## Phase Artifact Store

### Overview

When an orchestrator issues `[ACCEPT]`, the accepted text is persisted as a Markdown file with YAML frontmatter under `result/<session>/phases/`. This eliminates the context-overflow problem that occurs when the full manuscript is injected into every subsequent directive.

```
result/<session>/phases/
├── 01_asset-manifest.md     ← Phase 1 output (AssetDirector)
├── 02_char-design.md        ← Phase 2 output (CharDesigner)
├── 03_geometry-code.md      ← Phase 3 output (GeomBuilder)
└── ...
```

Each file has frontmatter:
```yaml
---
phase: 3
slug: geometry-code
agent: GeomBuilder
timestamp: 2026-04-17T14:22:01
depends_on: [char-design]
tokens_approx: 5200
summary: "9 buildXxx() Three.js functions for all game assets"
---
```

### Accessing Artifacts

The orchestrator and workers receive a compact index in their context:

```
--- PHASE ARTIFACTS (3 completed) ---
  01_asset-manifest.md         | AssetDirector   | 9 assets defined for theme...
  02_char-design.md            | CharDesigner    | Parts tables for all 9 assets
  03_geometry-code.md          | GeomBuilder     | 9 buildXxx() Three.js functions (~5200 tok)
--- END PHASE ARTIFACTS ---
Use read_artifact(slug) to retrieve the full content of any phase.
```

Workers call `read_artifact(slug)` (injected as a tool) to fetch the full content of any prior phase — by slug substring, phase number, or agent name.

---

## Coding Sessions

### Overview

A coding session is a self-contained round-robin sub-floor where agents collaboratively build a project by reading, writing, and editing files in a **shared local sandbox directory** (`result/<session>/sandbox/`).

Unlike regular agents, coding session agents receive injected file-system tools every turn:

| Tool | Description |
|------|-------------|
| `list_workspace` | List all files and their sizes in the sandbox |
| `read_file` | Read a file from the sandbox |
| `write_file` | Write or overwrite a file in the sandbox |
| `edit_file` | Replace a specific string within a file |
| `update_todo` | Update the shared TODO list |

At the start of each turn, the agent receives a workspace snapshot (file listing + TODO state) in its context.

### Creating a Coding Session

**Tool calling** (preferred for Anthropic/OpenAI/Google orchestrators):
```python
create_coding_session(
    topic="Refine the Three.js platformer game",
    policy="round_robin",
    max_rounds=6,
    agents=[
        {"name": "DevAlpha", "provider": "anthropic", "system": "...", "timeout": 1200},
        {"name": "DevBeta", "provider": "openai", "system": "...", "timeout": 1200},
    ]
)
```

**Text directives** (fallback, works with all orchestrators including HuggingFace):
```
[CODING_SESSION policy=round_robin max_rounds=6 topic=Refine the Three.js platformer]
[CODING_AGENT -provider anthropic -name DevAlpha -system @development/threejs-developer -timeout 1200]
[CODING_AGENT -provider openai -name DevBeta -system @development/geometry-builder -timeout 1200]
```

### Coding Session Lifecycle

1. Orchestrator triggers session (tool call or directive)
2. FloorManager creates agents with file tools injected
3. Phase artifacts and session memory are mirrored into the sandbox as context files
4. Agents take turns: read workspace → write/edit files → yield
5. Session ends at `max_rounds` or when any agent emits `[CODING_COMPLETE]`
6. Full file manifest + project structure returned to orchestrator

### Constraints

- **Provider support**: `openai`, `anthropic`, `google` only — `hf:code-generation` is not supported
- **One level deep**: coding sessions cannot be nested
- **Hard timeout**: 1800 seconds
- **Round range**: 2–50 (default 16)

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

### Coding Session Tools (`coding_session_tools.py`)

| Tool | Description |
|------|-------------|
| `create_coding_session` | Launch a multi-agent coding sub-floor with shared file tools |

Parameters: `topic`, `policy`, `max_rounds`, `agents[]` (each with `name`, `system`, `provider`, optional `model`, optional `timeout`).

Not available for HuggingFace orchestrators — use `[CODING_SESSION]` text directives instead (though HF code-generation agents are unsupported; use Anthropic/OpenAI/Google providers for coding agents).

### Artifact Tools (`memory/artifact_tools.py`)

| Tool | Description |
|------|-------------|
| `read_artifact` | Retrieve full content of a phase artifact by slug, phase number, or agent name |
| `list_artifacts` | List all saved phase artifacts with summaries |

Available to all agents in SHOWRUNNER_DRIVEN sessions. The artifact index is also injected automatically into each directive context.
