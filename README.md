# OFPlaygorund

A CLI tool for running multi-party AI conversations using the [Open Floor Protocol (OFP)](https://github.com/open-voice-interoperability/openfloor-python). Spawn local LLM agents and remote OFP agents, pick a floor policy, and watch them talk.

**GitHub:** https://github.com/bladeszasza/OFPlaygorund

---

## Features

- **Multi-agent conversations** — mix human input with LLM agents from multiple providers
- **Open Floor Protocol** — structured turn-taking with floor request/grant/yield mechanics
- **Four floor policies** — sequential, round-robin, moderated, free-for-all
- **Four LLM providers** — Anthropic Claude, OpenAI GPT, Google Gemini, HuggingFace Inference API
- **Remote OFP agents** — connect any live OFP-compatible HTTP endpoint with `--remote`
- **Autonomous mode** — run agent-only debates with `--no-human --topic`
- **Dynamic agent management** — `/spawn` and `/kick` agents mid-conversation
- **Rich terminal UI** — per-agent colors, timestamps, floor status

---

## Installation

```bash
git clone https://github.com/bladeszasza/OFPlaygorund
cd OFPlaygorund

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

Load before running:

```bash
export $(grep -v '^#' .env | xargs)
```

Keys are also read from `~/.ofp-playground/config.toml` under `[api_keys]`.

---

## Quick Start

### Interactive session (human + one AI agent)

```bash
ofp-playground start --agent hf:Assistant:You are a helpful assistant.
```

### Two AI agents debating (no human)

```bash
ofp-playground start --no-human \
  --topic "Is remote work better than office work?" \
  --max-turns 20 \
  --agent "hf:Optimist:You believe remote work is superior." \
  --agent "hf:Skeptic:You believe office work fosters better collaboration."
```

---

## Agent Spec Formats

Both formats are equivalent and can be mixed freely.

### Colon format

```
type:name[:description[:model]]
```

```bash
--agent hf:Alice:You are a marine biologist.
--agent hf:Bob:You are a skeptical physicist.:meta-llama/Llama-3.1-8B-Instruct
--agent anthropic:Claude:You are a helpful assistant.:claude-haiku-4-5-20251001
```

### Flag format

```
-provider TYPE -name NAME [-system DESCRIPTION] [-model MODEL]
```

```bash
--agent "-provider hf -name Alice -system You are a marine biologist."
--agent "-provider hf -name Bob -system You are a skeptical physicist. -model meta-llama/Llama-3.1-8B-Instruct"
--agent "-provider anthropic -name Claude -system You are a helpful assistant."
```

---

## LLM Providers

| Type alias | Provider | Default model | Env var |
|---|---|---|---|
| `anthropic` / `claude` | Anthropic | `claude-haiku-4-5-20251001` | `ANTHROPIC_API_KEY` |
| `openai` / `gpt` | OpenAI | `gpt-4o-mini` | `OPENAI_API_KEY` |
| `google` / `gemini` | Google | `gemini-2.0-flash-lite` | `GOOGLE_API_KEY` |
| `huggingface` / `hf` | HuggingFace Inference API | `meta-llama/Llama-3.2-1B-Instruct` | `HF_API_KEY` |

Default models are the smallest/cheapest available. Override per-agent with the `model` field in either spec format.

**Confirmed working HuggingFace models:**
- `meta-llama/Llama-3.2-1B-Instruct` — fastest, lightweight
- `meta-llama/Llama-4-Scout-17B-16E-Instruct` — Llama 4, good reasoning
- `meta-llama/Llama-4-Maverick-17B-128E-Instruct` — Llama 4, long context
- `Qwen/Qwen3-235B-A22B` — large MoE, may queue on free tier
- `deepseek-ai/DeepSeek-V3-0324` — strong reasoning, strips `<think>` blocks automatically
- `openai/gpt-oss-20b` — OpenAI open-source on HuggingFace

---

## Remote OFP Agents

Connect any live OFP-compatible HTTP endpoint with `--remote`. The agent will participate in the conversation using floor protocol.

```bash
ofp-playground start --no-human \
  --topic "Your topic here" \
  --agent "hf:Alice:You are Alice." \
  --remote "https://parrot-agent.openfloor.dev/" \
  --remote "https://yahandhjjf.us-east-1.awsapprunner.com/"
```

**Known live OFP agents:**

| Name | URL | Description |
|------|-----|-------------|
| Talker | `https://bladeszasza-talker.hf.space/ofp` | Qwen3-0.6B conversational agent |
| Parrot | `https://parrot-agent.openfloor.dev/` | Echoes everything back |
| Wikipedia | `https://yahandhjjf.us-east-1.awsapprunner.com/` | Encyclopedic research via Wikipedia |

---

## Floor Policies

| Policy | Behaviour |
|---|---|
| `sequential` | Agents take turns in the order they joined |
| `round_robin` | Strict rotation through all registered agents |
| `moderated` | Agents request the floor; moderator grants |
| `free_for_all` | Anyone can speak at any time |

```bash
ofp-playground start --policy round_robin --agent ...
```

---

## CLI Reference

### `ofp-playground start`

```
Options:
  -p, --policy TEXT          Floor policy: sequential, round_robin, moderated, free_for_all
  -a, --agent TYPE:NAME...   Pre-spawn an agent (repeatable)
  -r, --remote URL           Connect to a remote OFP agent via HTTP (repeatable)
  --no-human                 Run without human input (autonomous mode)
  -t, --topic TEXT           Seed topic to start the conversation
  -n, --max-turns INT        Stop automatically after N utterances
  -v, --verbose              Enable debug logging
```

### `ofp-playground agents`

List available agent types and required environment variables.

### `ofp-playground validate <file>`

Validate an OFP envelope JSON file.

---

## In-Conversation Commands

When running with a human agent, these slash commands are available:

| Command | Description |
|---|---|
| `/help` | Show all commands |
| `/agents` | List active agents and floor holder |
| `/floor` | Show current floor holder and queue |
| `/history [N]` | Show last N utterances (default 10) |
| `/spawn <type> <name> [desc] [model]` | Add a new agent mid-conversation |
| `/kick <name>` | Remove an agent from the conversation |
| `/quit` | End the session |

---

## Example: 8-Agent Skateboarding Debate

```bash
ofp-playground start --no-human \
  --topic "Skateboarding on streets VS in the park: which is better?" \
  --max-turns 30 \
  --policy round_robin \
  --agent "-provider hf -name UrbanArchitect -system You are an urban architect who values public space design. -model meta-llama/Llama-3.1-8B-Instruct" \
  --agent "-provider hf -name StreetSkater -system You are a passionate street skater who loves urban spots." \
  --agent "-provider hf -name ParkSkater -system You are a competitive park skater who trains at skate parks." \
  --agent "-provider hf -name Designer -system You are a skate park designer focused on safety and creativity. -model meta-llama/Llama-3.1-8B-Instruct" \
  --agent "-provider hf -name MarketingPro -system You are a sports marketing professional. -model meta-llama/Llama-3.1-8B-Instruct" \
  --agent "-provider hf -name CameraMan -system You are a skate videographer who documents street and park skating." \
  --agent "-provider hf -name Physio -system You are a physiotherapist who treats skaters for injuries." \
  --agent "-provider hf -name SoundGuy -system You are a musician and sound designer for skate videos. -model meta-llama/Llama-3.1-8B-Instruct"
```

---

## Project Structure

```
src/ofp_playground/
├── cli.py                  # Click CLI entry point
├── config/settings.py      # Settings + API key resolution
├── bus/message_bus.py      # Async in-process message bus
├── floor/
│   ├── manager.py          # Floor coordinator (receives all messages)
│   ├── policy.py           # Floor policies (sequential, round_robin, ...)
│   └── history.py          # Bounded conversation history
├── agents/
│   ├── base.py             # BasePlaygroundAgent (OFP envelope handling)
│   ├── human.py            # Human stdin/stdout agent
│   ├── remote.py           # Remote OFP agent via HTTP
│   └── llm/
│       ├── base.py         # BaseLLMAgent (context, relevance filter)
│       ├── anthropic.py    # Anthropic Claude
│       ├── openai.py       # OpenAI GPT
│       ├── google.py       # Google Gemini
│       └── huggingface.py  # HuggingFace Inference API
└── renderer/terminal.py    # Rich terminal output
```

---

## License

Apache-2.0
