# Floor Policies

OFP Playground implements **5 floor control policies** that determine how agents take turns speaking. Each policy is defined in `FloorPolicy` (an enum) and executed by `FloorController`.

## Policy Summary

| Policy | Turn Taking | Best For |
|--------|-------------|----------|
| [SEQUENTIAL](#sequential) | FIFO queue — agents speak in the order they request | Panel Q&A, step-by-step reviews |
| [ROUND_ROBIN](#round_robin) | Strict rotation through all agents | Multi-chapter stories, debates |
| [MODERATED](#moderated) | Floor granted manually; requests are queued | Expert panels, code reviews |
| [FREE_FOR_ALL](#free_for_all) | Any agent can speak anytime | Brainstorming, rapid ideation |
| [SHOWRUNNER_DRIVEN](#showrunner_driven) | Orchestrator assigns tasks; workers only speak when assigned | Production pipelines, creative production |

---

## SEQUENTIAL

```bash
ofp-playground start --policy sequential --agent "anthropic:Alice:..." --agent "openai:Bob:..."
```

### Behavior

- Agents speak in the order they **request the floor** (FIFO queue).
- When an agent yields, the next agent in the queue gets the floor.
- If the queue is empty, the floor is free (next requester gets it immediately).
- The human agent (if present) requests the floor after others speak and yields after sending.

### FloorController Logic

```
request_floor(uri):
  if no current holder → grant immediately (return True)
  else → add to queue (return False)

yield_floor(uri):
  if queue not empty → pop next → grant (return next_uri)
  else → clear holder (return None)
```

### Example

```bash
bash examples/sequential_code_review.sh "def process(u): exec(u)"
```

Three specialist reviewers (Security → Performance → Style) each speak once per human turn.
See [examples/sequential_code_review.sh](../examples/sequential_code_review.sh).

### When to Use

- Turn-based discussions where order matters
- Structured Q&A sessions
- Any scenario where agents should respond one at a time without a fixed rotation

### Cross-references

- Works with `HumanAgent` — human holds floor, speaks, yields, re-queues
- Works with `DirectorAgent` — director requests floor like any other agent
- **Not compatible** with `OrchestratorAgent` (use SHOWRUNNER_DRIVEN instead)

---

## ROUND_ROBIN

```bash
ofp-playground start --policy round_robin --agent "anthropic:Alice:..." --agent "openai:Bob:..."
```

### Behavior

- Agents take turns in a **fixed rotation** order (the order they were registered).
- Each agent speaks exactly once per round.
- After all agents have spoken, the round counter increments.
- If a `DirectorAgent` is present, it speaks **first** each round (granted by FloorManager at round boundaries).
- If a `ShowRunnerAgent` is present, it speaks **last** each round (granted after all story agents have spoken).

### FloorController Logic

```
request_floor(uri):
  if uri is next in rotation and no current holder → grant (return True)
  else → queue (return False)

yield_floor(uri):
  advance rotation pointer → grant to next in rotation
```

### Round Tracking (FloorManager)

The FloorManager tracks which agents have spoken each round via `_agents_spoken_this_round`. When all text agents (excluding media, floor-manager, director, showrunner) have spoken:

1. Round counter increments
2. `_agents_spoken_this_round` resets
3. Director or ShowRunner is granted the floor for next-round instructions

### Example

```bash
bash examples/round_robin_novel.sh "a gothic horror mystery" 5
```

Five-part collaborative story (any genre): Director sets scene beats → 3 voice agents (Protagonist, Mentor, Antagonist) write → ShowRunner synthesises into canon.
See [examples/round_robin_novel.sh](../examples/round_robin_novel.sh).

### When to Use

- Multi-chapter collaborative stories (each agent writes a section)
- Structured debates (each participant gets equal airtime)
- Sequential creative workflows where all agents contribute each round

### Cross-references

- **DirectorAgent**: Speaks first, emits `[SCENE]` and per-agent `[AgentName]: instruction` directives
- **ShowRunnerAgent**: Speaks last, synthesizes all utterances into canonical `STORY SO FAR`, emits directives for next round
- `_inject_round_summary()`: FloorManager injects a summary of recent utterances into the director context to prevent divergence
- **Not compatible with** `OrchestratorAgent` (use SHOWRUNNER_DRIVEN)

---

## MODERATED

```bash
ofp-playground start --policy moderated --agent "anthropic:Alice:..." --agent "openai:Bob:..."
```

### Behavior

- Floor is granted **manually** by the moderator (in practice, the FloorManager or human).
- Agents request the floor; requests are queued.
- The moderator decides when and to whom to grant the floor.
- `grant_to(uri)` and `revoke_floor()` are the primary control methods.

### FloorController Logic

```
request_floor(uri):
  always queued (return False) — moderator must explicitly grant

grant_to(uri):
  if no current holder → grant to uri
  else → revoke current, then grant to uri

revoke_floor():
  clear current holder
```

### Example

```bash
bash examples/moderated_investment_committee.sh "NVDA — is the AI capex cycle sustainable?"
```

Four investment analysts (Macro, Fundamentals, Risk, ESG) queue their requests but only speak when you call on them.
See [examples/moderated_investment_committee.sh](../examples/moderated_investment_committee.sh).

### When to Use

- Expert panels where a moderator controls the discussion
- Code review sessions where a lead asks specific agents to comment
- Any scenario where human control over turn-taking is desired

### Cross-references

- The human agent acts as implicit moderator via `/floor` command visibility
- FloorManager can `grant_to()` programmatically for orchestrated scenarios
- Works with all agent types

---

## FREE_FOR_ALL

```bash
ofp-playground start --policy free_for_all --agent "anthropic:Alice:..." --agent "openai:Bob:..."
```

### Behavior

- **Any agent can speak at any time** — floor requests are immediately granted.
- Multiple agents may be speaking simultaneously (in practice, they're async and messages interleave).
- No queue, no rotation, no blocking.
- The human always has implicit floor access.

### FloorController Logic

```
request_floor(uri):
  grant immediately (return True)

yield_floor(uri):
  clear holder (return None) — no next-in-line concept
```

### Example

```bash
bash examples/free_for_all_brainstorm.sh "the most transformative opportunity in AI right now"
```

Four product personas (UserResearcher, PM, Designer, DevilsAdvocate) jump in whenever they have a relevant take — often 2–3 at once.
See [examples/free_for_all_brainstorm.sh](../examples/free_for_all_brainstorm.sh).

### When to Use

- Brainstorming sessions where rapid-fire ideas are valuable
- Unstructured conversations where agents self-regulate
- Relevance-filtered setups where agents only speak when they have something to say

### Cross-references

- **Relevance filter** (`BaseLLMAgent._check_relevance()`): Especially useful in FREE_FOR_ALL to prevent agents from speaking on every message. Each agent asks itself "should I respond?" before requesting the floor.
- Works with all agent types except orchestrators

---

## SHOWRUNNER_DRIVEN

```bash
ofp-playground start --policy showrunner_driven \
  --agent "anthropic:orchestrator:Director:You drive the pipeline." \
  --agent "openai:Writer:You write scripts." \
  --agent "hf:text-to-image:Painter:Generate storyboard images."
```

### Behavior

- One agent is registered as the **orchestrator** — it controls the entire session.
- Workers **never** speak unless explicitly assigned via `[ASSIGN]` directives.
- The orchestrator always gets the floor back after a worker finishes.
- Media outputs (images, video, audio) are **auto-accepted** — no explicit `[ACCEPT]` needed.
- Accepted text outputs accumulate into a shared `_manuscript`.

### Orchestrator Directives

| Directive | Action | Example |
|-----------|--------|---------|
| `[ASSIGN Name]: task` | Grant floor to agent with task description | `[ASSIGN Writer]: Write scene 1` |
| `[ACCEPT]` | Accept last worker's output into manuscript | `[ACCEPT]` |
| `[REJECT Name]: reason` | Ask agent to redo with feedback | `[REJECT Writer]: needs more dialogue` |
| `[KICK Name]` | Remove agent from session (UninviteEvent) | `[KICK SlowAgent]` |
| `[SPAWN spec]` | Create a new agent dynamically | `[SPAWN -provider openai -name Editor -system "Edit text"]` |
| `[SKIP Name]: reason` | Record skip in manuscript, move on | `[SKIP Painter]: API unavailable` |
| `[BREAKOUT ...]` | Spin up a temporary sub-floor session | See [Orchestration Patterns](orchestration.md) |
| `[TASK_COMPLETE]` | End the session, output manuscript | `[TASK_COMPLETE]` |

### Directive Flow

```
Orchestrator: "[ASSIGN Writer]: Write the opening"
  → FloorManager parses [ASSIGN Writer]
  → Resolves Writer URI
  → Sends private directive: "[DIRECTIVE for Writer]: Write the opening
        --- STORY SO FAR ---
        (accumulated manuscript)"
  → Grants floor to Writer

Writer generates response → yields floor
  → FloorManager stores last_worker_text
  → Re-grants floor to orchestrator

Orchestrator: "[ACCEPT]"
  → FloorManager appends last_worker_text to _manuscript
  → Orchestrator gets next directive turn
```

### Media Auto-Accept

When a media agent (image/video/audio) produces output in SHOWRUNNER_DRIVEN mode:
1. Output is automatically accepted (appended to manuscript)
2. Floor returns to orchestrator immediately
3. Orchestrator receives `[auto-accepted {type} output]: {text}` in context
4. This prevents the orchestrator from re-issuing the same `[ASSIGN]`

### Example

```bash
bash examples/showcase.sh
```

Ten-chapter illustrated story pipeline: story brainstorm breakout → StoryWriter → review/cutscene breakouts → NanoBananPainter → ChapterBuilder → Composer → IndexBuilder.
See [examples/showcase.sh](../examples/showcase.sh).

### When to Use

- Multi-step production pipelines (story → images → music → web page)
- Project management scenarios
- Any workflow where one agent coordinates multiple specialists

### Cross-references

- **OrchestratorAgent** variants: `AnthropicOrchestratorAgent`, `OpenAIOrchestratorAgent`, `GoogleOrchestratorAgent`, `OrchestratorAgent` (HF)
- **Breakout sessions**: Orchestrator can call `create_breakout_session` tool → temporary sub-floor
- **Session memory**: `MemoryStore` shared across all agents; `[REMEMBER]` directives write to it
- **Manuscript**: Accumulated accepted outputs, saved to `result/<session>/manuscript.txt`
- See [Orchestration Patterns](orchestration.md) for detailed pipeline examples

---

## Policy Comparison Matrix

| Feature | SEQUENTIAL | ROUND_ROBIN | MODERATED | FREE_FOR_ALL | SHOWRUNNER_DRIVEN |
|---------|-----------|-------------|-----------|--------------|-------------------|
| Turn order | FIFO queue | Fixed rotation | Manual | None | Orchestrator assigns |
| Agent speaks when | Floor granted | Their turn | Moderator grants | Anytime | Assigned |
| Round tracking | No | Yes | No | No | No (task-based) |
| Director compatible | ✓ | ✓ (speaks first) | ✓ | ✓ | ✗ |
| ShowRunner compatible | ✓ | ✓ (speaks last) | ✓ | ✓ | ✗ |
| Orchestrator compatible | ✗ | ✗ | ✗ | ✗ | ✓ (required) |
| Human agent | ✓ | ✓ | ✓ | ✓ | ✓ (optional) |
| Manuscript output | ✗ | ✗ | ✗ | ✗ | ✓ |
| Media auto-accept | ✗ | ✗ | ✗ | ✗ | ✓ |
| Breakout support | ✗ | ✗ | ✗ | ✗ | ✓ |
