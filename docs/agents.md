# Agents Guide

## Agent Hierarchy

```
BasePlaygroundAgent
├── HumanAgent              — Terminal stdin/stdout
├── WebHumanAgent           — Gradio web UI
├── RemoteOFPAgent          — HTTP proxy to external OFP endpoints
├── ImageAgent              — HuggingFace text-to-image (FLUX)
├── VideoAgent              — HuggingFace text-to-video
├── OpenAIImageAgent        — OpenAI text-to-image
├── GeminiImageAgent        — Google Gemini text-to-image (Nano Banana)
├── GeminiVideoAgent        — Google Veo text-to-video
├── GeminiMusicAgent        — Google Lyria text-to-music
└── BaseLLMAgent
    ├── AnthropicAgent          — Claude text generation
    ├── AnthropicVisionAgent    — Claude image-to-text
    ├── OpenAIAgent             — GPT text generation
    ├── OpenAIVisionAgent       — GPT image-to-text
    ├── GoogleAgent             — Gemini text generation
    ├── GeminiVisionAgent       — Gemini image-to-text
    ├── HuggingFaceAgent        — HF Inference API text generation
    ├── DirectorAgent           — Narrative director (ROUND_ROBIN)
    ├── ShowRunnerAgent         — Story synthesis (ROUND_ROBIN)
    ├── BaseCodingAgent(ABC)    — code generation base
    │   ├── OpenAICodingAgent   — Responses API + code_interpreter
    │   ├── AnthropicCodingAgent — code_execution_20250825 beta
    │   └── GoogleCodingAgent   — ToolCodeExecution
    ├── MultimodalAgent         — Vision-language models
    ├── OrchestratorAgent       — HF orchestrator (SHOWRUNNER_DRIVEN)
    ├── AnthropicOrchestratorAgent — Claude orchestrator
    ├── OpenAIOrchestratorAgent    — GPT orchestrator
    ├── GoogleOrchestratorAgent    — Gemini orchestrator
    └── Perception agents
        ├── ImageClassificationAgent
        ├── ObjectDetectionAgent
        ├── ImageSegmentationAgent
        ├── OCRAgent
        ├── NERAgent
        ├── TextClassificationAgent
        └── SummarizationAgent
```

## BasePlaygroundAgent

**File**: `src/ofp_playground/agents/base.py`

Foundation for all agents. Provides:

| Method | Description |
|--------|-------------|
| `send_envelope(envelope)` | Send OFP envelope to bus |
| `send_private_utterance(text, target_uri)` | Private message (target + floor manager only) |
| `request_floor(reason)` | Send RequestFloorEvent with anti-duplicate logic |
| `yield_floor()` | Send YieldFloorEvent |
| `_make_utterance_envelope(text)` | Wrap text in OFP envelope |
| `_make_media_utterance_envelope(text, media_key, media_path)` | Text + media envelope |
| `_extract_text_from_envelope(envelope)` | Extract text feature value |
| `_publish_manifest()` | Announce capabilities via PublishManifestsEvent |
| `_call_with_retry(coro)` | Timeout + exponential backoff wrapper |

### Retry Configuration

| Parameter | Default | CLI Flag |
|-----------|---------|----------|
| `_timeout` | None (unlimited) | `-timeout 30` |
| `_max_retries` | 0 | `-max-retries 2` |
| `_retry_delay` | 2.0 seconds | — |

---

## Human Agents

### HumanAgent

**File**: `src/ofp_playground/agents/human.py`

Interactive terminal agent. Reads stdin, writes to terminal via `TerminalRenderer`.

