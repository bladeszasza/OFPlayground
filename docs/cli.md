# CLI Reference

## Commands

### `ofp-playground start`

Start an interactive or autonomous conversation session.

```bash
ofp-playground start [OPTIONS]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--policy` | Floor policy | `sequential` |
| `--agent` | Agent spec (repeatable) | — |
| `--remote` | Remote agent slug or URL (repeatable) | — |
| `--topic` | Seed topic / mission for the conversation | — |
| `--max-turns` | Stop after N agent utterances (counted by the floor manager; any N) | — |
| `--no-human` | Run without human participant | `False` |
| `--human-name` | Display name for human | `User` |
| `--show-floor-events` | Show floor control events in terminal | `False` |
| `-v`, `--verbose` | Enable debug logging | `False` |

### `ofp-playground web`

Launch the Gradio web UI for the conversation. Accepts the same session options as `start`: `--policy`, `--agent`, `--remote`, `--topic`, `--max-turns`, `--no-human`, `--human-name`, `--show-floor-events` and `-v`. It also takes:

| Option | Description | Default |
|--------|-------------|---------|
| `--host` | Server host (loopback only in this local profile) | `127.0.0.1` |
| `--port` | Server port | `7860` |
| `--share` | Rejected: public sharing needs an authenticated deployment profile | `False` |

Floor and orchestrator events (assignments, round boundaries, auto-accepts, the final manuscript) appear in the web chat. Floor commands typed into the chat box (`/grant`, `/pass`, `/floor`, `/agents`) behave as in the terminal.

### `ofp-playground canvas`

Launch the visual canvas (FastAPI + React, `pip install -e ".[canvas]"`). `--policy`, `--agent`, `--remote`, `--topic`, `--max-turns`, `--no-human`, `--human-name` and `--show-floor-events` pre-populate the graph. Agent `-timeout` / `-max-retries` / `-max-tokens` flags are carried as node fields. Press Run to start. `--host` must be a loopback address; `--port` defaults to `8765`; `--open/--no-open` controls the browser.

### `ofp-playground agents` / `personas` / `models` / `status`

| Command | Purpose |
|---------|---------|
| `agents` | Provider / task help for agent specs |
| `personas [SEARCH] [--preview] [--json]` | Search the `@category/slug` persona library; `--preview` shows the resolved prompt |
| `models [--json]` | Configured provider model choices (never prints keys) |
| `status [--watch] [--json] [--agent NAME]` | Read authenticated ASP status observations (see [ASP](asp.md)) |

### `ofp-playground validate`

Validate an OFP envelope JSON file.

```bash
ofp-playground validate <file.json>
```

---

## Agent Spec Formats

### Colon Format

```
type:name[:description[:model]]
type:subtype:name[:description[:model]]
```

**Examples**:
```bash
# Simple text agent
--agent "anthropic:Alice:You are a curious scientist."

# Text agent with model override
--agent "openai:Bob:You are a skeptical engineer.:gpt-5.4-2026-03-05"

# Image agent (type:subtype:name)
--agent "hf:text-to-image:Painter:Storyboard artist"

# Orchestrator
--agent "anthropic:orchestrator:Director:You drive the pipeline.:claude-sonnet-4-6"

# Coding agent
--agent "openai:code-generation:CodeFixer:You are a coding agent. Implement the assigned task."
```

### Flag Format

```
-provider TYPE -name NAME [-system DESC] [-model MODEL] [-type TASK]
  [-max-tokens N] [-timeout SECONDS] [-max-retries N]
```

**Examples**:
```bash
# Basic text agent
--agent "-provider anthropic -name Alice -system You are a scientist."

# With model and timeout
--agent "-provider openai -name Bob -model gpt-5.4-2026-03-05 -timeout 30 -max-retries 2"

# HF task type
--agent "-provider hf -type text-to-image -name Painter -system Storyboard artist"
```

### Supported Agent Types

#### Provider Prefixes

| Prefix | Provider |
|--------|----------|
| `anthropic`, `claude` | Anthropic Claude |
| `openai`, `gpt` | OpenAI GPT |
| `google`, `gemini` | Google Gemini |
| `hf`, `huggingface` | HuggingFace Inference API |
| `director` | Director (HF, ROUND_ROBIN) |

#### Task Subtypes

