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
| `--max-turns` | Stop after N utterances | — |
| `--no-human` | Run without human participant | `False` |
| `--human-name` | Display name for human | `User` |
| `--show-floor-events` | Show floor control events in terminal | `False` |
| `-v`, `--verbose` | Enable debug logging | `False` |

### `ofp-playground web`

Launch Gradio web UI for the conversation.

```bash
ofp-playground web [OPTIONS]
```

Same options as `start`, plus:

| Option | Description | Default |
|--------|-------------|---------|
| `--host` | Server host | `0.0.0.0` |
| `--port` | Server port | `7860` |
| `--share` | Create public Gradio share link | `False` |

### `ofp-playground agents`

List all available agent types and their CLI specs.

```bash
ofp-playground agents
```

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

# Web page generator
--agent "openai:web-page-generation:WebShowcase:Generate HTML pages"
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
| `text-to-music` | Music generation | Google |
| `image-text-to-text` | Multimodal VLM | HF |
| `image-classification` | Image classification | HF |
| `object-detection` | Object detection | HF |
| `image-segmentation` | Image segmentation | HF |
| `token-classification` | Named entity recognition | HF |
| `text-classification` | Sentiment / text classification | HF |
| `summarization` | Text summarization | HF |
| `orchestrator` | Orchestrator (SHOWRUNNER_DRIVEN) | All |
| `showrunner` | ShowRunner (ROUND_ROBIN) | HF |
| `web-page-generation` | HTML page generator | All |
| `web-page` | Alias for web-page-generation | All |
| `web-showcase` | Legacy alias for web-page-generation | All |

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

Available during interactive sessions:

| Command | Description |
|---------|-------------|
| `/help` | Show command reference |
| `/agents` | List active agents with URIs |
| `/floor` | Show current floor holder and policy |
| `/history [n]` | Show last n utterances (default: 10) |
| `/spawn <spec>` | Dynamically spawn an agent (same spec format as `--agent`) |
| `/kick <name>` | Remove an agent from the conversation |
| `/quit` | Exit the session |

**Examples**:
```
/spawn anthropic:Editor:You edit text for clarity.
/spawn -provider openai -name Reviewer -system "You review code."
/kick Editor
/history 20
```

---

## Floor Policy Values

| Value | Description |
|-------|-------------|
| `sequential` | FIFO turn queue |
| `round_robin` | Strict rotation |
| `moderated` | Manual floor grants |
| `free_for_all` | Everyone speaks freely |
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
