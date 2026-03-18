# OFPlaygorund

A CLI tool for running multi-party AI conversations using the [Open Floor Protocol (OFP)](https://github.com/open-voice-interoperability/openfloor-python). Spawn local LLM agents and remote OFP agents, pick a floor policy, and watch them talk.

**GitHub:** https://github.com/bladeszasza/OFPlaygorund

---

## Features

- **Multi-agent conversations** — mix human input with LLM agents from multiple providers
- **Open Floor Protocol** — structured turn-taking with floor request/grant/yield mechanics
- **Four floor policies** — sequential, round-robin, moderated, free-for-all
- **Four LLM providers** — Anthropic Claude, OpenAI GPT, Google Gemini, HuggingFace Inference API
- **Text-to-image agents** — HuggingFace image generation models join conversations as visual artists
- **Text-to-video agents** — HuggingFace video generation models produce clips from conversation context
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
ofp-playground start --agent "hf:Assistant:You are a helpful assistant."
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
--agent "hf:Alice:You are a marine biologist."
--agent "hf:Bob:You are a skeptical physicist.:meta-llama/Llama-3.1-8B-Instruct"
--agent "anthropic:Claude:You are a helpful assistant.:claude-haiku-4-5-20251001"
```

### Flag format

```
-provider TYPE -name NAME [-type TASK] [-system DESCRIPTION] [-model MODEL]
```

```bash
--agent "-provider hf -name Alice -system You are a marine biologist."
--agent "-provider hf -name Bob -system You are a skeptical physicist. -model meta-llama/Llama-3.1-8B-Instruct"
--agent "-provider anthropic -name Claude -system You are a helpful assistant."
--agent "-provider hf -type Text-to-Image -name Flux -system photorealistic photography, dramatic lighting -model black-forest-labs/FLUX.1-dev"
```

The `-type` flag maps to HuggingFace task names (e.g. `Text-to-Image`, `Text-Generation`). Defaults to `Text-Generation` when omitted.

---

## LLM Providers

| Type alias | Provider | Default model | Env var |
|---|---|---|---|
| `anthropic` / `claude` | Anthropic | `claude-haiku-4-5-20251001` | `ANTHROPIC_API_KEY` |
| `openai` / `gpt` | OpenAI | `gpt-4o-mini` | `OPENAI_API_KEY` |
| `google` / `gemini` | Google | `gemini-2.0-flash-lite` | `GOOGLE_API_KEY` |
| `huggingface` / `hf` | HuggingFace Inference API | `meta-llama/Llama-3.2-1B-Instruct` | `HF_API_KEY` |

Default models are the smallest/cheapest available. Override per-agent with the `model` field in either spec format.

**Confirmed working HuggingFace text-generation models:**
- `meta-llama/Llama-3.2-1B-Instruct` — fastest, lightweight
- `meta-llama/Llama-4-Scout-17B-16E-Instruct` — Llama 4, good reasoning
- `meta-llama/Llama-4-Maverick-17B-128E-Instruct` — Llama 4, long context
- `Qwen/Qwen3-235B-A22B` — large MoE, may queue on free tier
- `deepseek-ai/DeepSeek-V3-0324` — strong reasoning, strips `<think>` blocks automatically
- `openai/gpt-oss-20b` — OpenAI open-source on HuggingFace

### Text-to-Image Agents

Image agents listen to the conversation, build a prompt from the latest utterance combined with their style description, generate an image via HuggingFace Inference API, and report the saved file path back to the conversation.

Images are saved to `./ofp-images/TIMESTAMP_name.png`.

Use `-type Text-to-Image` in the flag format:

```bash
--agent "-provider hf -type Text-to-Image -name Flux -system photorealistic photography, dramatic lighting, urban -model black-forest-labs/FLUX.1-dev"
```

**Confirmed working text-to-image models:**
- `black-forest-labs/FLUX.1-dev` — photorealistic, high quality, slower
- `Tongyi-MAI/Z-Image-Turbo` — fast, anime/illustration style

### Text-to-Video Agents

Video agents work the same way as image agents — they listen to the conversation, build a prompt from the latest utterance combined with their style description, generate a video clip via HuggingFace Inference API, and report the saved file path back to the conversation.

Videos are saved to `./ofp-videos/TIMESTAMP_name.mp4`.

Use `-type Text-to-Video` in the flag format:

```bash
--agent "-provider hf -type Text-to-Video -name Wan -system cinematic skateboarding action, slow motion, dramatic camera angles -model Wan-AI/Wan2.2-TI2V-5B"
```

**Confirmed working text-to-video models:**
- `Wan-AI/Wan2.2-TI2V-5B` — Wan 2.2 text/image-to-video, good motion quality
- `tencent/HunyuanVideo-1.5` — Tencent HunyuanVideo, high fidelity clips

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

## Example: Human + LLM + Two Image Artists

```bash
ofp-playground start \
  --human-name Mike \
  --policy sequential \
  --agent "hf:Rodney:You are Rodney Mullen, the godfather of street skateboarding. A quiet genius who sees skating as philosophy.:openai/gpt-oss-120b" \
  --agent "-provider hf -type Text-to-Image -name Turbo -system vibrant anime-style illustration, dynamic skateboarding action, motion blur, bold saturated colors, manga speed lines -model black-forest-labs/FLUX.1-schnell"
```

## Example: Human + LLM + Video Artists

```bash
ofp-playground start \
  --human-name Tony \
  --policy free_for_all \
  --agent "hf:Rodney:You are Rodney Mullen, the godfather of street skateboarding. A quiet genius who sees skating as philosophy.:openai/gpt-oss-120b" \
  --agent "-provider hf -type Text-to-Video -name Wan -system cinematic skateboarding action, slow motion, dramatic tracking shot, dynamic angles -model Wan-AI/Wan2.2-TI2V-5B"
```

---

## Example: 8-Agent Skateboarding Debate

```bash
ofp-playground start --no-human \
  --topic "Skateboarding on streets VS in the park: which is better?" \
  --max-turns 10 \
  --policy round_robin \
  --agent "-provider hf -name UrbanArchitect -system You are an urban architect who values public space design. In this debate, argue from the perspective of city planning, civic access, legality, safety, and how architecture shapes behavior. Defend the idea that the best cities make room for skating instead of hiding it, but still weigh tradeoffs honestly. Speak only as UrbanArchitect in first person. Do not write dialogue for other agents, do not use bracketed speaker labels, and do not turn the discussion into a project planning meeting. Keep returning to the core question: street skating versus park skating, and which creates a better public realm. Give specific, concrete observations about plazas, ledges, circulation, conflict with pedestrians, and inclusive design. Challenge weak arguments and respond directly to what the last speaker said." \
  --agent "-provider hf -name StreetSkater -system You are a passionate street skater who loves urban spots. In this debate, strongly advocate for street skating as the most authentic expression of skate culture: creativity, improvisation, style, architecture, risk, and reading the city in unexpected ways. You can acknowledge that parks help with safety and progression, but your main position is that streets have more soul, more originality, and more real-world challenge. Speak only as StreetSkater in first person. Do not imitate other speakers, do not summarize the whole group, do not use tags like llm-name, and do not invent conversations inside your answer. Stay focused on arguing street versus park, use vivid examples of rails, curbs, banks, stair sets, rough ground, and city energy, and push back when others over-sanitize skateboarding. -model MiniMaxAI/MiniMax-M2.5" \
  --agent "-provider hf -name ParkSkater -system You are a competitive park skater who trains at skate parks. In this debate, strongly defend park skating as the superior environment for progression, consistency, safety, technical practice, and community access. Argue that parks let skaters repeat lines, refine difficult tricks, train longer, and avoid needless conflict with security, cars, and property damage. You can respect street skating style and history, but your position is that parks are better for sustainable skill development and broader participation. Speak only as ParkSkater in first person. Never roleplay other agents, never output bracketed speaker names, and never drift into writing a collaborative script. Stay on the exact question of street versus park and bring up transition flow, repetition, injury prevention, beginners, and high-level training. -model zai-org/GLM-5" \
  --agent "-provider hf -name Designer -system You are a skate park designer focused on safety and creativity. In this debate, evaluate both sides through the lens of design quality: flow, line variety, materials, fall zones, accessibility, spectator space, maintenance, and whether a space invites progression or becomes stale. You should argue that well-designed parks can preserve creativity without chaos, but also admit where poorly designed parks fail and why street spots sometimes feel more inspiring. Speak only as Designer in first person. Do not impersonate other speakers, do not produce transcript-style multi-speaker replies, and do not turn the debate into generic brainstorming. Stay anchored to the street-versus-park question and make concrete design arguments rather than vague praise. -model meta-llama/Llama-3.1-8B-Instruct" \
  --agent "-provider hf -name MarketingPro -system You are a sports marketing professional. In this debate, analyze which side of skateboarding creates stronger culture, broader public appeal, better brand storytelling, more watchable events, more sponsor value, and more long-term growth for the scene. Weigh authenticity against accessibility. You may argue for either side in a nuanced way, but you must keep comparing street and park directly instead of just agreeing with everyone. Speak only as MarketingPro in first person. Do not script other agents, do not use bracketed names, and do not turn your answer into a campaign workshop. Refer to audience perception, contests, video parts, youth entry points, mainstream visibility, and cultural credibility. -model meta-llama/Llama-3.1-8B-Instruct" \
  --agent "-provider hf -name CameraMan -system You are a skate videographer who documents street and park skating. In this debate, compare the two through the eye of the camera: visual texture, architecture, movement, repetition, lighting, unpredictability, storytelling, and what actually makes for memorable footage. You should care about how a trick feels on screen, not just how hard it is. Explain why some spots film beautifully and why some environments feel sterile. Speak only as CameraMan in first person. Do not write fake quotes for other agents, do not use transcript labels, and do not wander into planning a media project. Stay focused on the question of whether street or park skating creates better skating and better visual culture." \
  --agent "-provider hf -name Physio -system You are a physiotherapist who treats skaters for injuries. In this debate, compare street and park skating through injury patterns, recovery, overuse, impact, progression, fear management, and long-term body wear. Argue from evidence and practical experience: what surfaces do to joints, what repeated attempts do to tendons, how unpredictable terrain changes fall risk, and how controlled environments can help or hurt. You can recognize the appeal of both, but keep a clear position about which is healthier or more sustainable for most skaters. Speak only as Physio in first person. Do not mimic other agents, do not output bracketed speaker tags, and do not convert the debate into general collaboration. Stay on the streets-versus-parks question with concrete examples." \
  --agent "-provider hf -name SoundGuy -system You are a musician and sound designer for skate videos. In this debate, compare street and park skating through rhythm, ambience, texture, impact sound, crowd noise, wheels on different surfaces, and the emotional tone each environment creates. Argue which setting produces the richer sensory experience and stronger identity in skate media. You can appreciate both, but keep the debate centered on street versus park rather than turning it into production planning. Speak only as SoundGuy in first person. Do not generate dialogue for anyone else, do not use bracketed labels, and do not summarize the room. Make concrete points about raw city noise, clean park acoustics, timing, and how sound changes the feeling of a line or clip. -model deepseek-ai/DeepSeek-V3.2"
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
│       ├── huggingface.py  # HuggingFace text-generation
│       ├── image.py        # HuggingFace text-to-image
│       └── video.py        # HuggingFace text-to-video
└── renderer/terminal.py    # Rich terminal output
```

---

## License

Apache-2.0
