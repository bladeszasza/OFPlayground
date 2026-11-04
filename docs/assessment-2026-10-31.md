# Repository assessment - 2026-10-31

Baseline: `ce952be4582fd8533a7be5d964338a7cf2dbf1e9`, plus the local expert personas and documentation work. This assessment informs the [fresh-start and ASP plan](plans/2026-10-31-fresh-start-and-asp.md). Findings describe that baseline; fixes and remaining release gates are tracked in [implementation status](implementation-status.md).

## Judgment

The project is worth developing. It has substantial working concepts: shared orchestration, multiple provider adapters, phase artifacts, nested conversations, a persona library, and unusually useful trace infrastructure. Its strongest direction is an instrumented workbench for understanding and improving multi-agent interaction.

Feature breadth currently exceeds reliability. Several interface promises do not match runtime behavior, and core scheduling/lifecycle edge cases remain. More provider types or elaborate demos would increase maintenance before establishing a dependable foundation. The immediate goal should be a reproducible install and a small, fully observable session that starts, changes participants, handles failure, and stops correctly.

The code has useful module boundaries, but responsibility is concentrated in `cli.py` (1,704 lines) and `floor/manager.py` (1,597 lines). Extract shared session services around tested behavior rather than undertaking a wholesale rewrite.

## Prioritized findings

| Priority | Finding and consequence | Evidence / acceptance criterion |
|---|---|---|
| High | A non-holder can advance the floor queue by yielding. Removed agents can remain holder or queued. | `floor/policy.py`, `yield_floor` and `remove_from_rotation`; `floor/manager.py`, `unregister_agent`. Actual controller reproduction: A holds, B queued, unrelated sender yields, B becomes holder. Require holder validation and complete participant cleanup. |
| High | Early floor completion can leave CLI/web turn-watchdog tasks running indefinitely. | `cli.py`, `_run_session` and `_run_web_session` orchestration loops. Extracted actual watchdog bodies kept polling after the actual `stop` body ran; bounded wait expired. Require shared cancellation and awaited task cleanup. |
| High | Fresh-clone Canvas assets are incomplete. | `canvas/frontend/dist/index.html` references `/assets/index-DXNoQzdn.js`, absent from tracked files. Require reproducible frontend build and a smoke test that loads every referenced asset. |
| High before exposure | Canvas is unauthenticated; media-path containment is not robust on Windows. | `canvas/backend/main.py`, `build_app` and `_safe_media_path`. String-prefix ancestry and slash-only traversal checks are insufficient. Require path-ancestry checks, negative tests, explicit local trust boundary and authenticated operator/status endpoints. |
| High | Canvas persona selection can pass the literal slug instead of persona contents. | `SlugCombobox.tsx`, backend `_spawn_agent_for_canvas`, CLI `_resolve_agent_slug`. CLI-prepopulated Canvas expands correctly. Require one shared resolver for initial and dynamically added agents. |
| Medium | Canvas and CLI configuration differ. | Canvas calls `Settings()`; CLI/web use `Settings.load()`. CLI group does load `.env` for all subcommands, but Canvas bypasses TOML. Require shared precedence, model defaults, and configuration diagnostics without exposing keys. |
| Medium | Canvas graph edges do not enforce routing. | Backend uses the first floor node and ignores edges for routing. Require explicit membership/observed-flow semantics in the UI; do not imply an executable workflow graph. |
| Medium | Visual event state can omit events or duplicate them on reconnect. | Bridge serializer returns after the first matching event; connection replays history; frontend appends events/counters without deduplication. Require event identities, complete envelope fan-out, replay cursor and idempotent reducers. |
| Medium | General technical agents inherit creative-writing instructions. | `agents/llm/base.py`, `SYSTEM_PROMPT_TEMPLATE`. Require neutral base instructions with explicit task-specific modes. |
| Medium | Policy names overstate guarantees. | Queues precede round-robin fallback; moderated mode auto-grants when idle. Require defined invariants, tests, and names/help matching behavior. |
| Medium | Routing traces are not delivery receipts. | `bus/message_bus.py` records intended recipients even when queues are absent/full. Require clear delivery/drop observations and no UI implication that every recipient processed an event. |
| Medium | Packaging and dependency resolution are not reproducible. | Unpinned Git SDK dependency, broad Python version ranges, assets/personas outside the Python package tree, no checked-in CI workflow found. Require lock/constraints policy, clean-build checks, and a documented supported installation path. |

Paths beginning with `floor/`, `agents/`, or `bus/` are under `src/ofp_playground/`. Further frontend limitations include incomplete Vite proxy routes, unbounded event history, and serial WebSocket broadcasting. These need focused tests and performance budgets, not just cosmetic changes.

## Verification and limits