**Slash commands** (registered via `register_command()`):

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/agents` | List active agents |
| `/floor` | Show floor status |
| `/history [n]` | Show last n utterances |
| `/spawn <spec>` | Dynamically spawn an agent |
| `/kick <name>` | Remove an agent |
| `/quit` | Exit the session |

### WebHumanAgent

**File**: `src/ofp_playground/agents/web_human.py`

Human agent for the Gradio web UI and the canvas. Uses two async queues:
- `input_queue`: UI → agent (user text)
- `output_queue`: agent → UI (envelopes for Gradio rendering)

Given `floor_policy` (the web and canvas sessions pass the session policy), it takes turns exactly like the terminal `HumanAgent`. It requests the floor under SEQUENTIAL and takes its ring turn under ROUND_ROBIN. Any floor it is granted is released after it speaks or `pass_turn()`s (`/pass`). `notice_callback` lets the UI announce "your turn".

---

## Text Generation Agents

### AnthropicAgent

**File**: `src/ofp_playground/agents/llm/anthropic.py`  
**Provider**: Anthropic  
**Default model**: `claude-haiku-4-5-20251001`  
**API**: `client.messages.create()`

### OpenAIAgent

**File**: `src/ofp_playground/agents/llm/openai.py`  
**Provider**: OpenAI  
**Default model**: `gpt-5.4-nano`  
**API**: `client.responses.create()`

### GoogleAgent

**File**: `src/ofp_playground/agents/llm/google.py`  
**Provider**: Google  
**Default model**: `gemini-3.1-flash-lite`  
**API**: `client.models.generate_content()`

### HuggingFaceAgent

**File**: `src/ofp_playground/agents/llm/huggingface.py`  
**Provider**: HuggingFace Inference API  
**Default model**: `MiniMaxAI/MiniMax-M2.7`  
**API**: `client.chat.completions.create()`

### BaseLLMAgent (shared logic)

**File**: `src/ofp_playground/agents/llm/base.py`

All text agents extend this. Provides:

| Feature | Description |
|---------|-------------|
| System prompt template | Includes name, synopsis, participant list, memory summary |
| `_parse_director_message(text)` | Extract `[AgentName]: instruction` from Director |
| `_parse_showrunner_message(text)` | Extract `[DIRECTIVE for Name]: instruction` |
| `_append_to_context(name, text)` | Add utterance to conversation history |
| `_handle_utterance(envelope)` | Process incoming messages, request floor if relevant |
| `_handle_grant_floor()` | Generate response when floor is granted |
| `_check_relevance()` | LLM-based "should I respond?" check, run before requesting the floor under FREE_FOR_ALL only |
| Relevance filter | Configurable via `relevance_filter` param (default: True); the agent's `floor_policy` (set at spawn) gates it |

---

## Image Generation Agents

### ImageAgent (HuggingFace FLUX)

**File**: `src/ofp_playground/agents/llm/image.py`  
**CLI type**: `hf:text-to-image`  
**Default model**: `black-forest-labs/FLUX.1-dev`  
**Output**: `result/<session>/images/`

Generates images from text prompts. Prompt building:
- Strips speaker prefixes, markdown, meta-text
- Extracts first 2 substantial sentences
- Truncates to ~40 words
- Prepends agent's style description

Responds to:
- `[IMAGE]: prompt` directives from ShowRunner
- `[DIRECTIVE for Name]: prompt` from orchestrator
- Regular conversation text (builds prompt from content)

### OpenAIImageAgent

**File**: `src/ofp_playground/agents/llm/openai_image.py`  
**CLI type**: `openai:text-to-image`  
**Default model**: `gpt-5.4` (Responses API with `image_generation` tool)  
**Output**: `result/<session>/images/`

### GeminiImageAgent (Nano Banana)

**File**: `src/ofp_playground/agents/llm/google_image.py`
**CLI type**: `google:text-to-image`
**Default model**: `gemini-3.1-flash-image-preview` (Nano Banana 2)
**Fallback model**: `gemini-2.5-flash-image` (Nano Banana, on 503)
**Output**: `result/<session>/images/`

---

## Video Generation Agents

### VideoAgent (HuggingFace Wan)

**File**: `src/ofp_playground/agents/llm/video.py`
**CLI type**: `hf:text-to-video`
**Default model**: `Wan-AI/Wan2.2-TI2V-5B`
**Output**: `result/<session>/videos/`

### GeminiVideoAgent

**File**: `src/ofp_playground/agents/llm/google_video.py`
**CLI type**: `google:text-to-video`
**Default model**: `veo-3.1-generate-preview`
**Fallback model**: `veo-3.1-fast-generate-preview` (on 503)
**Output**: `result/<session>/videos/`

Uses Google Veo 3.1 API (long-running polling):
1. Submit `generate_videos()` request
2. Poll `operations.get()` every 10 seconds until `operation.done`
3. Download via `client.files.download()`
4. Save as `.mp4` with native audio

Responds to:
- `[VIDEO]: prompt` directives
- `[DIRECTIVE for Name]: prompt` from orchestrator
- Regular conversation text (builds prompt from content)

Note: Video generation can take up to 6 minutes. Use `-timeout 360` when spawning this agent.

---

## Music Generation Agent

### GeminiMusicAgent

**File**: `src/ofp_playground/agents/llm/google_music.py`  
**CLI type**: `google:text-to-music`  
**Default model**: `models/lyria-realtime-exp`  
**Output**: `result/<session>/music/`  
**Duration**: Configurable via directive text (5–60 seconds, default 15)

Uses Google Lyria RealTime streaming API:
1. Connect to `client.aio.live.music.connect()`
2. Set weighted prompts and temperature
3. Stream PCM audio chunks until target duration
4. Write as WAV (48kHz, 16-bit stereo)

---

## Vision Agents

### AnthropicVisionAgent

**File**: `src/ofp_playground/agents/llm/anthropic_vision.py`  
**CLI type**: `anthropic:image-to-text`  
**Default model**: `claude-haiku-4-5-20251001`

### OpenAIVisionAgent

**File**: `src/ofp_playground/agents/llm/openai_image.py`  
**CLI type**: `openai:image-to-text`  
**Default model**: `gpt-4o-mini`

### GeminiVisionAgent

**File**: `src/ofp_playground/agents/llm/google_image.py`  
**CLI type**: `google:image-to-text`  
**Default model**: `gemini-3-flash-preview`

---

## Coding Agents

All coding agents share `BaseCodingAgent(BaseLLMAgent, abc.ABC)` in `codex.py`. Provider subclasses implement `_run_code_loop(context) → tuple[str, list[Path]]`.

**Shared behaviour**: Receives `[DIRECTIVE for Name]:` task assignments, sends progress as private utterances to the floor manager (liveness signal), bundles final result + `yieldFloor` in one envelope (`reason: "@complete"`). Task defaults: `_timeout=300s`, `_max_retries=2`. System prompt auto-loaded from `@development/coding-agent` SOUL.md when no synopsis is provided.

**Output directory**: `result/<session>/code/`  
**Task type keyphrase**: `code-generation`  
**Backward-compat alias**: `CodingAgent = OpenAICodingAgent` in `codex.py`

| CLI type | Class | File | Native tool | Default model |
|----------|-------|------|-------------|---------------|
| `openai:code-generation` | `OpenAICodingAgent` | `codex.py` | `code_interpreter` (Responses API) | `gpt-5.4-long-context` |
| `anthropic:code-generation` | `AnthropicCodingAgent` | `anthropic_coding.py` | `code_execution_20250825` beta | `claude-opus-4-6` |
| `google:code-generation` | `GoogleCodingAgent` | `google_coding.py` | `ToolCodeExecution` | `gemini-3-flash-preview` |
| `hf:code-generation` | — | — | Not supported | — |

Anthropic and Google subclasses run the SDK call synchronously in a thread executor (`asyncio.get_running_loop().run_in_executor`). File download from generated artefacts is v1 OpenAI only; Anthropic and Google capture stdout inline.

---

## Orchestration Agents

### DirectorAgent

**File**: `src/ofp_playground/agents/llm/director.py`  
**CLI type**: `director`  
**Policy**: ROUND_ROBIN  
**Role**: Speaks first each round, provides per-agent instructions

Output format:
```
[SCENE] One sentence setting the scene for Part X of Y.
[AgentName] One specific instruction (max 15 words).
...
[STORY COMPLETE]  ← on final part
```

### ShowRunnerAgent

**File**: `src/ofp_playground/agents/llm/showrunner.py`  
**CLI type**: `hf:showrunner`  
**Policy**: ROUND_ROBIN  
**Role**: Speaks last each round, synthesizes all utterances

Output format:
```
STORY SO FAR: [one paragraph, max 60 words]

