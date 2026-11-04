# Floor Policies

OFP Playground implements **5 floor control policies** that decide who may speak. The policy is a `FloorPolicy` enum; the pure state machine is `FloorController` (`src/ofp_playground/floor/policy.py`) and the round / delegation logic lives in `FloorManager` (`src/ofp_playground/floor/manager.py`).

This page describes implemented behavior (verified by `tests/test_floor_delegation.py`, `tests/test_floor_controller.py` and `tests/test_session_lifecycle.py`). The same rules apply in `start`, `web` and `canvas` because all three use the same floor manager and the shared `session.kickoff()`.

## Policy Summary

| Policy | Who speaks next | Best For |
|--------|-----------------|----------|
| [SEQUENTIAL](#sequential) | First requester, then FIFO queue | Panel Q&A, step-by-step reviews |
| [ROUND_ROBIN](#round_robin) | Strict ring successor; director opens / show runner closes a round | Multi-chapter stories, debates |
| [MODERATED](#moderated) | Whoever the human chair calls on (FIFO without a human) | Expert panels, chaired reviews |
| [FREE_FOR_ALL](#free_for_all) | Anyone, immediately (relevance-filtered) | Brainstorming, rapid ideation |
| [SHOWRUNNER_DRIVEN](#showrunner_driven) | The worker the orchestrator assigned; then the orchestrator | Production pipelines, creative production |

## Invariants shared by every policy

- **Stale yields are ignored.** A `yieldFloor` from an agent that does not hold the floor never advances it, so two agents can't both end up holding it.
- **A grant revokes the previous holder** (except under FREE_FOR_ALL, where holders are concurrent).
- **Removing an agent hands its floor on**. That applies to removals from `/kick`, the canvas remove button, `[KICK]`, `bye` and quarantine. If the agent was an in-flight SHOWRUNNER worker, the orchestrator gets the floor back with a `[SYSTEM]` note.
- **Turn limit:** `--max-turns N` / `maxTurns` stops the floor on the N-th agent utterance. It is counted by the floor manager itself, so `N > 100` works. Conversation history only keeps the last 100 entries. It is a stop condition, not a cost or time budget.
- **First grant (`FloorManager.kickoff_target`)** is the same in every UI:
  1. The orchestrator if there is one.
  2. Otherwise the director if there is one.
  3. Nobody under MODERATED with a human chair.
  4. The first ring member under ROUND_ROBIN.
  5. The first agent of an autonomous session that has no topic.
  6. Otherwise nobody, so agents request the floor in response to the topic.

---

## SEQUENTIAL

```bash
ofp-playground start --policy sequential --agent "anthropic:Alice:..." --agent "openai:Bob:..."
```

- The first agent to request an idle floor gets it; later requests wait in a **FIFO queue** and are served one at a time as each speaker yields. Agents re-request after each utterance they see, so a conversation proceeds in roughly round-like order.
- **Human** (terminal, web and canvas alike): requests the floor at start, so it opens the conversation. After others speak it queues for its next turn and yields after sending. `/pass` releases a turn without speaking. When a topic is seeded the human's start-up turn is released first so agents can answer the topic.
- A director / show runner, if present, is granted at round boundaries. A round is every text participant except media, perception and vision agents and the director / show runner themselves.

Example: `bash examples/sequential_code_review.sh "def process(u): exec(u)"`. Three reviewers each speak once per human turn.

---

## ROUND_ROBIN

```bash
ofp-playground start --policy round_robin --agent "anthropic:Alice:..." --agent "openai:Bob:..."
```

- **Strict ring.** Participants rotate in join order. When the holder yields, the floor goes to its ring successor. Requests from ring members are never queued, so they can't jump the ring; they only bootstrap an idle floor.
- **Round boundaries:** a round is `[director] → every ring member → [show runner]`. Director and show runner are **not** ring members; the floor manager grants them explicitly. Ring wraps without either inject a short `[FLOOR — Round N complete]` recap for LLM agents.
- **Silent turns** (an agent yields without speaking) just pass to the successor. If a full cycle passes with nobody speaking (e.g. only perception agents with nothing to analyse), the floor **parks** instead of spinning forever. The next request, for example after new human input, restarts it.
- **Humans are ring members.** When it's their turn they are told (“Floor granted to you … or /pass”) and the ring waits for them. This now holds in the web UI and canvas too; previously the web/canvas human never yielded and deadlocked the ring.
- First grant: the director if present, otherwise the first non-human ring member when a topic was seeded, otherwise the first ring member.

Example: `bash examples/round_robin_novel.sh "a gothic horror mystery" 5`. Director sets scene beats, the three voices write in order, and the ShowRunner synthesises canon.

---

## MODERATED

```bash
ofp-playground start --policy moderated --human-name Chair --agent "anthropic:MacroAnalyst:..." ...
```

- **With a human in the session, the human is the chair.** Agent requests are queued and **never auto-granted**. The chair gives the floor by:
  - **addressing agents by name**, e.g. `"RiskAnalyst and MacroAnalyst, your view?"`. Agents are called on in the order they are mentioned; the first speaks now and the rest follow as each yields. Called-on agents are served before agents that merely requested.
  - **`/grant NAME`** in the terminal, the web chat box, the canvas human input, or `POST /session/floor/grant/{name}`.
- `/floor` shows the holder and the waiting queue (called-on agents first).
- **Without a human**, the floor manager is the moderator and serves requests FIFO, the same as SEQUENTIAL.
- Nobody is granted at start-up when a chair is present; the chair opens.

Example: `bash examples/moderated_investment_committee.sh "NVDA — is the AI capex cycle sustainable?"`

---

## FREE_FOR_ALL

```bash
ofp-playground start --policy free_for_all --agent "anthropic:Alice:..." --agent "openai:Bob:..."
```

- Every floor request is granted immediately; several agents can hold the floor concurrently (`floor_holders`). There is no queue or rotation.
- **Relevance filter:** before requesting the floor in response to a regular utterance, LLM agents ask themselves “should I contribute?” with a short `_check_relevance()` call. Directives, director scenes and grants bypass it. It runs only under FREE_FOR_ALL, because the other policies give every agent a turn by design. Disable it with `defaults.relevance_filter = false` in `~/.ofp-playground/config.toml`. Providers without a cheap check (e.g. scripted demo agents) always speak.
- The human never needs the floor and can speak at any time.

Example: `bash examples/free_for_all_brainstorm.sh "the most transformative opportunity in AI right now"`

---

## SHOWRUNNER_DRIVEN

```bash
ofp-playground start --policy showrunner_driven \
  --agent "anthropic:orchestrator:Director:You drive the pipeline." \
  --agent "openai:Writer:You write scripts." \
  --agent "hf:text-to-image:Painter:Generate storyboard images."
```

- One **orchestrator** controls the session and gets the first grant. Workers only take the floor when assigned, and only the assigned worker's (or parallel batch's) requests are honoured.
- **The floor always returns to the orchestrator:**
  - after the assigned worker's output (media is auto-accepted);
  - after the worker **yields without any output** (error, timeout, empty reply). The orchestrator also receives `[SYSTEM] X yielded the floor without delivering any output…`. Previously this stalled the session.
  - after an in-flight worker is removed by an operator;
  - after a breakout / coding session completes;
  - when the orchestrator itself yields without assigning. It gets an escalating recovery nudge, and the session stops after 8 idle turns.
- **Unsolicited output does not count as delivery.** A human interjecting, a remote agent answering a broadcast or a late reply is kept as conversation context. It never clears the in-flight assignment or hands the orchestrator a second turn. Human input does wake an idle orchestrator.

### Orchestrator Directives

| Directive | Action |
|-----------|--------|
| `[ASSIGN Name]: task` | Private `[DIRECTIVE for Name]` (task may span lines), then the grant |
| `[ASSIGN_PARALLEL A, B]: task` | Same task to several agents at once (fan-out); the orchestrator regains the floor once **all** delivered or failed |
| `[ACCEPT]` / `[ACCEPT prose]` | Append the last delivery (all parallel answers, labelled) to the manuscript and save a phase artifact |
| `[ACCEPT plan\|image\|music\|audio\|video]` | Save the phase artifact only; keep the manuscript clean |
| `[REJECT Name]: reason` | Revision directive (reason **and** the original task), then the grant; the rejected draft can't be `[ACCEPT]`ed afterwards |
| `[KICK Name]` | OFP `uninvite` and removal |
| `[SPAWN spec]` | Create a new agent (follow with `[ASSIGN NewName]`) |
| `[SKIP Name]: reason` | Record a skip in the manuscript and move on |
| `[STORE_MEMORY key]: content` | Write a decision to session memory |
| `[BREAKOUT …]` / `[CODING_SESSION …]` | Temporary sub-floor; **not** started in the same turn as an `[ASSIGN]` (the orchestrator is told why) |
| `[TASK_COMPLETE]` | Save the manuscript and end the session |

### What a worker receives

The floor manager sends the directive privately and **before** the grant, so the worker reads its task first. Then:
- **Text workers** get the whole directive: the task, the compact phase-artifact index, the sandbox note, session memory, and (once) the latest breakout transcript.
- **Media workers** (image / video / audio generators) get only the bare prompt. The context blocks used to leak into Lyria prompts.
- **Remote OFP agents** get only the bare task.

See [Orchestration Patterns](orchestration.md) for pipelines and breakouts.

---

## Policy Comparison Matrix

| Feature | SEQUENTIAL | ROUND_ROBIN | MODERATED | FREE_FOR_ALL | SHOWRUNNER_DRIVEN |
|---------|-----------|-------------|-----------|--------------|-------------------|
| Next speaker | FIFO requests | Ring successor | Chair's call-on (FIFO w/o human) | Anyone, now | Assigned worker → orchestrator |
| Requests queued | Yes | No (ring members) | Yes, until called on | No | Only assigned agents |
| Round tracking | On utterances | On ring wrap | On utterances | On utterances | No (task-based) |
| Director / ShowRunner | At round boundaries | Opens / closes each round | At round boundaries | At round boundaries | ✗ |
| Orchestrator | ✗ | ✗ | ✗ | ✗ | Required |
| Human | Takes turns | Ring member | Chair (moderator) | Speaks anytime | Interjects; can be assigned |
| Relevance filter | — | — | — | ✓ | — |
| Manuscript / media auto-accept / breakouts | ✗ | ✗ | ✗ | ✗ | ✓ |
