# I Built a Playground for Multi-Agent AI — And It Actually Produces Things

*A personal exploration of the Open Floor Protocol, what "multi-agent conversation" really means in practice.*

---

What I do know is that building it taught me more about how language models actually collaborate — or fail to —.

So. Let's talk about the Open Floor Protocol, the playground I built on top of it, and what happens when you let a bunch of AI agents loose in a room together.

---

## What Is the Open Floor Protocol?

The [Open Floor Protocol](https://openfloor.dev/introduction) (OFP) is a specification for multi-party AI conversations. Think of it as a shared contract that defines how agents — AI or human — take turns speaking, manage floor access, identify themselves, and exchange structured messages.

At its core, OFP defines a few elegant things:

**The Envelope.** Every message in an OFP system is wrapped in a standard envelope that includes who sent it, what conversation it belongs to, and what kind of event it represents. This sounds bureaucratic until you realize it is the thing that makes routing, filtering, and composition possible without custom glue code for every new pair of agents you introduce.

**Events.** OFP has distinct event types: utterances (agents speaking), floor requests (agents asking to speak), floor grants and revocations (the coordinator deciding who goes next), yields (agents voluntarily giving up the floor), manifests (agents announcing their capabilities), and invites and uninvites (agents joining and leaving sessions). This taxonomy is simple enough to hold in your head but rich enough to express the full lifecycle of a structured conversation.

**Manifests.** Each agent publishes a manifest describing what it can do — its name, its role, its input and output modalities. An orchestrator that reads these manifests can make intelligent task assignment decisions: it knows that the image generation agent accepts text and produces images, that the code agent handles code-generation tasks, that the research agent should be sent to arxiv when someone mentions papers. The capability graph is explicit rather than implied by convention.

**The Floor.** This is the elegant conceptual heart of the protocol. "The floor" is the conversational right to speak. Agents must request it, receive it, hold it, and yield it. The entity managing this — the floor manager — enforces whatever policy governs how turns work. This means you can swap policies without touching agent code, and agents don't need to know anything about the policy to participate correctly.

---

## What Is OFP Playground?

[OFP Playground](https://github.com/bladeszasza/OFPlayground) is the thing I built on top of OFP to actually *use* it — to run sessions, explore patterns, test pipelines, and understand through practice what these concepts mean when code is actually running.

The honest framing is this: I wanted a sandbox, not a product. A place where I could ask *what happens if* and get a real answer in minutes instead of hours of infrastructure setup. The fact that the laboratory started producing genuinely interesting outputs was a surprise that I am still processing.

Here is the overall shape of the system.

### The Message Bus

At the center is a pure async message bus. Every agent registers its own asyncio queue on the bus, and the bus routes envelopes based on simple rules: if an envelope has a specific `to` address, it goes to that agent plus the floor manager; if it has no `to` address, it broadcasts to everyone except the sender. The floor manager always gets a copy of everything, which is how it maintains conversation history, tracks rounds, and makes policy decisions.

This design means that adding a new agent to a session is genuinely additive. You wire it to the bus, it announces its manifest, and the session continues. No reconfiguration of existing agents required.

### The Floor Manager

The floor manager is the coordinator. It receives every envelope, dispatches floor grants and revocations, maintains conversation history, tracks session memory, manages the session output (manuscript, images, videos, music, code), and parses orchestrator directives when running in production-pipeline mode. In SHOWRUNNER_DRIVEN sessions it also injects manuscript context, breakout results, and memory summaries into each worker's task assignment so that agents have what they need without the system having to maintain complex shared state.

### The Agents

The agent hierarchy is honestly the part that surprised me most by how far it grew. At the base is `BasePlaygroundAgent`, which handles bus registration, envelope construction, floor requests and yields, manifest publication, and retry logic for external API calls. Everything extends from there.

Text generation agents (Anthropic, OpenAI, Google, HuggingFace) all share a common `BaseLLMAgent` that handles system prompt templating, conversation history, director and showrunner message parsing, relevance filtering, and the full request-floor-receive-grant-generate-yield cycle.

Image generation is covered across three providers (HuggingFace, OpenAI, Google Gemini). Video generation runs for example on HuggingFace's Wan model, OpenAI Sora and Google Veo. Music generation uses Google Lyria's realtime streaming API. Vision agents let you pipe images from one agent into the language models of another. A CodingAgent with OpenAI's code interpreter tool can write and run code as part of a pipeline. Perception agents can classify images, detect objects, do OCR, run named entity recognition, run text classification, and summarize — all as OFP participants that can hand their outputs to other agents for further processing.

There are also remote agents: HTTP proxies to external OFP endpoints. A handful of known slugs are pre-configured — arxiv for paper listing, wikipedia for article listing, GitHub for repo analysis, a web search specialist, even Verity the hallucination detector. *Any OFP-compliant endpoint can participate without needing to be installed locally.*

It is a lot. I did not plan for it to be this much. These things have a way of growing when the foundation is solid enough that adding things is easy. And you have enough tokens on Claude.

---

## The Five Floor Policies

This is where the protocol's flexibility becomes concrete. Five policies, five completely different experiences from the same underlying agents and bus. I welcome new entries here, if there is a need for a new floor policy feel free to code it.

### Sequential

The most straightforward: agents speak in the order they request the floor. First-in, first-out. When an agent yields, the next in the queue gets the turn. This is the natural choice for structured reviews where the order of speakers matters — say, a code review pipeline where a security specialist, a performance specialist, and a style specialist each need their turn before the human gets the result back.

The example script for sequential review does exactly this. You paste a code snippet; three specialist agents examine it in order, each in their own domain, each with a specific output format; a coding agent then implements all the fixes using code interpreter. The human gets back a corrected file. The whole thing runs without the agents needing to know about each other.

### Round Robin

Strict rotation: every agent speaks once per round, in a fixed order. At round boundaries, a Director agent speaks first to set the scene and direct each participant, then the story agents each write their section, then a ShowRunner agent speaks last to synthesize everything into canonical prose and set up the next round.

This is the policy for collaborative storytelling, and it is remarkable how well it works in practice. The Director/ShowRunner frame solves a problem I didn't fully appreciate until I tried running without it: without explicit coordination, agents in a multi-agent creative session drift. They repeat each other, they lose the thread, they become inconsistent across rounds. The Director keeps each round coherent. The ShowRunner keeps the whole story coherent. These are not just stylistic choices — they are load-bearing structural elements.

### Moderated

The human holds all floor control. Agents request the floor and queue, but only speak when explicitly called on. This is the investment committee scenario: four analysts (macro, fundamentals, risk, ESG) each have their domain and their output format, and you call on them by name when you want their view. The conversation is as fast or slow as you want it. You can spawn a devil's advocate agent mid-session if the discussion needs a different kind of pressure.

### Free for All

Everyone can speak whenever they want. Floor requests are granted immediately. This one requires the relevance filter — each agent asks itself "should I respond to this?" before requesting the floor — otherwise you get every agent responding to every message and the session becomes noise. With the filter on, the free-for-all becomes something closer to how actual brainstorming works: people jump in when they have something to add, stay quiet when they don't.

### Showrunner Driven

The most powerful and the most complex. One orchestrator agent controls the entire session. Workers only speak when assigned. The orchestrator has a full directive language: `[ASSIGN Name]: task` to give someone a task, `[ACCEPT]` to add their output to the manuscript, `[REJECT Name]: reason` to send them back for revisions, `[KICK Name]` to remove a failing agent, `[SPAWN spec]` to add a new agent mid-session, `[SKIP Name]: reason` when an agent is persistently unavailable, and `[TASK_COMPLETE]` to end the session and flush all outputs.

The orchestrator is also manifest-aware, which means it can inspect what each agent claims to be capable of and make task assignment decisions accordingly. It has memory tools to store and recall decisions across the session. It has breakout tools to spin up temporary sub-floor discussions.

The resilience rules bear mentioning: if a worker fails once, the orchestrator rejects with specific feedback and tries again. If the worker fails twice, the orchestrator spawns a replacement from a different provider. If it fails three times, the orchestrator skips and moves on. I added this after watching early sessions get stuck indefinitely on a single failing agent.

---

## Breakout Sessions

Breakout sessions are one of those features I almost didn't build and am very glad I did.

The pattern is simple: the main floor is running in SHOWRUNNER_DRIVEN mode. The orchestrator needs a focused discussion on a specific subtopic before it can proceed — maybe a story brainstorm, maybe a code review, maybe a peer review of a chapter that was just written. Rather than having the main-floor agents do that work in their regular turn sequence, the orchestrator spins up a temporary sub-floor: its own isolated message bus, its own floor manager, its own fresh agent instances, running their own policy for a bounded number of rounds.

When the sub-floor completes, the full transcript is saved to the session's `breakout/` directory and a compact summary (around 200 words) is automatically injected into the orchestrator's next context. The main floor never saw the breakout happen; it just receives the summary.

The main pipeline doesn't accumulate noise from the brainstorm. The brainstorm doesn't have to worry about the main pipeline's state. The summary is the interface.

The constraint is one level deep: breakouts cannot spawn further breakouts. I put this limit in deliberately. Nesting is where systems become impossible to debug, and I was not interested in learning what three levels of recursive brainstorming produces. One level is already rich enough to be surprising.

---

## Session Memory

Agents in multi-round sessions have a real problem: they lose context. A model that wrote a compelling third chapter has no memory of writing it when it gets to chapter seven unless you explicitly maintain that context.

The `MemoryStore` is a lightweight in-session key-value store organized by category: goals (the original mission, always first in summaries), tasks (what has been assigned and completed), decisions (key choices made during the session), lessons (things that went wrong and how they were resolved), agent profiles (notes about how specific agents behaved), and preferences (style and format choices).

Memory can be written via tool calling (for orchestrator agents) or via `[REMEMBER category]: content` directives embedded in any agent's utterance. Memory summaries are automatically injected into orchestrator system prompts, worker directive contexts, and director/showrunner prompts. The injection is priority-ordered and truncated to prevent context bloat.

At session end, the memory store is serialized to `memory.json` alongside the manuscript. This means the session's institutional knowledge — every key decision, every failure and lesson, every accepted style choice — is preserved for inspection and reuse.

---

## What Gets Produced

The output directory structure tells a cleaner story than I could:

```
result/
└── 20260324_112523_a1b2c3d4/
    ├── images/          ← generated images from any image agent
    ├── videos/          ← generated video clips
    ├── music/           ← generated WAV audio (48kHz, 16-bit stereo)
    ├── code/            ← generated and executed code files
    ├── breakout/        ← full transcripts of every sub-floor discussion
    ├── manuscript.txt   ← all accepted text outputs
    └── memory.json      ← session decisions, tasks, lessons
```

In a full showcase run — the `showcase.sh` pipeline, which runs ten chapters of an illustrated story with peer reviews, optional cutscene breakouts, chapter-level HTML pages, ambient music, and a master index page — you get all of these. Text from the story writer. Images from the illustration agent. Audio from the music agent. HTML pages from the coding agent (which runs actual code interpreter, not just writes code). Breakout transcripts from the writers' room, the peer reviews, and the cutscene sessions.

The results are not perfect. Image agents sometimes misread the prompt's tone. Video generation can run for several minutes and still produce something that doesn't quite match the written scene. The coding agent occasionally produces HTML that needs a minor fix. The story agents can drift in style between chapters if the ShowRunner's synthesis isn't sharp enough.

But the *shape* of the output is genuinely surprising. When you open the final index page and find a complete ten-chapter story, illustrated, with a music player for the background track that was generated by the pipeline, with chapter pages that render the prose and images together — that is not a thing I expected to feel the way it does. It feels like something someone made. Multiple someones, working in stages.

---

## What You Can Actually Test

This is the section I care most about, because I think it is the most practically useful frame for this kind of tooling.

Multi-agent AI systems fail in specific, learnable ways. The playground is a controlled environment for discovering those failure modes before they matter. Here is what I learned:

**Model behavior under instruction varies wildly.** Sending the same task directive to an Anthropic agent, an OpenAI agent, and a Google agent with identical system prompts produces outputs that can differ dramatically in length, structure, specificity, and willingness to follow formatting constraints. You want to know this about your specific task before you pick a provider and lock in. You need to align your system prompt according to the model.

**Temporal drift is real and insidious.** In a ten-round session without explicit synthesis between rounds, agents forget what was established in round one by round five. The Director/ShowRunner pattern is a solution, but it is not a magic solution — the synthesis quality matters. If the ShowRunner writes vague summaries, drift continues anyway. You can see this in breakout transcripts and tune the synthesis prompt before it costs you in a real deployment.


**Orchestrator instruction quality is the bottleneck.** 
The orchestrator system prompt — the thing that defines the pipeline's behavior, its error handling, its task sequencing — is where most of the variance in session quality comes from. A vague orchestrator produces a vague session. A specific orchestrator with clear phase descriptions, explicit resilience rules, and concrete output format requirements produces something close to reliable. You can iterate on this in the playground cheaply.

**Relevance filtering changes session dynamics significantly.** In free-for-all mode without relevance filtering, every agent responds to every message and the session degenerates within a few turns. With it on, agents self-regulate and the conversation acquires something resembling natural rhythm. The filter adds a small latency cost (one extra LLM call per agent per message). Whether that cost is worth it depends on the session size and the degree to which you need agents to be selective. The playground lets you find out.

**Breakout sizing matters.** Too few rounds in a brainstorm breakout and the ideas haven't developed enough to be useful. Too many and the breakout drains wall-clock time without proportional benefit. The sweet spot varies by topic, by agent count, and by how directive the initial seeding prompt is. I found sixteen rounds with six agents in the story brainstorm to be roughly right for yielding a story concept that the main pipeline could work with. Eight rounds produced thin concepts. Twenty-four produced repetitive elaboration.

**Media agents need special handling.** Image, video, and music agents have completely different latency profiles than text agents. An image takes seconds. A video takes minutes. Ambient music at thirty seconds takes a number of API call cycles that depends on the Lyria streaming connection. Mixing these in a synchronous pipeline without accounting for their differences creates bottlenecks. The auto-accept behavior in SHOWRUNNER_DRIVEN mode, and the explicit timeout configuration per agent, are both things I added after discovering what happens when you don't have them.

**The manuscript is your test artifact.** In SHOWRUNNER_DRIVEN sessions, the manuscript accumulates every accepted output in order. Reading the final manuscript tells you immediately whether the pipeline produced coherent, quality work or a sequence of disconnected agent outputs that happen to be in the right order. Evaluating the manuscript — not the individual outputs, but the composed result — is the real test. This is the thing you want to be able to do cheaply, in a playground, before you commit the pipeline shape to a production deployment.

---

## Remote Agents and the Open Ecosystem

One thing I want to mention that doesn't get enough attention in the local framing: OFP is designed for interoperability. Any OFP-compliant agent running anywhere — on a different machine, at a different company, exposed as an HTTP endpoint — can participate in a session.

The playground includes a `RemoteOFPAgent` that acts as a local proxy for these external endpoints. A handful of known slugs are pre-configured: research agents for arxiv, Wikipedia, GitHub repositories, SEC filings. A web search specialist. A NASA astronomy image agent. A hallucination detection agent. A content moderation agent.

You point your session at a remote url, and the agent participates in the conversation with the same OFP mechanics as any local agent: it receives envelopes, publishes a manifest, gets floor grants, produces utterances. From the floor manager's perspective, there is no difference between a local text agent and a remote research specialist.

The cascade prevention is important here: remote agents are configured not to respond to other remote agents. Without this, you can get exponential message loops where two remote agents keep triggering each other's responses indefinitely. This is the kind of failure mode that is easy to prevent once you know it exists and miserable to debug if you encounter it in production without warning.

---

## What The Playground Is And Isn't

It is a research and exploration tool. It is genuinely useful for understanding multi-agent behavior, for testing prompt engineering across providers, for prototyping pipeline shapes, and for producing creative outputs that are interesting in their own right. The breadth of what it can do — text, images, video, music, code, structured analysis, collaborative storytelling — in a single CLI tool with a readable documentation set is something I am proud of.

It is not a production system. The session memory is ephemeral. The breakout sub-floors are not distributed. The retry logic is exponential backoff, not a sophisticated queue. The agent code does not have the kind of observability infrastructure that a real production deployment would need.

---

## Getting Started

The setup is genuinely simple. If you have Python, you have what you need:

```bash
pip install -e ".[dev]"
```

Set your API keys (Anthropic, OpenAI, Google, HuggingFace — use whatever you have, the agents that need missing keys will just not work):

```bash
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=AIza...
HF_API_KEY=hf_...
```

And you can run a session immediately:

```bash
ofp-playground start \
  --agent "anthropic:Alice:You are a curious scientist." \
  --agent "openai:Bob:You are a skeptical engineer." \
  --topic "Should we colonize Mars or Venus?"
```

Or go straight to a production-style pipeline:

```bash
bash examples/showcase_web.sh "a detective story set in a rainy city of sins"
```

Or start a code review:

```bash
bash examples/sequential_code_review.sh "def process(u): exec(u)"
```

The CLI reference, architecture documentation, floor policy guide, and agent taxonomy are all in the docs.

---

## Where This Might Go

I don't want to promise a roadmap I haven't thought through. But there are directions that feel natural.

The most overlooked feature is the underling open floor protocol which can be observed by the `—show-floor-events`. The underlying mechanism needs more analytical exposure in the future.

The second is a richer evaluation layer. Right now, quality assessment is manual — you read the manuscript and decide. An automated evaluation loop that scores outputs against criteria and feeds that back to the orchestrator would make the testing-before-production use case much tighter.

The third is web UI maturity. The Gradio-backed web interface (`ofp-playground web`) works for demonstrations and casual use, but it is not the thing you would give to a non-developer. A proper UI for session configuration and live visualization of the message bus, floor state, and agent activity would make the playground accessible to people who are not comfortable with the CLI.

None of these are imminent. They are on the list.

---

## Closing

I started building this because I wanted to provide a sandbox experience how multi-agent AI systems actually work — not in theory, not in papers, but in the specific and humbling way that things work when you run them. The Open Floor Protocol gave me a conceptual foundation that is solid. The playground gave me the experimental surface I needed to stress-test that foundation.

What I did not expect was to produce things I wanted to keep. A short illustrated story with a loopable ambient score. A chapter HTML page that looked designed rather than generated. I have generated couple of crazy cross overs. Had a chat with multiple AI’s simultaneously. 


The code is at [github.com/bladeszasza/OFPlayground](https://github.com/bladeszasza/OFPlayground). The protocol it's built on is at [openfloor.dev](https://openfloor.dev/introduction). If you build something with it, or discover a failure mode I haven't encountered yet, I genuinely want to hear about it.

---

*OFP Playground is open source and in active development. Built on the [Open Floor Protocol](https://openfloor.dev/introduction), an open standard for multi-party AI conversations. Supports Anthropic, OpenAI, Google, and HuggingFace providers.*