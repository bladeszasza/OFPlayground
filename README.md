# OFP Playground

A local playground for multi-agent conversations using [Open Floor Protocol](https://github.com/open-voice-interoperability/openfloor-python) envelopes and floor events. Combine Anthropic, OpenAI, Google, and Hugging Face agents, experiment with turn-taking, and inspect how conversations produce text, media, and code artifacts.

The project includes a terminal CLI, a Gradio chat interface, and a React canvas backed by FastAPI. Creative workflows directed by an orchestrator are its most developed examples. General-purpose technical collaboration remains experimental.

**Status: experimental local workbench.** The shared runtime, authenticated ASP service, separate status dock and observed communication graph are implemented. Release verification is tracked in the [implementation ledger](docs/implementation-status.md). The [assessment](docs/assessment-2026-10-31.md) explains the original defects and the project's direction.

## What is implemented

- In-process asynchronous routing of OFP envelopes between human, LLM, and remote HTTP agents.
- Five floor-policy modes, with the implementation distinctions described below.
- Orchestrator assignment, output acceptance/revision, dynamic agent creation, breakout conversations, and shared coding sessions.
- Provider-specific text, vision, media, and coding agents. Task support differs by provider.
- A library of 200+ `SOUL.md` personas, including four [research and development experts](docs/expert-agents.md).
- Session memory, persisted phase artifacts, and an interactive HTML event trace.
- Canvas membership and observed transport layers, authoritative floor/queue observations, bounded timeline replay, filters, pinned layouts and artifact inspection.
- A separate authenticated [ASP 0.1 status service](docs/asp.md), with five status dimensions, provenance, freshness, independent observations and locally enforced quarantine.

These describe the code in this checkout, not full conformance to every OFP version or current availability of every configured provider model.

## Install from a source checkout

Requirements: **Python 3.12+**, Git, and access to package registries. **Node.js and npm are needed to rebuild Canvas.** A prebuilt frontend is included. Use an editable install: persona files and Canvas assets live outside the Python package tree, so a standalone wheel is not a complete application distribution. The current verified local environment uses Python 3.12 and Node 24.

```sh
git clone https://github.com/bladeszasza/OFPlayground.git
cd OFPlayground
python -m venv .venv
```

Activate the environment for your shell:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```sh
# macOS / Linux
source .venv/bin/activate
```

Install the Python application and Canvas backend:

```sh
python -m pip install -e ".[canvas]"
```

For Gradio, install `python -m pip install -e ".[web]"`. Gradio is optional because its current Pillow constraint prevents installing Pillow's security fixes; see [dependency review](docs/dependencies.md) before enabling media workflows. The CLI and Canvas do not require Gradio.

Development uses the committed `uv.lock`: install uv 0.13.6, then run `uv sync --locked --extra dev --extra canvas --extra web`. The OFP SDK is pinned to a tested commit. Python 3.12 and 3.13 are the CI targets; Node 22.12+ or 24 builds the frontend. This workbench is distributed as a source checkout: Canvas and the persona library use repository assets. A core wheel does not include those assets.

## Configure one provider

You can first explore without a provider account: run `ofp-playground canvas`, load **ASP showcase · no API keys**, and press **Run session**. All agents are explicitly synthetic and execute through the real session, floor and status paths. See the [showcase walkthrough](docs/asp.md#provider-free-showcase).

Create `.env` in the repository root with the key for the provider you want. You do not need all four keys:

```dotenv
OPENAI_API_KEY=your-key
# ANTHROPIC_API_KEY=your-key
# GOOGLE_API_KEY=your-key
# HF_API_KEY=your-key
```

Hugging Face also accepts `HF_TOKEN` as a fallback. The CLI group loads `.env` for `start`, `web`, and `canvas`, preserving variables already set in the process environment.

| Interface | Configuration behavior in this revision |
|---|---|
| `start` / `web` | Load `~/.ofp-playground/config.toml`; configured TOML keys take precedence over environment keys. |
| `canvas` | Uses the same TOML → environment/CLI-loaded `.env` → default precedence. |
| Direct Python examples | May read only environment variables; inspect the example before assuming CLI configuration applies. |

Model IDs in the code are configured defaults, not availability guarantees. Select an account-supported model with the agent specification's `-model` option when needed. Live sessions use your provider accounts and their billing.

## First conversation

These single-line commands work in PowerShell or a POSIX shell. They use OpenAI, matching the configuration example; substitute another supported provider if preferred.

```sh
# Interactive terminal conversation
ofp-playground start --agent "openai:Assistant:Help the human with the topic."

# Two agents with a topic and a turn-count stop condition
ofp-playground start --no-human --policy round_robin --max-turns 8 --topic "Design a short story set in a floating city." --agent "openai:Writer:Propose the story." --agent "openai:Editor:Identify weaknesses and suggest revisions."

# Local Gradio chat interface
ofp-playground web --host 127.0.0.1 --agent "openai:Assistant:Help the human with the topic."
```

`--max-turns` stops the floor on the N-th agent utterance (counted by the floor manager, so any N works). It is not a hard token, cost, or wall-clock budget. Early completion now joins and closes session tasks; cancelling a local wait cannot retract an already submitted provider job.

## Build and open Canvas

The checkout includes a built frontend. Rebuild it after changing frontend source:

```sh
cd canvas/frontend
npm ci
npm run build
cd ../..
ofp-playground canvas
```

Canvas is served at `http://localhost:8765`. Add participants, configure exactly one floor, and press Run. The backend supports one active session per app instance. Controls, media, trace and status endpoints require the local browser's HttpOnly session cookie or the app's bearer credential. The CLI accepts loopback hosts in this local profile; it does not provide multi-user isolation.

Personas resolve to full prompts for both initial and dynamically added agents. CLI prepopulation is also available:

```sh
ofp-playground canvas --agent "openai:Reviewer:@development/code-reviewer" --topic "Give technical feedback on the supplied proposal."
```

The [expert panel guide](docs/expert-agents.md) includes a four-agent prepopulation command. Canvas add, remove and send controls report backend rejection; a requested new agent is added to the live graph after acknowledgement.

`npm run dev` starts Vite with the same discovery, session, authentication, status, media and trace proxies. Use the served production bundle for the simplest setup.

## Agents and orchestration

Both agent specification formats are supported:

```sh
ofp-playground start --agent "openai:Reviewer:@development/code-reviewer"
ofp-playground start --agent "-provider openai -name Reviewer -system @development/code-reviewer"
```

Append `-model MODEL_ID` within a flag-format specification to override the provider default. A persona supplies instructions; it does not grant browsing, local file access, or execution tools. Ordinary LLM prompts are task-neutral; creative behavior belongs in a creative persona or director/showrunner role.

`ofp-playground agents` prints the provider/task reference. Search personas, preview their resolved instructions and inspect configured model choices:

```sh
ofp-playground personas reviewer --preview
ofp-playground models --json
ofp-playground status --watch
```

See [the persona library](docs/agents-library.md), [expert panel](docs/expert-agents.md), and [agent guide](docs/agents.md). Task-name parsing does not guarantee implementation by every provider. Hugging Face `code-generation` is currently unsupported.

An orchestrator can assign work, request revisions, accept phase outputs, create agents, and launch sub-sessions:

```sh
ofp-playground start --no-human --policy showrunner_driven --topic "Write and revise a short story." --agent "-provider openai -type orchestrator -name Director -system Coordinate a writer and an editor, then finish the story."
```

The runtime offers tools based on configured key presence. That does not validate credentials or model entitlement. See [orchestration](docs/orchestration.md) for directives and artifact handling.

## Floor policies

These application policies sit above the OFP message layer. [Floor policies](docs/floor-policies.md) documents the implemented rules; `tests/test_floor_delegation.py` pins them:

| Policy | Current behavior |
|---|---|
| `sequential` | Grants an idle floor to a requester, then services queued requests FIFO. |
| `round_robin` | Strict ring: the yielder's successor speaks next; requests never jump the ring. A director opens and a show runner closes each round. |
| `moderated` | With a human, the human chairs: requests queue until the chair addresses an agent by name or uses `/grant`. Without a human, FIFO. |
| `free_for_all` | Grants requests immediately (overlapping participation); LLM agents run a relevance check first. |
| `showrunner_driven` | Orchestrator assignments, parallel fan-out and accept/reject workflow; the floor returns to the orchestrator even when a worker fails silently. |

Only a holder can yield; free-for-all tracks concurrent holders. Removing a participant clears queue/rotation membership and transfers a departing holder's floor. Graceful removal excludes new work, drains the active call for up to the chosen timeout, then falls back to force cancellation. Quarantine removes and cancels the local instance; clearing its assertion does not restart it or repeat a provider call.

## Examples and output

The [examples directory](examples/) contains writing, media production, code review, and coding-session experiments. Shell scripts require Bash and often several provider keys. Inspect the chosen script's providers, models, and turn limit before running it.

| Example | Purpose |
|---|---|
| [showcase.sh](examples/showcase.sh) | Multi-phase illustrated-story workflow using several providers |
| [showcase_web.sh](examples/showcase_web.sh) | A separate showcase configuration through Gradio |
| [example_platformer.sh](examples/example_platformer.sh) | Collaborative browser-game generation |
| [example_song_production.sh](examples/example_song_production.sh) | Music-production workflow |
| [round_robin_novel.sh](examples/round_robin_novel.sh) | Collaborative story experiment |
| [breakout_code_review.sh](examples/breakout_code_review.sh) | Breakout review conversation |
| [simple_chat.py](examples/simple_chat.py) | Direct Python example; reads its provider key from the environment |

Results go under `result/<session>/`, including applicable media, code, phase artifacts, manuscript, memory snapshot, and `trace.html`. The trace records envelopes and routing decisions; it does not prove successful receipt or processing by every listed recipient. See [output structure](docs/output.md).

Remote agents can be supplied through `start --remote ALIAS_OR_URL`. Registry aliases are configuration entries, not guarantees that endpoints are currently reachable or compatible.

## Development

```sh
python -m pip install -e ".[dev,canvas]"
python -m pytest -q
python -m ruff check src/
python -m mypy src/
```

Frontend validation: run `npm ci`, `npm test` and `npm run build` from `canvas/frontend`. The checked-in CI matrix covers Windows/Linux and Python 3.12/3.13, plus Node 22.12/24 frontend tests/build/audit. Local evidence and the distinction from remote CI are recorded in the [implementation ledger](docs/implementation-status.md).

| Area | Source |
|---|---|
| Commands and agent construction | [`src/ofp_playground/cli.py`](src/ofp_playground/cli.py) |
| Routing and scheduling | [`bus/`](src/ofp_playground/bus/), [`floor/`](src/ofp_playground/floor/) |
| Agent implementations | [`agents/`](src/ofp_playground/agents/) |
| Memory and artifacts | [`memory/`](src/ofp_playground/memory/) |
| Event trace | [`trace/`](src/ofp_playground/trace/) |
| Canvas | [`canvas/backend/`](canvas/backend/), [`canvas/frontend/src/`](canvas/frontend/src/) |
| Regression tests | [`tests/`](tests/) |

## Current limitations

- Core scheduling/removal, early completion, ASP and failure scenarios have provider-free regression coverage. Live provider compatibility is opt-in.
- Canvas edges describe membership or observed routes; they do not define an execution DAG. Backend replay retains 5,000 events and reports cursor gaps; the browser renders the last 1,000 observations. ASP has its own durable SQLite journal and independent cursors.
- The message bus is local and in-memory, without durable delivery or application acknowledgments.
- Canvas and the Gradio control interface are intended for loopback use. Windows media containment uses resolved path ancestry. Public multi-tenant deployment remains outside this release.
- The SDK is pinned to commit `eab87464fde1c79c32ad6e51cf3161686daf8316`. The [dependency review](docs/dependencies.md) records the supported profile, audit and unresolved optional Gradio/Pillow security constraint.
- Provider integrations and model metadata need live compatibility checks. Personas are not separately trained experts or security boundaries.

ASP implements the [pinned local profile](docs/protocols/asp/PROFILE.md) of a draft protocol. It reports observations; it cannot automatically detect compromise. Uninstrumented remote agents remain unknown. A local cancellation cannot retract an already submitted provider job.

## Documentation and license

Start with the [documentation index](docs/README.md). Some reference pages predate current implementation; the index identifies important discrepancies.

Licensed under [Apache-2.0](LICENSE).