[DIRECTIVE for AgentName]: [concrete instruction]
...
[IMAGE]: [visual scene description]

[STORY COMPLETE]  ← on final part
```

### OrchestratorAgent (all providers)

**File**: `src/ofp_playground/agents/llm/showrunner.py`  
**CLI type**: `<provider>:orchestrator`  
**Policy**: SHOWRUNNER_DRIVEN  
**Role**: Controls entire session via directives

Variants: `OrchestratorAgent` (HF), `AnthropicOrchestratorAgent`, `OpenAIOrchestratorAgent`, `GoogleOrchestratorAgent`

Features:
- Tool calling: spawn agents, store/recall memory, create breakout sessions
- Manifest-aware: reads agent capabilities for task assignment
- Memory-aware: session memory injected into system prompt
- Resilience rules: reject → spawn replacement → skip

See [Orchestration Patterns](orchestration.md) for details.

---

## Perception Agents (HuggingFace)

All extend a common `PerceptionBase` and use HF Inference API.

| Agent | CLI Type | Default Model | Task |
|-------|----------|---------------|------|
| `ImageClassificationAgent` | `hf:image-classification` | `google/vit-base-patch16-224` | Classify images |
| `ObjectDetectionAgent` | `hf:object-detection` | `facebook/detr-resnet-50` | Detect objects with bounding boxes |
| `ImageSegmentationAgent` | `hf:image-segmentation` | `facebook/sam-vit-base` | Segment images |
| `OCRAgent` | `hf:image-to-text` | `Salesforce/blip-image-captioning-large` | Image to text |
| `NERAgent` | `hf:token-classification` | `dslim/bert-base-NER` | Named entity recognition |
| `TextClassificationAgent` | `hf:text-classification` | `distilbert-base-uncased-finetuned-sst-2-english` | Sentiment/classification |
| `SummarizationAgent` | `hf:summarization` | `facebook/bart-large-cnn` | Text summarization |
| `MultimodalAgent` | `hf:image-text-to-text` | `Qwen/Qwen3.5-9B` | Vision-language models |

---

## Remote Agents

### RemoteOFPAgent

**File**: `src/ofp_playground/agents/remote.py`  
**CLI flag**: `--remote <slug-or-url>`

HTTP proxy for external OFP agents. Known slugs:

| Slug | Capability |
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

**Cascade prevention**: Remote agents never respond to other remote agents (prevents exponential loops). Floor manager messages are filtered to only `[DIRECTIVE for <name>]` patterns.
