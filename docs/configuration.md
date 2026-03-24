# Configuration Guide

## API Keys

API keys are resolved in this order:

1. **Environment variables** (highest priority)
2. **`.env` file** in the project root
3. **Config file** at `~/.ofp-playground/config.toml`
4. **Interactive prompt** (if key is missing and agent requires it)

### Environment Variables

| Variable | Provider |
|----------|----------|
| `ANTHROPIC_API_KEY` | Anthropic Claude |
| `OPENAI_API_KEY` | OpenAI GPT |
| `GOOGLE_API_KEY` | Google Gemini |
| `HF_API_KEY` | HuggingFace Inference API |

### `.env` File

Place a `.env` file in the project root:

```bash
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=AIza...
HF_API_KEY=hf_...
```

The CLI loads this automatically (no `python-dotenv` dependency required).

### Config File

`~/.ofp-playground/config.toml`:

```toml
[api_keys]
anthropic = "sk-ant-..."
openai = "sk-..."
google = "AIza..."
huggingface = "hf_..."

[defaults]
llm_model_anthropic = "claude-haiku-4-5-20251001"
llm_model_openai = "gpt-5.4-nano"
llm_model_google = "gemini-3.1-flash-lite-preview"
llm_model_huggingface = "MiniMaxAI/MiniMax-M2.5"
relevance_filter = true

[floor]
policy = "sequential"
max_agents = 10
timeout_seconds = 30
```

---

## Model Defaults

### Text Generation

| Provider | Default Model |
|----------|---------------|
| Anthropic | `claude-haiku-4-5-20251001` |
| OpenAI | `gpt-5.4-nano` |
| Google | `gemini-3.1-flash-lite-preview` |
| HuggingFace | `MiniMaxAI/MiniMax-M2.5` |

### Vision (Image-to-Text)

| Provider | Default Model |
|----------|---------------|
| Anthropic | `claude-haiku-4-5-20251001` |
| OpenAI | `gpt-4o-mini` |
| Google | `gemini-3-flash-preview` |

### Image Generation

| Provider | Default Model |
|----------|---------------|
| OpenAI | `gpt-4o` (Responses API) |
| Google | `gemini-3.1-flash-image-preview` (fallback: `gemini-2.5-flash-image`) |
| HuggingFace | `black-forest-labs/FLUX.1-dev` |

### Other

| Agent | Default Model |
|-------|---------------|
| Music (Google Lyria) | `models/lyria-realtime-exp` |
| Video (HF) | `Wan-AI/Wan2.2-TI2V-5B` |
| WebPageAgent (Anthropic) | `claude-sonnet-4-6` |
| WebPageAgent (OpenAI) | `gpt-5.4-long-context` |
| WebPageAgent (Google) | `gemini-3.1-pro-preview` |
| WebPageAgent (HF) | `deepseek-ai/DeepSeek-V3.2` |

### Overriding Models

Use the model field in agent spec:

```bash
# Colon format (4th field)
--agent "anthropic:Alice:Scientist:claude-sonnet-4-6"

# Flag format
--agent "-provider anthropic -name Alice -model claude-sonnet-4-6"
```

---

## Agent Tuning

### Timeout and Retries

Control per-agent API call behavior:

```bash
--agent "-provider anthropic -name Alice -timeout 30 -max-retries 2"
```

| Parameter | Description | Default |
|-----------|-------------|---------|
| `-timeout` | Seconds per API call | Unlimited |
| `-max-retries` | Extra attempts after first failure | 0 |

Retry logic:
- Retryable errors: 429, 502, 503, 504, rate limit, timeout
- Non-retryable: all other errors (raised immediately)
- Backoff: `2^attempt` seconds, capped at 30s

### Max Tokens

```bash
--agent "-provider hf -name Writer -max-tokens 4096"
```

### Relevance Filter

When enabled (default), text agents ask themselves "should I respond?" before requesting the floor. Useful in FREE_FOR_ALL mode to prevent spam.

Configure in `config.toml`:

```toml
[defaults]
relevance_filter = true   # or false
```

---

## Floor Settings

```toml
[floor]
policy = "sequential"      # Default policy
max_agents = 10            # Maximum concurrent agents
timeout_seconds = 30       # Floor timeout (unused currently)
```

Override at runtime:

```bash
ofp-playground start --policy round_robin
```

---

## Settings Data Model

```python
@dataclass
class FloorConfig:
    policy: str = "sequential"
    max_agents: int = 10
    timeout_seconds: int = 30

@dataclass
class ApiKeysConfig:
    anthropic: Optional[str] = None
    openai: Optional[str] = None
    google: Optional[str] = None
    huggingface: Optional[str] = None

@dataclass
class DefaultsConfig:
    llm_model_anthropic: str = "claude-haiku-4-5-20251001"
    vision_model_anthropic: str = "claude-haiku-4-5-20251001"
    llm_model_openai: str = "gpt-5.4-nano"
    image_model_openai: str = "gpt-4o"
    vision_model_openai: str = "gpt-4o-mini"
    llm_model_google: str = "gemini-3.1-flash-lite-preview"
    image_model_google: str = "gemini-3.1-flash-image-preview"
    vision_model_google: str = "gemini-3-flash-preview"
    music_model_google: str = "models/lyria-realtime-exp"
    llm_model_huggingface: str = "MiniMaxAI/MiniMax-M2.5"
    relevance_filter: bool = True

@dataclass
class Settings:
    floor: FloorConfig
    api_keys: ApiKeysConfig
    defaults: DefaultsConfig
```
