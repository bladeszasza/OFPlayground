# OFPlayground fresh start and ASP implementation plan

Date: 2026-10-31. Status: accepted for implementation; work started 2026-11-01. See [implementation status](../implementation-status.md) for evidence and outstanding gates. Release convention finalized 2026-11-04: combine the verified hardening and ASP work in v0.3.0; the proposed 0.2.0 checkpoint remained untagged while author identity was deferred.

## Outcome

Create a dependable local multi-agent workbench with consistent CLI, Canvas, and Gradio behavior. Incorporate Agent Status Protocol (ASP) as a distinct status and observation layer, and make the Canvas show actual activity and operational state clearly enough that a user can explain what each agent is doing, why it is waiting, and what intervention is available.

Use the existing code and prior documentation as the starting point. Preserve Git history, expert personas, research, examples, and local changes. A fresh start means a verified release baseline and clear contracts, not deleting the project or hiding outstanding defects.

## Sources and protocol scope

- [Step 0 assessment](../assessment-2026-10-31.md), repository source at `ce952be`, existing tests, and previous expert/research work.
- User-supplied `2610.21538v1.pdf`, 30 pages; [arXiv paper v1](https://arxiv.org/abs/2610.21538v1), especially sections 5-8 and 12-14.
- [ASP repository](https://github.com/AgentStatusProtocol/spec) and its [event schema](https://github.com/AgentStatusProtocol/spec/blob/main/schemas/asp-event.schema.json). Reviewed schema blob: `b7660039a49e3fdbc013c95c3f44bf94dbe05761`.

ASP 0.1 is a working proposal. The published schema is non-normative and the illustrative implementations are not conformance-tested production components. Pin the exact specification commit before implementation; record paper/schema differences in an architecture decision. Build behavioral tests against the chosen profile rather than claiming compliance because JSON validates.

OFP remains the conversation/floor protocol. ASP reports status. Operator commands remain application control operations: recording `quarantined` is not the same as actually stopping a model request or revoking its floor.

## Step 0 - Establish the baseline

Complete the dated assessment with runtime, frontend, packaging, documentation, protocol, and UX findings. Inventory branches/tags and all current version declarations. Classify every issue as reproduced, source-inspected, suspected, or blocked by environment. Preserve logs and resolved dependency versions.

Baseline already collected: isolated editable install completed; Python test run reached 524 passes and 10 failures before its failure-limit stop; Ruff reported 333 findings; frontend install was blocked by a package-registry timeout. See the assessment for environment-versus-code classification. The full suite, type checks, frontend build and live UX gates remain to be completed during implementation.

Run the Python suite, lint/type checks, frontend build, asset-load smoke test, and a small provider-free session through each interface. Audit the real journeys: install, configure, add agent, load persona, start, send human input, inspect status, recover from error, remove agent, stop, reconnect, reopen saved configuration.

**Exit gate:** a reproducible baseline report with actual pass/fail results, prioritized defects, and acceptance tests. Network failures must remain labeled environment failures. Previous mock checks do not replace end-to-end verification.

## Step 1 - Fix runtime and interface correctness

Start with failures already identified, then expand from test evidence:

1. Enforce floor ownership on yield/revoke; remove departed agents from ownership, pending requests and rotation; prevent late responses from removed agents re-entering the session.
2. Give sessions explicit lifecycle/cancellation ownership. Retain agent task handles, cancel and await children, stop watchdogs on completion/failure, and terminate CLI/web/Canvas cleanly.
3. Introduce a shared session service for start, stop, participant add/remove, human input and configuration. All interfaces use the same validation and outcomes.
4. Unify persona expansion and settings precedence for initial and dynamically added agents. Give agents stable instance IDs independent of display names, with an explicit mapping to OFP speaker URIs.
5. Make general-purpose prompts neutral; put story instructions in an explicit creative mode.
6. Correct Windows media containment, request validation, duplicate names, session races, and error propagation. Keep local access explicit.
7. Fix event serialization, reconnect duplication, ignored errors, and complete frontend assets/proxy routes. Distinguish queued, delivered, dropped and processed evidence where actually available.

**Exit gate:** regression tests for each reproduced defect; deterministic two-agent sessions complete; removing the floor holder or a queued agent does not deadlock; stop during a request leaves no uncontrolled background task. Where a provider cannot cancel an already submitted job, report that limitation and suppress stale results rather than pretending cancellation succeeded.

## Step 2 - Update and align the project

Audit Python and npm dependencies against official releases/security advisories. Compare installed APIs, declared support and actual provider adapters. Upgrade in coherent groups with compatibility tests; a new major version is not automatically an improvement. Pin the OFP SDK revision and select/document the supported OFP message profile.

Choose a reproducible Python dependency workflow and retain the npm lockfile. Align Python minimum/supported versions, Node support, scripts, package metadata, generated types, model defaults, sample presets and documentation. Move provider model choices behind configuration and validate availability without exposing keys; do not silently change a user's selected models.

Add Windows and Linux CI for core tests, lint, type checks, frontend typecheck/build, documentation links and packaged/static assets. Ensure a fresh clone can complete the documented quick start. Decide and test whether distribution remains an editable checkout or includes bundled assets/personas in built packages.

**Exit gate:** a clean supported install, passing required checks, documented exceptions only where justified, and no unsupported claims in the READMEs. Do not mass-upgrade dependencies and suppress resulting failures.

## Step 3 - Tag the clean baseline

Proposed release naming: **v0.2.0** for the hardened baseline, followed by **v0.3.0** for ASP and the UX work. Current package version is 0.1.0; the historical `working-2.0` tag is not treated as an existing semantic 2.0 release. Confirm the release convention during Step 0.

Synchronize `pyproject.toml`, Python version export, frontend package and lock metadata, application version display/API where present, and changelog. Use small reviewable commits and an annotated tag only after the baseline gates pass. Keep the old tag unchanged and verify the new tag points at the intended clean commit. No destructive reset or history rewrite. Publishing follows the repository's release process; local tagging does not imply a package was published.

## Step 4 - Implement ASP as a shared status layer

Proposed modules: `src/ofp_playground/status/` for event models, reducer, identity/authority checks, journal, snapshots, liveness monitoring and publishers; thin HTTP/SSE bindings reusable by the local application. Use a separately runnable status service so monitoring can survive failure of the conversation runner. A one-command local launcher should start the needed processes and communicate their endpoints/credentials without making users wire them manually.

Track lifecycle, health and governance per agent; tasks per task ID; floor state per conversation ID. Never collapse these into one ambiguous green/red flag. Distinguish self-report, supervisor/peer observation and server inference, including freshness and unresolved disagreement.

Implement the selected ASP profile:

- Registration, heartbeats, state/floor changes, deregistration; stable identities and per-agent/per-sender ordering.
- Event-ID deduplication; older sequence events retained in history but unable to overwrite newer state; server stream cursors distinct from sender sequence numbers.
- Single/batch ingest, agent snapshots and resumable SSE. Handle the snapshot/subscribe race, retention gaps, restart/re-registration and slow clients explicitly. Any draft ambiguity becomes a documented implementation decision with tests.
- Heartbeat timeout at the draft's 2.5 declared intervals; other components' observations must not falsely refresh agent liveness. Show retained task/floor values as stale after loss of contact.
- Progress-aware stall detection only while progress is expected; legitimate input/approval/queue waits are not stalls. Configure thresholds by work type, including long media generation. Heartbeat activity must not mask a stuck work loop.
- Server-assigned states, terminal task rules, and governance precedence. Derive reporter identity and authority from credentials, not from a caller-supplied `source` field alone.
- Authenticated ingest and authorized snapshots/streams, even in the local profile; no secrets in URLs. Use suitable authenticated streaming rather than depending on native EventSource to send arbitrary bearer headers. TLS for non-local deployments.
- Bounded event size/rate, append-only journal with retention and replay, and state-only payloads without prompts, outputs, credentials or user content. Allowlisted reason codes and metadata prevent exception messages leaking secrets.

Instrument lifecycle, provider calls, retry/backoff, cancellation, task milestones, floor transitions and child sessions at their actual runtime boundaries. Avoid inferring working/healthy solely from a chat response. Remote agents without trustworthy ASP support appear as uninstrumented/unknown or explicitly adapter-observed.

**Exit gate:** schema and behavioral tests cover ordering, duplicates, authority conflicts, stale data, reconnect, restart, dropped heartbeats, stalls, terminal tasks, invalid inputs and status-service outages. No claim that ASP detects compromise automatically. Optional OTLP ingest, active probing, alternative transports and cross-vendor credential federation can follow after this profile is working; document them as unsupported initially.

## Step 5 - Give Canvas a distinct status experience

Keep the graph for membership and observed communication. Add a separate **Agent Status** dock with a fleet summary, searchable/sortable agent rows, and a selected-agent detail view. Each row exposes lifecycle, health, task, floor, governance, last observation and provenance; details show transition history and scoped tasks/conversations. Indicate stale, disconnected, unavailable and uninstrumented states explicitly.

Agent nodes show a compact version of that state. Clicking an event highlights the relevant agent, floor and timeline entry. Distinguish a live event, a replay and a requested operation. Show disagreements between self-report and independent observation instead of silently choosing the most reassuring value.

Add/remove flows must work before and during a session:

- Search providers/models/personas, preview the resolved persona, validate required fields and keys, show spawn progress and actionable errors.
- Add only after the backend acknowledges the participant; show pending and failed requests without phantom nodes.
- Remove with defined graceful-drain and force-stop behavior. Clean up queues, tasks, floor membership and status; retain a historical record for the trace.
- Preserve layout/view state across reconnects, distinguish hiding a visual node from removing its live agent, and make destructive or billable actions clear.

Governance controls show requested, acknowledged and enforced outcomes separately. A quarantine adapter must actually block new assignments/floor grants and apply defined cancellation behavior; recording ASP governance alone is insufficient. Release requires enforcement tests and provenance in the status view.

**Exit gate:** keyboard-accessible flows, readable labels beyond color, useful empty/loading/error states, reconnect recovery without duplicates, and tested participant changes while idle, active, queued, failing or quarantined.

## Step 6 - Make visualization dynamic and meaningful

Use distinct visual layers for floor membership, observed message flow and status. Animate only real observed events, with direction and a brief meaning; no decorative continuous traffic. Queue order and current floor holder should be immediately visible. Distinguish failed or dropped routes from successful delivery evidence.

Add activity trails, timeline scrub/replay, filters, agent-focused inspection, auto-layout with position pinning, fit-to-selection and nested-session grouping. Use aggregation and bounded rendering for larger sessions. Preserve user pan/zoom while updates arrive; support reduced motion and avoid color-only encoding.

For this release, edges represent membership or observed flow, not a freely editable execution DAG. If editable routing is later introduced, it needs an explicit backend routing contract and separate tests.

**Exit gate:** users can answer who is working, waiting, holding the floor, unhealthy, stale or restricted without reading raw logs. Test a small session and a larger simulated fleet for clarity, responsiveness and stable layout.

## Step 7 - Align CLI and Gradio UX

Use the same session/control/status services across all three interfaces. CLI gets useful status/watch output, persona/model discovery, add/remove operations, understandable errors, meaningful exit codes and predictable Ctrl+C cleanup. Offer machine-readable output where automation needs it.

Gradio gets participant management, a compact multi-dimensional status panel, clear input/approval/error states, and the same lifecycle outcomes. Keep frontend-specific presentation while sharing behavior. Disabled or unsupported operations must explain why; silent no-ops are not acceptable.

**Exit gate:** one acceptance matrix exercises setup, add, remove, start, stop, input, provider error, recovery, reconnect and status provenance across CLI, Canvas and web. Web supports reconnect; CLI explicitly reports lost observation rather than presenting stale state as live.

## Step 8 - Demonstrate, document and release ASP

Ship a deterministic provider-free Canvas showcase with scripted agents that perform work through the real runtime/status path. Demonstrate normal work/floor exchange, a recoverable rate limit, human-input wait, an agent that stops reporting, one that reports but stops progressing, and an independently issued governance restriction. Labels must make synthetic scenarios clear; the status panel itself must consume real emitted/derived ASP events.

Also provide an opt-in small live-provider example with declared limits. Save replayable examples and document protocol support, authority, freshness, failure handling and known omissions. Rewrite README/tutorial instructions from a fresh-clone run, not from memory.

**Final release gate:** required tests, type/lint checks, frontend build, clean-install smoke tests, documentation links, ASP behavioral fixtures, replay tests and cross-interface journeys pass. Verify stop/cancellation and resource cleanup under faults. Produce changelog and release evidence, align versions to the selected ASP release number, and create the annotated release tag only at the tested commit.

## Implementation order and scope control

Steps 0-3 establish the trustworthy baseline. Step 4 establishes status semantics. Steps 5-7 consume those semantics; Step 8 proves the complete user journey. Add tests for runtime behavior and state transitions, not tests that merely duplicate Markdown or visual styling.

Do not start with a large visual redesign, a complete architecture rewrite, or a tag that implies unresolved checks passed. Keep the first ASP release local-first, with explicit trust boundaries. Public multi-tenant hosting, new model training, full workflow-DAG execution, automatic compromise detection and large distributed-fleet infrastructure are separate scopes.