- Editable installation with development and Canvas extras completed in an isolated Python 3.12 environment, resolving `openfloor` 0.1.5. This proves dependency installation in that environment, not runtime compatibility of every provider.
- 35 existing dependency-free controller test methods passed when executed unchanged without the pytest runner. The additional floor-state reproductions above exposed cases those methods do not cover.
- CLI and web shutdown reproductions used unchanged AST-extracted orchestration/stop code, mock state/renderers, and real asyncio sleeps. They are focused lifecycle checks, not live provider sessions.
- Ruff reported 333 findings with the freshly resolved toolchain. Separate correctness defects from formatting/type-modernization work; do not count every finding as a runtime bug.
- Frontend `npm ci` failed twice on a registry timeout downloading `update-browserslist-db`. The retry was bounded. This is an environment/network blocker, not evidence that the frontend source cannot compile. No successful frontend build is claimed.
- Python suite attempt: `python -m pytest -q --disable-warnings --maxfail=10` produced **524 passed, 10 failed in 221.97 seconds**, then stopped at the requested failure limit. This is not a complete-suite pass or a complete failure inventory. No live provider or paid model calls were deliberately run.

The ten failures separate into these categories:

| Count | Area | Observed failure / interpretation |
|---:|---|---|
| 1 | Symlink-boundary test | Windows denied creation of the fixture symlink (`WinError 1314`); this did not exercise the intended escape rejection. |
| 1 | Gradio renderer | A native dependency DLL was blocked by Windows Application Control; environment execution restriction, not proof of a renderer logic defect. |
| 3 | Agent manifests | Installed SDK `Identification` requires `organization` and `synopsis`; current construction omits them. Reproduced compatibility defect. |
| 2 | Image references | Windows path representation differs from test expectations; another test treats a tuple result as a path. Investigate implementation/test contract before changing behavior. |
| 3 | Session bridge | Serialized results now include `envelope_json`, while exact-equality expectations omit it. Resolve intended schema and test coverage rather than simply removing useful trace data. |

The run also reported an unset pytest-asyncio fixture-loop scope. Set and test that explicitly during dependency alignment. Raw test, installation, lint, reproduction logs and resolved package versions are retained in the task's `work/` directory; they are not release artifacts or evidence of passing live-provider integrations.

## Recommended release direction

Implementation began on 2026-11-01. The complete pre-change Python run finished with **569 passed and 10 failed** (59.54 seconds), rather than stopping at the earlier failure limit. The Gradio native-library failure from the previous attempt did not recur. Mypy reported **130 errors in 32 files**. The original frontend lockfile can build after retrieving its official npm tarballs with the Windows HTTP client and adding them to npm's integrity-checked cache; npm's own network fetch had timed out. This is a local installation workaround, not a changed registry or evidence of a clean install on another machine.

First establish a dependable local baseline: runtime invariants, clean session termination, unified configuration/personas, complete packaging, and CI. Then add ASP as an independently testable status service with honest provenance, followed by a dedicated status view and meaningful observed-flow visualization. See the phased plan for release gates and user journeys.

Keep the current Git history and local work. The existing `working-2.0` tag points to `a553adf`; package versions currently say `0.1.0`. Reconcile that naming explicitly before choosing new release tags.

## Implementation follow-up — 2026-11-01

The baseline findings above describe the pre-change repository and remain historical evidence. The current 0.3.0 candidate fixes the reproduced floor/lifecycle, manifest, media containment, persona/settings, asset, bridge and reconnect defects. The complete locked suite now reports **689 passed and 1 skipped**; Ruff, mypy and frontend checks pass. Browser acceptance covers synthetic work/faults, participant changes, configuration reopen, phone-width status, reduced motion and a 100-agent rendered layout. See the [implementation ledger](implementation-status.md) for exact scope and limitations.

My assessment: the project now has a credible foundation for a local research workbench. Its strongest idea is making OFP conversation behavior observable through a separate ASP status model, with actual enforcement distinguished from a status assertion. It is still experimental software. CLI/provider construction and the Canvas application remain large modules, real provider/account compatibility has not been paid-tested, source assets are not bundled into a complete wheel, and the optional Gradio dependency has an unresolved security constraint. Those deserve focused follow-up before public or production use; another broad feature expansion would make maintenance harder.

A second follow-up on 2026-11-04 addresses the remaining "policy names overstate guarantees" finding and SHOWRUNNER delegation stalls (strict round-robin, human-chaired moderation, silent-worker recovery, parallel fan-out, CLI/web/Canvas parity); see the [implementation ledger](implementation-status.md#floor-delegation-and-interface-parity--2026-11-04).

On 2026-11-04 the user supplied a Git author identity and authorized the source push. The release convention combines the verified work in v0.3.0; the earlier proposed 0.2.0 checkpoint remains untagged. Source tagging does not imply package publication.
