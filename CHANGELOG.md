# Changelog

## 0.3.0 — ASP and workbench (2026-11-04)

This release combines runtime hardening, the first local ASP implementation and Canvas/CLI/web integration. The planned 0.2.0 baseline was verified locally but remained untagged while author identity was deferred. The release convention is therefore one combined v0.3.0 source release; there is no earlier ASP-free release tag. Package publication is separate from pushing the source and annotated tag.

- Fix floor ownership, concurrent free-for-all holders, departed-agent queues, cancellation, session teardown, neutral prompts, SDK manifests, shared personas/settings and Windows media containment.
- Add shared graceful/force participant removal and independently authenticated quarantine enforcement with requested/acknowledged/enforced evidence.
- Do not report a restriction cleared during a status-service outage; retain unknown acknowledgement and keep the removed instance stopped.
- Add a separate authenticated ASP service, durable ordering/replay, liveness/stall/deadline reduction, state-only payload validation and runtime instrumentation.
- Add Canvas status dock, observed transport layers, authoritative floor snapshots, bounded replay/filtering, pinned layout and local browser authorization.
- Isolate browser authorization cookies between local Canvas ports so simultaneous instances do not replace each other's credentials.
- Add validated configuration export/import/reopen, resolved persona preview, provider-specific task discovery and credential-presence hints, phone-width controls, reduced motion and square fleet layout. Keep transition history and operation results scoped to the selected agent.
- Add CLI discovery/status/watch/control commands and Gradio status/participant controls. Fix redirected Windows output and autonomous startup without a topic.
- Add a provider-free synthetic ASP showcase, dependency lockfiles, SDK pin and Windows/Linux CI configuration.

Known exception: optional Gradio 6.31.0 prevents Pillow 13 security fixes through its Pillow constraint. The full application remains a local source-checkout distribution. See [dependency review](docs/dependencies.md) and [implementation evidence](docs/implementation-status.md).

## 0.1.0

Original experimental runtime. The historical `working-2.0` Git tag remains unchanged; it is not treated as a semantic package version 2.0.0.