| Subtype | Description | Providers |
|---------|-------------|-----------|
| (none) / `text-generation` | Text generation | All |
| `text-to-image` | Image generation | OpenAI, Google, HF |
| `image-to-text` | Vision / OCR | Anthropic, OpenAI, Google, HF |
| `text-to-video` | Video generation | HF |
| `text-to-music` | Music generation (Lyria) | Google (HF is rejected with an error) |
| `image-text-to-text` | Multimodal VLM | HF |
| `image-classification` | Image classification | HF |
| `object-detection` | Object detection | HF |
| `image-segmentation` | Image segmentation | HF |
| `token-classification` | Named entity recognition | HF |
| `text-classification` | Sentiment / text classification | HF |
| `summarization` | Text summarization | HF |
| `orchestrator` | Orchestrator (SHOWRUNNER_DRIVEN) | All |
| `showrunner` | ShowRunner (ROUND_ROBIN) | HF |
| `code-generation` | Coding agent with native code execution | Anthropic, OpenAI, Google (HF unsupported) |

---

## Remote Agents

```bash
# By slug
--remote polly
--remote arxiv
--remote wikipedia

# By URL
--remote https://custom-ofp-agent.example.com/api/ofp
```

### Known Slugs

| Slug | Description |
|------|-----------|
| `polly` | Echo agent |
| `arxiv` | Research paper analysis |
| `github` | GitHub repo analyst |
| `sec` | SEC filing analyst |
| `web-search` | Web search specialist |
| `wikipedia` / `wiki` | Wikipedia research |
| `stella` | NASA astronomy images |
| `verity` | Hallucination detector |
| `profanity` | Content moderation |

---

## Slash Commands (In-Conversation)

Available to the terminal human in `start`:

| Command | Description |
|---------|-------------|
| `/help` | Show command reference |
| `/agents` | List participants (🎤 marks floor holders) |
| `/floor` | Show the floor holder(s) and the waiting queue |
| `/grant <name>` | Give an agent the floor (MODERATED chair; an override in any policy) |
| `/pass` | Give up a turn you hold without speaking |
| `/history [n]` | Show last n utterances (default: 10) |
| `/spawn <type> <name> [description] [model]` | Quick spawn; arguments are whitespace-split, so use `/add` for multi-word prompts. `/spawn remote <slug-or-url>` connects a remote |
| `/add <agent spec>` | Spawn from a full `--agent` spec (colon or flag format, incl. `-timeout` etc.) |
| `/connect <slug-or-url>` | Connect a remote OFP agent |
| `/kick <name>` / `/remove <name> [graceful\|force]` | Remove an agent |
| `/status [json]`, `/watch [s]`, `/quarantine`, `/review`, `/clear`, `/personas`, `/models` | ASP status and governance (see [ASP](asp.md)) |
| `/quit` | Exit the session |

`/grant`, `/pass`, `/floor` and `/agents` are shared floor commands (`SessionService.floor_command`). They work the same when typed into the web chat box or the canvas human input. See [Interfaces](interfaces.md) for the full CLI / web / canvas mapping.

**Examples**:
```
/add -provider openai -name Reviewer -timeout 60 -system You review code.
/spawn anthropic Editor
/connect polly
/grant Reviewer
/kick Editor
/history 20
```

---

## Floor Policy Values

| Value | Description |
|-------|-------------|
| `sequential` | First-come, first-served FIFO turns |
| `round_robin` | Strict ring; director opens / show runner closes each round |
| `moderated` | Human chair calls on agents by name or `/grant` (FIFO without a human) |
| `free_for_all` | Everyone speaks immediately (relevance-filtered) |
| `showrunner_driven` | Orchestrator-controlled pipeline |

See [Floor Policies](floor-policies.md) for detailed behavior.

---

## Examples

### Simple two-agent conversation

```bash
ofp-playground start \
  --agent "anthropic:Alice:You are a curious scientist." \
  --agent "openai:Bob:You are a skeptical engineer." \
  --topic "Should we colonize Mars?"
```

### Autonomous round-robin story

```bash
ofp-playground start --no-human --policy round_robin \
  --agent "director:Narrator:A mystery story in 5 parts." \
  --agent "anthropic:Detective:You are a hard-boiled detective." \
  --agent "openai:Witness:You are a nervous witness." \
  --topic "A diamond has been stolen from the museum."
```

### Production pipeline (SHOWRUNNER_DRIVEN)

```bash
ofp-playground start --no-human --policy showrunner_driven \
  --agent "anthropic:orchestrator:Director:Drive a 3-step pipeline: 1) Writer drafts, 2) Painter illustrates, 3) TASK_COMPLETE" \
  --agent "openai:Writer:You write compelling narratives." \
  --agent "hf:text-to-image:Painter:Digital art style" \
  --topic "Create a short story about a robot discovering music."
```

### With remote agents

```bash
ofp-playground start \
  --agent "anthropic:Alice:You are a research assistant." \
  --remote arxiv \
  --remote wikipedia \
  --topic "What are the latest advances in quantum computing?"
```

### Web UI

```bash
ofp-playground web --policy free_for_all \
  --agent "anthropic:Alice:Curious scientist" \
  --agent "openai:Bob:Skeptical engineer" \
  --port 8080
```
