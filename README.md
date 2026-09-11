# OFP Playground

A model-agnostic, multi-provider sandbox for the [Open Floor Protocol](https://github.com/open-voice-interoperability/openfloor-python).

The idea is straightforward: give multiple AI agents a shared floor, let them take turns following OFP rules, and see what happens when you hand one of them the conductor's baton. 

---

## What this is

OFP Playground is a CLI tool that:

- runs OFP-compliant multi-agent conversations in-process over async queues
- lets you mix agents from any provider (Anthropic, OpenAI, Google, HuggingFace) in the same session
- enforces structured turn-taking via OFP floor request / grant / yield mechanics
- supports five floor policies that change *how* agents take turns
- includes a showrunner-driven policy where one agent acts as a director, dynamically assigning tasks, accepting or rejecting output, and spawning new specialist agents on demand

It's not a framework. It's a playground. We built it to explore what OFP can actually do when pushed past simple chat. The showcase is our answer.

---

## Installation

```bash
git clone https://github.com/bladeszasza/OFPlayground
cd OFPlayground
pip install -e .
```

**Python 3.10+** required.

**API keys** — create `.env` in the project root:

```env
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=AIza...
HF_API_KEY=hf_...
```

Keys are also read from `~/.ofp-playground/config.toml` under `[api_keys]`. Only the providers you configure become available to orchestrators.

---

## Quick Start

`ofp-playground canvas`

or other options

```bash
# Simple chat with one agent
ofp-playground start --agent "anthropic:Claude:You are a helpful assistant."

# Rich Canvas interface 
ofp-playground canvas

# Autonomous debate, no human
ofp-playground start --no-human \
  --policy round_robin \
  --topic "Is remote work better than office work?" \
  --max-turns 12 \
  --agent "hf:Optimist:You believe remote work is superior." \
  --agent "hf:Skeptic:You believe office work fosters better collaboration."

# Gradio web UI
ofp-playground web \
  --agent "anthropic:Claude:You are a helpful assistant." \
  --agent "google:text-to-image:Painter:impressionistic oil painting"
```

---

## Floor Policies

How agents take turns is controlled by policy. See [docs/floor-policies.md](docs/floor-policies.md) for the full breakdown.

| Policy | Behaviour |
|---|---|
| `sequential` | Agents take turns in join order |
| `round_robin` | Strict rotation; every agent speaks every cycle |
| `moderated` | Agents request the floor; moderator decides |
| `free_for_all` | Anyone speaks at any time |
| `showrunner_driven` | One orchestrator agent assigns tasks and controls flow |

```bash
ofp-playground start --policy round_robin --agent ...
```

---

## Agent Library

The `agents/` directory is a library of 200+ ready-to-use personas organised by domain. Each persona is a `SOUL.md` file — a structured system prompt encoding an agent's identity, principles, and output format.

Reference any persona with the `@category/name` slug:

```bash
# Use a persona as the system prompt
--agent "anthropic:Alice:@development/code-reviewer"
--agent "-provider google -name Bob -system @creative/brand-designer"

# List all available slugs
ofp-playground agents
```

Categories: `automation`, `business`, `compliance`, `creative`, `customer-success`, `data`, `development`, `devops`, `ecommerce`, `education`, `finance`, `freelance`, `healthcare`, `hr`, `legal`, `marketing`, `personal`, `productivity`, `real-estate`, `saas`, `security`, `supply-chain`, `voice`.

The `development/` category includes coding-aware personas with embedded TDD, debugging, and verification methodology — `@development/coding-agent` is auto-loaded by all coding agents when no system prompt is provided.

See [docs/agents-library.md](docs/agents-library.md) for the full category breakdown, auto-loaded personas, and how to add new ones.

---

## Agent Spec Formats

Two formats, freely mixable. See [docs/cli.md](docs/cli.md) for full reference.

```bash
# Colon format: provider:name:description
--agent "anthropic:Claude:You are a helpful assistant."
--agent "hf:Bob:You are a skeptical physicist.:meta-llama/Llama-3.1-8B-Instruct"

# With task subtype
--agent "openai:text-to-image:Artbot:cinematic concept art"
--agent "google:text-to-music:Composer:ambient cinematic score"

# Flag format: -provider TYPE -name NAME [-type TASK] [-system DESC] [-model MODEL]
--agent "-provider anthropic -name Claude -system You are helpful."
--agent "-provider hf -type Text-to-Image -name Flux -system photorealistic -model black-forest-labs/FLUX.1-dev"
```

---

## Providers & Tasks

Full table in [docs/agents.md](docs/agents.md).

| Provider | Alias | Default model |
|---|---|---|
| Anthropic | `anthropic` / `claude` | `claude-haiku-4-5-20251001` |
| OpenAI | `openai` / `gpt` | `gpt-5.4-nano` |
| Google | `google` / `gemini` | `gemini-3.1-flash-lite` |
| HuggingFace | `hf` / `huggingface` | `MiniMaxAI/MiniMax-M2.7` |

Supported task types across providers: text generation, image generation, image-to-text (vision), text-to-music, text-to-video, code-generation (OpenAI/Anthropic/Google), classification, NER, summarization, orchestrator.

---

## Examples

Ready-made scripts in [`examples/`](examples/):

| Script | What it does |
|---|---|
| [`showcase.sh`](examples/showcase.sh) | Full illustrated story pipeline — 10 chapters, images, music, HTML (any topic) |
| [`showcase_web.sh`](examples/showcase_web.sh) | Same pipeline via Gradio web UI |
| [`example_platformer.sh`](examples/example_platformer.sh) | Procedural Three.js endless runner — 9-phase design + coding pipeline (Anthropic + OpenAI) |
| [`example_song_production.sh`](examples/example_song_production.sh) | AI music production pipeline |
| [`round_robin_novel.sh`](examples/round_robin_novel.sh) | Round-robin collaborative novel |
| [`sequential_code_review.sh`](examples/sequential_code_review.sh) | Sequential code review pipeline |
| [`breakout_code_review.sh`](examples/breakout_code_review.sh) | Code review using breakout sessions |
| [`moderated_investment_committee.sh`](examples/moderated_investment_committee.sh) | Moderated investment committee |
| [`free_for_all_brainstorm.sh`](examples/free_for_all_brainstorm.sh) | Free-for-all brainstorm |
| [`simple_chat.py`](examples/simple_chat.py) | Minimal Python example |

---

## Remote OFP Agents

Connect any live OFP-compatible HTTP endpoint:

```bash
ofp-playground start --no-human \
  --topic "What do JWST observations reveal about galaxy formation?" \
  --agent "hf:Alice:You are Alice." \
  --remote arxiv \
  --remote wikipedia

# Or mid-conversation
/spawn remote arxiv
/spawn remote https://my-custom-agent.example.com/ofp
```

Known live agents: `polly`, `arxiv`, `github`, `sec`, `web-search`, `wikipedia`, `stella`, `verity`, `profanity`. Full registry at [openfloor.dev/agent-registry](https://openfloor.dev/agent-registry).

---

## Orchestrator (Showrunner-Driven)

Any provider can act as orchestrator. It speaks first, assigns tasks, evaluates output, and spawns new agents on demand. See [docs/orchestration.md](docs/orchestration.md) for the full directive reference.

| Directive | Action |
|---|---|
| `[ASSIGN AgentName]: task` | Grant floor to the named agent with a task |
| `[ACCEPT]` | Accept the last output into manuscript + artifact store |
| `[REJECT AgentName]: reason` | Re-grant with revision feedback |
| `[KICK AgentName]` | Remove an agent from the session |
| `[SPAWN spec]` | Dynamically create a new specialist agent |
| `[SKIP AgentName]: reason` | Record skip in manuscript, return floor to orchestrator |
| `[BREAKOUT policy=… max_rounds=… topic=…]` | Spin up a temporary sub-floor session |
| `[CODING_SESSION policy=… max_rounds=… topic=…]` | Launch a multi-agent coding sub-floor with file tools |
| `[REMEMBER category]: content` | Write to in-session memory store |
| `[TASK_COMPLETE]` | End the session |

Agent spawning goes through **native tool calling** — the orchestrator calls typed tools (`spawn_text_agent`, `spawn_image_agent`, etc.) built from whatever API keys are actually present. It can't hallucinate a provider that isn't configured.

```bash
ofp-playground start \
  --no-human \
  --policy showrunner_driven \
  --topic "Create a short illustrated story with 3 chapters." \
  --agent "-provider anthropic -type orchestrator -name Director -model claude-sonnet-4-6 \
           -system Create a short illustrated story with 3 chapters."
```

The orchestrator starts alone and spawns whatever it needs.

---

## In-Conversation Commands

| Command | Description |
|---|---|
| `/help` | Show all commands |
| `/agents` | List active agents and floor holder |
| `/floor` | Show floor holder and queue |
| `/history [N]` | Last N utterances |
| `/spawn <spec>` | Add an agent mid-conversation |
| `/kick <name>` | Remove an agent |
| `/quit` | End the session |

---

## Documentation

| Doc | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Message bus, FloorManager, agent hierarchy |
| [docs/ofp-protocol.md](docs/ofp-protocol.md) | OFP event types and how they're used |
| [docs/agents.md](docs/agents.md) | All agent types, tasks, and default models |
| [docs/agents-library.md](docs/agents-library.md) | `agents/` SOUL.md persona library — all 200+ slugs, @slug usage, how to add new personas |
| [docs/orchestration.md](docs/orchestration.md) | Showrunner directives, breakout sessions, manuscript |
| [docs/floor-policies.md](docs/floor-policies.md) | All five floor policies explained |
| [docs/cli.md](docs/cli.md) | Full CLI reference |
| [docs/configuration.md](docs/configuration.md) | API keys, config file, env vars |
| [docs/output.md](docs/output.md) | Session output layout (`result/`, media dirs) |

---

## Project Structure

```
src/ofp_playground/
├── cli.py                      # Click CLI (start, web, agents, validate)
├── bus/message_bus.py          # Async in-process OFP message bus
├── floor/
│   ├── manager.py              # FloorManager: OFP coordinator
│   ├── policy.py               # Five floor policies
│   ├── history.py              # Conversation history
│   ├── coding_session.py       # Multi-agent coding sub-floor (shared sandbox + file tools)
│   └── coding_session_tools.py # File tools injected into coding agents (read/write/edit/todo)
├── memory/
│   ├── store.py                # Ephemeral MemoryStore (goals, tasks, decisions, lessons…)
│   └── artifact_store.py       # Phase ArtifactStore (persisted to result/<session>/phases/)
├── trace/
│   ├── collector.py            # EventCollector — records every routed envelope
│   ├── model.py                # TraceEvent frozen dataclass
│   └── renderer.py             # D3-based interactive HTML timeline (trace.html)
├── agents/
│   ├── base.py                 # BasePlaygroundAgent
│   ├── human.py                # Human stdin/stdout agent
│   ├── web_human.py            # Human agent for Gradio
│   ├── remote.py               # Remote OFP agent via HTTP
│   └── llm/
│       ├── base.py             # BaseLLMAgent
│       ├── anthropic.py        # Anthropic Claude (text)
│       ├── openai.py           # OpenAI GPT (text + image)
│       ├── google.py           # Google Gemini (text + image + music)
│       ├── huggingface.py      # HuggingFace (text + image + video + perception)
│       ├── codex.py            # BaseCodingAgent + OpenAICodingAgent (code_interpreter)
│       ├── anthropic_coding.py # AnthropicCodingAgent (code_execution_20250825 beta)
│       ├── google_coding.py    # GoogleCodingAgent (ToolCodeExecution)
│       ├── model_catalog.py    # ModelCaps + MODEL_CATALOG for 9 models (drives manifests)
│       └── showrunner.py       # Orchestrator agents (all providers)
└── renderer/
    ├── terminal.py             # Rich terminal output
    └── gradio_ui.py            # Gradio web UI
agents/                         # SOUL.md persona library (@slug syntax)
examples/                       # Ready-to-run scripts
docs/                           # Architecture and reference docs
```

---

## License

Apache-2.0
