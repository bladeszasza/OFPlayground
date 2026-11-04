# OFP Playground documentation

OFP Playground is an experimental local runtime for multi-agent conversations, creative orchestration, and communication research. Its CLI, Gradio, and Canvas interfaces share Python agent and floor-management code.

Use the [root README](../README.md) for installation, configuration, Canvas builds, and limitations. Use the [2026-10-31 assessment](assessment-2026-10-31.md) for evidence, verification results, and development priorities. These documents are not a protocol-conformance certification or a production-readiness claim.

## Start here

1. Install an editable source checkout using the root README. Include the `canvas` extra and build `canvas/frontend` for the visual interface.
2. Run the [provider-free ASP showcase](asp.md#provider-free-showcase), then configure one provider for a small live conversation.
3. Use [personas](agents-library.md) or the [four-expert panel](expert-agents.md) through CLI specifications or the Canvas picker. Initial and dynamically added agents share prompt resolution.
4. Inspect the session trace and artifacts. Turn limits stop the floor on the N-th agent utterance; they are not hard cost or time budgets.

## Guides and references

| Document | Contents |
|---|---|
| [Architecture](architecture.md) | Message bus, floor manager, agent hierarchy |
| [ASP and showcase](asp.md) | Status semantics, provenance, independent service, controls and synthetic failure walkthrough |
| [Dependency review](dependencies.md) | Lockfiles, supported toolchains, SDK profile and optional Gradio security exception |
| [OFP integration](ofp-protocol.md) | Envelope/event concepts and historical examples; verify constructors against the installed SDK |
| [Floor policies](floor-policies.md) | Implemented scheduling rules, invariants and SHOWRUNNER delegation |
| [Interfaces](interfaces.md) | How every session option and runtime control maps across CLI, web and Canvas |
| [Agents](agents.md) | Taxonomy and provider/task implementations; defaults do not prove current provider availability |
| [Persona library](agents-library.md) | `SOUL.md` structure, slugs, loading API |
| [Expert panel](expert-agents.md) | Protocol, repository, communication, and emergent-communication specialists; launch commands |
| [Orchestration](orchestration.md) | Assignments, revisions, spawning, breakouts, phase artifacts |
| [CLI reference](cli.md) | Commands and agent specifications; confirm current options with `--help` |
| [Configuration](configuration.md) | File layout and provider variables; read the corrections below |
| [Output](output.md) | Session directories, media, artifacts, trace |
| [Canvas reference](canvas-reference.html) | Visual interface reference; edges currently do not define routing |
| [Repository assessment](assessment-2026-10-31.md) | Strengths, confirmed defects, verification limits, priorities |
| [Implementation status](implementation-status.md) | Current fixes, verification evidence and outstanding release gates |
| [July 2026 communication research](research/hugging-face-2026-communication.md) | Incident analysis distinguishing protocols, shorthand, and language-emergence claims |

## Corrections to older reference pages

These points were checked against source on 2026-11-01. Prefer them over conflicting statements in older guides:

- **Configuration:** `start`, `web` and `canvas` prefer TOML key values over environment values. The CLI loads `.env` without overwriting the environment.
- **Persona listing:** `ofp-playground agents` prints provider/task help. Use `ofp-playground personas SEARCH --preview` for actual slugs and resolved instructions.
- **Validation:** `ofp-playground validate envelope.json` validates an envelope file, not credentials or overall configuration.
- **Policies (updated 2026-11-04):** round-robin is a strict ring (requests no longer precede the rotation) and moderated mode waits for the human chair when one is present. See [floor policies](floor-policies.md); the older statement to the contrary describes the pre-fix code.
- **Coding:** Anthropic and Google have coding implementations alongside OpenAI. Hugging Face `code-generation` is unsupported; descriptions calling every non-OpenAI implementation a stub are stale.
- **Canvas:** the checkout includes a frontend bundle. Rebuild after source changes; Vite proxies the session, agent/model, preset, remote-list, media and trace endpoints. Graph edges do not configure backend routing.
- **Examples:** Bash showcase scripts are experiments with different configurations and provider requirements, not validated production pipelines or interchangeable CLI/web editions.

## Contributing documentation

Separate implemented behavior from intended behavior. Tie version-sensitive claims to a source revision, use real commands and paths, and state what verification actually exercised. A mock test or source inspection does not establish provider compatibility or end-to-end UI behavior.

Proposals in [`specs/`](specs/) and historical plans in [`plans/`](plans/) are context, not authoritative inventories of current features or unfinished work.
