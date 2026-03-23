# OFPlayground

A CLI tool for running multi-party AI conversations using the [Open Floor Protocol (OFP)](https://github.com/open-voice-interoperability/openfloor-python). Spawn agents from any provider, pick a floor policy, and watch them collaborate — or build autonomous pipelines where one agent's output feeds the next.

**GitHub:** https://github.com/bladeszasza/OFPlayground

---

## Features

- **Multi-agent conversations** — mix human input with LLM agents from multiple providers
- **Open Floor Protocol** — structured turn-taking with floor request / grant / yield mechanics (OFP-compliant invite/uninvite flow for spawned agents)
- **Five floor policies** — sequential, round-robin, moderated, free-for-all, showrunner-driven
- **Four LLM providers** — Anthropic Claude, OpenAI GPT, Google Gemini, HuggingFace Inference API
- **Multi-modal agents** — text, image generation, image analysis (vision), music, and video
- **Cross-provider pipelines** — chain agents across providers (Claude narrates → Google paints → Claude sees → Google scores)
- **Orchestrator agents** — any provider can run as an intelligent project manager that dynamically spawns, assigns, and directs specialist agents; uses native tool calling so the LLM can never hallucinate invalid spawn parameters
- **Registry-driven tool calling** — orchestrator spawn tools are built from configured API keys at runtime; only available providers appear as options
- **Remote OFP agents** — connect any live OFP-compatible HTTP endpoint with `--remote`
- **Autonomous mode** — agent-only sessions with `--no-human --topic`
- **Dynamic agent management** — `/spawn` and `/kick` agents mid-conversation
- **Gradio web UI** — browser-based chat via `ofp-playground web`
- **Rich terminal UI** — per-agent colors, timestamps, floor status

---

## Installation

```bash
git clone https://github.com/bladeszasza/OFPlayground
cd OFPlayground
pip install -e .
```

**Requirements:** Python 3.10+

---

## API Keys

Create a `.env` file in the project root (or export variables in your shell):

```env
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=AIza...
HF_API_KEY=hf_...
```

Keys are also read from `~/.ofp-playground/config.toml` under `[api_keys]`.

---

## Quick Start

### Interactive session

```bash
ofp-playground start --agent "anthropic:Claude:You are a helpful assistant."
```

### Autonomous debate (no human)

```bash
ofp-playground start --no-human \
  --topic "Is remote work better than office work?" \
  --max-turns 20 \
  --agent "hf:Optimist:You believe remote work is superior." \
  --agent "hf:Skeptic:You believe office work fosters better collaboration."
```

### Gradio web UI

```bash
ofp-playground web \
  --agent "hf:Alice:You are a curious explorer." \
  --agent "anthropic:Claude:You are a helpful assistant."
```

Open `http://localhost:7860`. Use `--no-human` for watch-only mode.

---

## Agent Spec Formats

Two equivalent formats can be mixed freely.

### Colon format

```
provider:name[:description[:model]]
provider:subtype:name[:description[:model]]
```

```bash
# Text generation
--agent "anthropic:Claude:You are a helpful assistant."
--agent "hf:Bob:You are a skeptical physicist.:meta-llama/Llama-3.1-8B-Instruct"

# With task subtype
--agent "openai:text-to-image:Artbot:cinematic concept art"
--agent "google:text-to-image:Painter:impressionistic oil painting:gemini-2.5-flash-image"
--agent "anthropic:image-to-text:Scout:You are a sharp visual critic."
--agent "google:text-to-music:Composer:ambient cinematic score"
```

### Flag format

```
-provider TYPE -name NAME [-type TASK] [-system DESCRIPTION] [-model MODEL]
[-timeout SECONDS] [-max-retries N] [-max-tokens N]
```

```bash
--agent "-provider anthropic -name Claude -system You are a helpful assistant."
--agent "-provider hf -type Text-to-Image -name Flux -system photorealistic photography -model black-forest-labs/FLUX.1-dev"
--agent "-provider openai -type Image-to-Text -name Lens -system You are a visual analyst."
--agent "-provider google -type Text-to-Music -name Composer -system ambient cinematic score"
```

---

## LLM Providers & Tasks

### Anthropic Claude

| Type alias | Default model | Env var |
|---|---|---|
| `anthropic` / `claude` | `claude-haiku-4-5-20251001` | `ANTHROPIC_API_KEY` |

| Task (`-type`) | Notes |
|---|---|
| `Text-Generation` *(default)* | Standard chat/text |
| `Image-to-Text` | Vision — analyze images via Claude |
| `Orchestrator` | Intelligent project manager (see below) |

### OpenAI GPT

| Type alias | Default model | Env var |
|---|---|---|
| `openai` / `gpt` | `gpt-5.4-nano` | `OPENAI_API_KEY` |

| Task (`-type`) | Notes |
|---|---|
| `Text-Generation` *(default)* | Standard chat/text |
| `Text-to-Image` | Images via Responses API `image_generation` tool |
| `Image-to-Text` | Vision analysis |
| `Orchestrator` | Intelligent project manager |

### Google Gemini

| Type alias | Default model | Env var |
|---|---|---|
| `google` / `gemini` | `gemini-3.1-flash-lite-preview` | `GOOGLE_API_KEY` |

| Task (`-type`) | Default model | Notes |
|---|---|---|
| `Text-Generation` *(default)* | `gemini-3.1-flash-lite-preview` | Standard chat/text |
| `Text-to-Image` | `gemini-3.1-flash-image-preview` | Nano Banana image generation |
| `Image-to-Text` | `gemini-3-flash-preview` | Vision analysis |
| `Text-to-Music` | `lyria-realtime-exp` | Lyria RealTime — 15s WAV → `./ofp-music/` |
| `Orchestrator` | `gemini-3.1-flash-lite-preview` | Intelligent project manager |

### HuggingFace Inference API

| Type alias | Default model | Env var |
|---|---|---|
| `hf` / `huggingface` | `MiniMaxAI/MiniMax-M2.5` | `HF_API_KEY` |

| Task (`-type`) | Notes |
|---|---|
| `Text-Generation` *(default)* | Any HF text-gen model |
| `Text-to-Image` | Default: `FLUX.1-dev` — images → `./ofp-images/` |
| `Text-to-Video` | Default: `Wan2.2-TI2V-5B` — clips → `./ofp-videos/` |
| `Image-Text-to-Text` | Default: `Qwen2.5-VL-7B` |
| `Image-Classification` | Label images |
| `Object-Detection` | Detect and count objects |
| `Image-Segmentation` | Segment image regions |
| `Image-to-Text` | OCR / read text from images |
| `Text-Classification` | Sentiment classification |
| `Token-Classification` | Named entity recognition |
| `Summarization` | Periodic conversation summarization |
| `Orchestrator` | Intelligent project manager |

---

## Orchestrator (Showrunner-Driven Policy)

Any provider can act as an intelligent orchestrator. The orchestrator speaks first, assigns tasks to worker agents, evaluates output, and can spawn new specialists on demand.

### Supported directives

| Directive | Action |
|---|---|
| `[ASSIGN AgentName]: task` | Grant floor to the named agent with a concrete task |
| `[ACCEPT]` | Accept the last output; move to the next step |
| `[REJECT AgentName]: reason` | Re-grant floor with revision feedback |
| `[KICK AgentName]` | Remove an unresponsive or unsuitable agent |
| `[TASK_COMPLETE]` | Signal that the mission is complete |

Spawning a new agent is done via **native tool calling** (not free-text): the orchestrator calls `spawn_text_agent`, `spawn_image_agent`, `spawn_video_agent`, or `spawn_music_agent`. Tool definitions are generated at startup from the API keys that are actually present — the LLM can't select a provider that isn't configured.

### Usage

```bash
# Anthropic orchestrator (Claude Sonnet)
ofp-playground start \
  --no-human \
  --policy showrunner_driven \
  --topic "Write a short mystery story with 3 chapters." \
  --agent "-provider anthropic \
           -type orchestrator \
           -name Director \
           -model claude-sonnet-4-6 \
           -system Write a short mystery story with 3 chapters."

# OpenAI orchestrator
ofp-playground start \
  --no-human \
  --policy showrunner_driven \
  --topic "Create a product launch campaign with copy and imagery." \
  --agent "-provider openai -type orchestrator -name PM -system Your mission."

# Google orchestrator
ofp-playground start \
  --no-human \
  --policy showrunner_driven \
  --topic "..." \
  --agent "-provider google -type orchestrator -name Director -system ..."

# HuggingFace orchestrator
ofp-playground start \
  --no-human \
  --policy showrunner_driven \
  --topic "..." \
  --agent "-provider hf -type orchestrator -name Director -model MiniMaxAI/MiniMax-M2.5 -system ..."
```

The orchestrator starts alone — it will spawn whatever specialist agents it needs.

---

## Cross-Provider Pipelines

Agents from different providers can feed each other's output. A typical pattern:

```
text → image → vision → music
Claude narrates → Google paints → Claude analyses → Google scores
```

Ready-made scripts are in `examples/`:

```bash
# Full Google floor (Gemini text + image + vision + Lyria music)
./examples/google_floor.sh "a rainy Tokyo street at midnight"

# Cross-provider floor (Claude text + Google image + Claude vision + Google music)
./examples/claude_floor.sh "a lone lighthouse keeper watching a storm roll in"

# Anthropic orchestrator driving a romantic comedy novella with 80s skate culture
./examples/run_orchestrator.sh

# Full romantic comedy with pre-configured HF workers
./examples/romanticomedy.sh

# Cosmos floor — Claude narrator + Stella (NASA images) + ArXiv + Wikipedia + Verity (fact-check)
./examples/cosmos_floor.sh "What do JWST observations reveal about galaxy formation?"
```

---

## Remote OFP Agents

Remote agents are referenced by **slug name** or raw URL — no need to remember endpoint addresses:

```bash
# Using known slugs
ofp-playground start --no-human \
  --topic "Your topic here" \
  --agent "hf:Alice:You are Alice." \
  --remote polly \
  --remote wikipedia

# Or spawn mid-conversation
/spawn remote arxiv
/spawn remote https://my-custom-agent.example.com/ofp
```

**Known live OFP agents** — full registry at [openfloor.dev/agent-registry](https://openfloor.dev/agent-registry):

| Slug | Name | Description |
|------|------|-------------|
| `polly` | Polly the Parrot | Echoes back any message with a parrot emoji — great for testing OFP round-trips |
| `arxiv` | ArXiv Research Specialist | Find and analyze scientific papers on arXiv |
| `github` | GitHub Technology Analyst | Analyze repositories for technology adoption trends |
| `sec` | SEC Financial Analyst | Research SEC filings and financial data for public companies |
| `web-search` | Web Search Specialist | Search the web for current information, news, and guides |
| `wikipedia` | Wikipedia Research Specialist | Encyclopedic research and authoritative factual information |
| `stella` | Stella | Shows astronomical images from NASA's image libraries |
| `verity` | Verity | Detects and mitigates hallucinations, fact-checking specialist |
| `profanity` | Content Moderator Sentinel | Automated content moderation and profanity detection |

---

## Floor Policies

| Policy | Behaviour |
|---|---|
| `sequential` | Agents take turns in the order they joined |
| `round_robin` | Strict rotation through all registered agents |
| `moderated` | Agents request the floor; moderator grants |
| `free_for_all` | Anyone can speak at any time |
| `showrunner_driven` | Orchestrator agent assigns tasks and controls flow |

```bash
ofp-playground start --policy round_robin --agent ...
```

---

## CLI Reference

### Global options (before the subcommand)

```
ofp-playground [OPTIONS] COMMAND

  -v, --verbose    Enable debug logging (full OFP envelope traces)
```

**Debug example:**
```bash
ofp-playground -v start --policy round_robin --remote polly
```

### `ofp-playground start`

```
Options:
  -p, --policy TEXT              Floor policy (default: sequential)
  -a, --agent TYPE:NAME...       Pre-spawn an agent (repeatable)
  -r, --remote URL_OR_NAME       Remote OFP agent by slug or URL (repeatable)
  --no-human                     Run without human input (autonomous mode)
  -t, --topic TEXT               Seed topic to start the conversation
  -n, --max-turns INT            Stop automatically after N utterances
  --human-name TEXT              Display name for the human (default: User)
  --show-floor-events            Show floor grant/request events
```

### `ofp-playground web`

```
Options:
  -p, --policy TEXT          Floor policy (default: sequential)
  -a, --agent TYPE:NAME...   Pre-spawn an agent (repeatable)
  -t, --topic TEXT           Seed topic for autonomous sessions
  --no-human                 Watch-only mode
  -n, --max-turns INT        Stop automatically after N utterances
  --host TEXT                Host to bind (default: 0.0.0.0)
  --port INT                 Port to listen on (default: 7860)
  --share                    Create a public Gradio share link
```

### `ofp-playground agents`

List all available agent types, tasks, and default models.

### `ofp-playground validate <file>`

Validate an OFP envelope JSON file.

---

## In-Conversation Commands

| Command | Description |
|---|---|
| `/help` | Show all commands |
| `/agents` | List active agents and floor holder |
| `/floor` | Show current floor holder and queue |
| `/history [N]` | Show last N utterances (default 10) |
| `/spawn <type> <name> [desc] [model]` | Add a new LLM agent mid-conversation |
| `/spawn remote <slug-or-url>` | Connect a remote OFP agent mid-conversation |
| `/kick <name>` | Remove an agent from the conversation |
| `/quit` | End the session |

---

## Examples

### Human + LLM + Image Artist

```bash
ofp-playground start \
  --agent "anthropic:Claude:You are a thoughtful assistant." \
  --agent "google:text-to-image:Painter:impressionistic oil painting, dramatic light"
```

### Cross-provider multi-modal floor

```bash
ofp-playground start --no-human --policy sequential --max-turns 8 \
  --agent "anthropic:text-generation:Claude:You are a poetic narrator. Describe the scene vividly in 3-4 sentences." \
  --agent "google:text-to-image:Painter:impressionistic oil painting, dramatic light:gemini-2.5-flash-image" \
  --agent "anthropic:image-to-text:Scout:You are a sharp visual critic. Describe exactly what you see." \
  --agent "google:text-to-music:Composer:cinematic orchestral score, tension and beauty" \
  --topic "A lone lighthouse keeper watches a storm roll in from the sea"
```

### Autonomous multi-agent debate

```bash
ofp-playground start --no-human \
  --topic "Skateboarding on streets vs in the park: which is better?" \
  --max-turns 12 \
  --policy round_robin \
  --agent "-provider hf -name StreetSkater -system You are a passionate street skater who loves urban spots." \
  --agent "-provider hf -name ParkSkater -system You are a competitive park skater who trains at skate parks." \
  --agent "-provider anthropic -name Referee -system You moderate the debate impartially and summarize key points."
```

### Orchestrator with pre-configured workers

```bash
ofp-playground start \
  --no-human \
  --policy showrunner_driven \
  --topic "Create a short illustrated story about a robot learning to paint." \
  --agent "-provider anthropic \
           -type orchestrator \
           -name Director \
           -model claude-sonnet-4-6 \
           -system Create a short illustrated story about a robot learning to paint." \
  --agent "-provider hf -name Writer -system You write vivid, emotional short fiction. Write EXACTLY what the Director assigns." \
  --agent "-provider hf -type Text-to-Image -name Painter -system painterly illustration style, warm colors"
```

---

## Project Structure

```
src/ofp_playground/
├── cli.py                      # Click CLI (start, web, agents, validate)
├── config/settings.py          # Settings + API key resolution
├── bus/message_bus.py          # Async in-process message bus
├── floor/
│   ├── manager.py              # Floor coordinator + OFP invite/uninvite
│   ├── policy.py               # Floor policies
│   └── history.py              # Conversation history
├── agents/
│   ├── base.py                 # BasePlaygroundAgent
│   ├── human.py                # Human stdin/stdout agent
│   ├── web_human.py            # Human agent for Gradio
│   ├── remote.py               # Remote OFP agent via HTTP
│   └── llm/
│       ├── base.py             # BaseLLMAgent (context, relevance filter)
│       ├── anthropic.py        # Anthropic Claude (text)
│       ├── anthropic_vision.py # Anthropic Claude (image-to-text)
│       ├── openai.py           # OpenAI GPT (text, Responses API)
│       ├── openai_image.py     # OpenAI (text-to-image, image-to-text)
│       ├── google.py           # Google Gemini (text)
│       ├── google_image.py     # Google Gemini (text-to-image, image-to-text)
│       ├── google_music.py     # Google Lyria (text-to-music)
│       ├── huggingface.py      # HuggingFace (text)
│       ├── image.py            # HuggingFace (text-to-image)
│       ├── video.py            # HuggingFace (text-to-video)
│       ├── showrunner.py       # All provider orchestrators + ShowRunnerAgent
│       ├── spawn_tools.py      # Registry-driven tool definitions for spawning
│       └── ...                 # HF perception agents
└── renderer/
    ├── terminal.py             # Rich terminal output
    └── gradio_ui.py            # Gradio web UI
examples/
├── run_orchestrator.sh         # Anthropic orchestrator — 80s skate romantic comedy
├── romanticomedy.sh            # HF orchestrator with full worker lineup
├── google_floor.sh             # Full Google multi-modal floor
└── claude_floor.sh             # Cross-provider Claude + Google floor
```

---

## License

Apache-2.0
