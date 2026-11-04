# Dependency and distribution review

Reviewed 2026-11-01. Use the committed `uv.lock` with uv 0.13.6 and `--locked`. It contains package hashes and the exact OFP SDK revision. `pip install -e` remains available but resolves dependencies independently. Frontend installs use `npm ci`, never an unrecorded `npm install` on deployment.

The supported development targets are Python 3.12/3.13 and Node 22.12+ or 24. CI runs Windows and Linux. A configured CI matrix is not evidence that a remote workflow has run; local evidence is recorded separately in [implementation status](implementation-status.md).

## Protocol profile

OFP SDK commit `eab87464fde1c79c32ad6e51cf3161686daf8316` advertises message 1.1.0, dialog 1.0.2 and manifest 1.0.1. Local constructors, serialization and floor behavior are tested against that revision. The SDK has no `py.typed` marker: mypy's external-package exception is scoped to `openfloor.*`; project function bodies are checked. This is not a claim of universal remote-agent interoperability. [SDK source](https://github.com/open-voice-interoperability/openfloor-python/tree/eab87464fde1c79c32ad6e51cf3161686daf8316).

## Security review and open release issue

The initial npm audit found seven affected dependency packages, including Vite 6.4.2. The updated lockfile resolves Vite 6.4.5 and patched transitive packages; its audit returned zero findings. Vite 6.4 is still a security-supported release line. React 18 and the current Vite major are retained to avoid coupling unrelated major migrations to runtime fixes. [Vite support policy](https://vite.dev/releases).

An OSV batch audit of the installed Python environment found affected Pillow 12.3.0 and pip 25.0.1. The development installer was updated to pip 26.2.2. Pillow's reported fixes are in 13.0.0, but the latest Gradio release, 6.31.0, requires Pillow below 13. The attempted upgrade was reverted after `pip check` reproduced the conflict. Neither ignoring the resolver nor claiming a clean audit is acceptable. [Pillow releases](https://pypi.org/project/pillow/), [Gradio dependency metadata](https://pypi.org/pypi/gradio/6.31.0/json), [OSV API](https://google.github.io/osv.dev/api/).

Gradio has therefore moved to the explicit `web` extra. A fresh CLI/Canvas install does not acquire Pillow through Gradio. The opt-in web environment still has this known security issue, particularly relevant to untrusted image files. It blocks a claim that every supported extra has passed its security release gate. Follow-up: adopt a compatible Gradio/Pillow combination, rerun the audit and media tests, then remove this exception. No vulnerable dependency is silently overridden or marked fixed.

## Distribution decision

The supported full application is an editable source checkout. The Canvas backend, compiled frontend, examples and `agents/` library are repository assets. The core Python wheel can expose CLI commands but is not a standalone Canvas/persona distribution. Wheel bundling is deferred and must not be advertised. CI builds packages and smoke-tests the tracked frontend through the editable application.

No provider model catalog is an account availability guarantee. Existing configured model IDs are retained. Select a provider-supported model explicitly; paid compatibility checks are opt-in.
