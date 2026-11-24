# Changelog

## Unreleased

- Adopt OFP inter-agent message 1.1.0 for remote agents: event-level `to`, `getManifests` with `recommendScope`, relayed `acceptInvite`/`declineInvite` with reasons, forwarded `uninvite`, and handling of a remote `bye`. Local envelopes already used the 1.1.0 SDK.
- Default Anthropic text and vision agents to Claude Haiku 5.5 (`claude-haiku-5-5`). Read text blocks by type (adaptive thinking is on by default), end every request on a user turn (no assistant prefill), and send `output_config.effort` only to models that accept it.
- Add `ofp-playground governance` for shared ASP status servers (`ASP_SERVER`, token from `ASP_TOKEN` only) and an opt-in mirror of session status events (`OFP_ASP_MIRROR=1`).
- Add the **Claude Haiku 5.5 roundtable** Canvas preset (three Haiku agents plus the remote Verity fact-checker) and opt-in Playwright Canvas browser tests (`tests/e2e`, `OFP_E2E=1`) that save screenshots and traces under `result/ui-tests/`.
- Canvas: agent node headers no longer overlap. The remove control is an inline button beside the status pill, and long names truncate. After a session ends, no node keeps showing "has floor".
- Unit tests no longer load the developer's `.env`, so they cannot reach real providers or shared ASP servers through it.

## 0.3.0 — ASP and workbench (2026-11-04)

This release combines runtime hardening, the first local ASP implementation and Canvas/CLI/web integration. The planned 0.2.0 baseline was verified locally but remained untagged while author identity was deferred. The release convention is therefore one combined v0.3.0 source release; there is no earlier ASP-free release tag. Package publication is separate from pushing the source and annotated tag.

- Fix floor ownership, concurrent free-for-all holders, departed-agent queues, cancellation, session teardown, neutral prompts, SDK manifests, shared personas/settings and Windows media containment.
- Add shared graceful/force participant removal and independently authenticated quarantine enforcement with requested/acknowledged/enforced evidence.
- Do not report a restriction cleared during a status-service outage; retain unknown acknowledgement and keep the removed instance stopped.
- Add a separate authenticated ASP service, durable ordering/replay, liveness/stall/deadline reduction, state-only payload validation and runtime instrumentation.
- Add Canvas status dock, observed transport layers, authoritative floor snapshots, bounded replay/filtering, pinned layout and local browser authorization.
- Isolate browser authorization cookies between local Canvas ports so simultaneous instances do not replace each other's credentials.
- Add validated configuration export/import/reopen, resolved persona preview, provider-specific task discovery and credential-presence hints, phone-width controls, reduced motion and square fleet layout. Keep transition history and operation results scoped to the selected agent.
- Add CLI discovery/status/watch/control commands and Gradio status/participant controls. Fix redirected output with legacy encodings on Windows/Linux and autonomous startup without a topic.
- Reject coding-workspace symlink escapes into similarly named sibling directories, and consistently reject rooted, drive-relative and UNC paths across supported Python versions.
- Add a provider-free synthetic ASP showcase, dependency lockfiles, SDK pin and Windows/Linux CI configuration.

Known exception: optional Gradio 6.31.0 prevents Pillow 13 security fixes through its Pillow constraint. The full application remains a local source-checkout distribution. See [dependency review](docs/dependencies.md) and [implementation evidence](docs/implementation-status.md).

## 0.1.0

Original experimental runtime. The historical `working-2.0` Git tag remains unchanged; it is not treated as a semantic package version 2.0.0.
