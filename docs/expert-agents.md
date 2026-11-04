# OFPlayground expert panel

Four reusable personas for developing OFPlayground, researched on **2026-10-30**. These are ordinary `SOUL.md` personas, loaded by the existing agent library. They provide role instructions and a sourced knowledge baseline; they are not separately trained models and do not add browsing, repository access, or execution tools.

| Persona | Slug | Responsibility |
|---|---|---|
| [Open Floor Protocol Expert](../agents/development/open-floor-protocol-expert/SOUL.md) | `@development/open-floor-protocol-expert` | Specification semantics, SDK differences, interoperability and conformance |
| [OFPlayground Expert](../agents/development/ofplayground-expert/SOUL.md) | `@development/ofplayground-expert` | Actual repository architecture, implementation constraints, fixes and verification |
| [Multi-Agent Communication Expert](../agents/development/multi-agent-communication-expert/SOUL.md) | `@development/multi-agent-communication-expert` | Handoffs, delivery, lifecycle, shared state, observability and evaluation |
| [Emergent Communication Researcher](../agents/development/emergent-communication-researcher/SOUL.md) | `@development/emergent-communication-researcher` | Shorthand, convention formation, communication style and causal evidence |

Each persona distinguishes observed facts, specification requirements, proposals, and uncertainty. Each states its runtime-access limits. The incident research is summarized in [the July 2026 brief](research/hugging-face-2026-communication.md); the essential findings also live inside the relevant persona because linked Markdown is not automatically injected.

## Launch the panel

Requires an editable install of this repository and a configured provider key. From Python 3.12+, install with `python -m pip install -e ".[dev,canvas]"`. These are persona files selected with an agent slug; loading them does not install dependencies or configure keys. Follow [configuration guidance](configuration.md).

This PowerShell example uses OpenAI for all four participants so only one provider needs configuring. You can replace `openai` with another supported text provider, or use the flag-format `-model` option to select a model available to your account. No model availability is implied by the example.

```powershell
$panelArgs = @(
    '--policy', 'round_robin',
    '--no-human',
    '--max-turns', '12',
    '--topic', 'Review a proposed OFPlayground communication improvement. Give technical analysis. First pass: one finding from each specialty. Second pass: challenge assumptions and propose measurable checks. Final pass: recommendations, disagreement, and missing evidence. Use supplied evidence; do not claim live research or code execution.',
    '--agent', 'openai:OFP:@development/open-floor-protocol-expert',
    '--agent', 'openai:Playground:@development/ofplayground-expert',
    '--agent', 'openai:Communication:@development/multi-agent-communication-expert',
    '--agent', 'openai:Emergence:@development/emergent-communication-researcher'
)
ofp-playground start @panelArgs
```

Replace the topic with your concrete proposal and relevant excerpts. `--max-turns` is a session cap, not a guarantee of three perfectly balanced rounds. Inspect the trace if participation or ordering differs from expectations.

For the visual interface, use the same argument array with:

```powershell
ofp-playground canvas @panelArgs
```

This opens a prepopulated canvas; press Run to start the session. CLI prepopulation expands each slug into its complete prompt before passing it to Canvas. At baseline commit `ce952be`, the direct Canvas spawn path does not perform that expansion. Use these CLI commands or paste the complete persona content into the node's system prompt; selecting a slug alone is not sufficient evidence that it was loaded. This caveat comes from source inspection, not a live provider test.

The shared LLM prompt also has story-oriented framing at this revision. The panel topic explicitly requests technical analysis. If a model still produces narrative output, inspect the composed prompt with the repository expert.

## Using the experts in development

Ask the protocol expert what is required, the repository expert what is implemented, the communication expert what behavior and failure handling to design, and the emergent researcher what an experiment can actually establish. A useful handoff contains a concrete question, source or trace references, expected artifact, and acceptance criterion.

The launch example creates text participants. For actual code inspection or edits, use a runtime with the relevant file tools and provide these personas as instructions; the example itself does not give them local filesystem access. Newly added files are discoverable automatically. Restart an already running library service to refresh its cached index.

## Research baseline and maintenance

The OFP expert records specification **1.1.1** versus SDK package **0.1.5** with a default message schema of **1.1.0**, as observed on the research date. Check the deployed SDK revision before changing serialization or assuming new roles are supported. [Official specification](https://github.com/open-voice-interoperability/openfloor-docs/blob/main/specifications/ConversationEnvelope/1.1.1/InteroperableConvEnvSpec.md), [SDK metadata](https://raw.githubusercontent.com/open-voice-interoperability/openfloor-python/main/pyproject.toml), [SDK schema implementation](https://raw.githubusercontent.com/open-voice-interoperability/openfloor-python/main/python/src/openfloor/envelope.py).

Refresh dated sources when the protocol, SDK, or repository changes. Keep confirmed knowledge inside each persona concise; keep longer investigations in `docs/research/`. Preserve source disagreement and record the revision behind implementation claims.
