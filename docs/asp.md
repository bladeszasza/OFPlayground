# Agent Status Protocol in OFPlayground

ASP reports operational state independently of OFP conversation content. The implementation follows the [pinned profile](protocols/asp/PROFILE.md), with a separately running local service, authenticated publishers/observers, a SQLite journal and resumable SSE. This is an implementation of a draft, not a conformance certification.

## Provider-free showcase

Install the source checkout with the Canvas extra, then run:

```sh
ofp-playground canvas
```

Load **ASP showcase · no API keys**, then choose **Run session**. These are synthetic local agents; no model account, provider key or paid call is used.

| Participant | Actual runtime behavior | Expected observation |
| --- | --- | --- |
| Worker | Executes scripted work and yields its floor | Working/completed tasks, progress and floor exchange |
| Retry | Raises one synthetic 429, backs off and succeeds | Rate-limited → retrying → working → completed |
| Input | Awaits an operator message | Input-required; healthy, without a false stall |
| Silent | Stops publishing while its local task remains alive | Server-derived unreachable after 2.5 heartbeat intervals; retained scopes stale |
| Stalled | Keeps publishing heartbeats while its work awaits indefinitely | Server-derived stalled after four seconds without progress |
| Operator | Waits on the web input queue | Input-required; send a message to release Input |

Select Stalled in the separate **Agent Status** dock, choose **Guardrail (independent)**, then **Quarantine & stop**. Requested, acknowledged and enforced are distinct. Successful enforcement removes floor/bus membership and cancels the owned task; the retained record shows its independent restriction and offline/canceled outcome. Clear restriction changes the retained assertion and never restarts that instance.

Scripted agents produce at most six responses each and then remain observable without continuing the synthetic discussion. Stop ends all remaining waits and local tasks.

**Drain & remove** excludes new assignments, waits up to ten seconds for the current work and then forces cleanup if necessary. **Force stop** detaches immediately. Submitted provider jobs may continue at the provider; removed agents cannot route late results back into this session.

## Read status and communication

Lifecycle, health and governance are agent-wide. Tasks are keyed by task ID and floor states by conversation ID. Values retain the reporter, source, receive time, reason and freshness. Self health and independent health observations remain separately visible. A green self health value alongside unreachable lifecycle is retained evidence, not proof of current health.

The Canvas dock searches/sorts the fleet and exposes selected-agent history and scoped state. Nodes show a compact status. The graph's membership layer describes participants; directed observed routes use bus outcomes. **Queued** means accepted by a local inbox. Processing remains unconfirmed. Dropped routes retain their failure labels.

Communication controls filter by conversation, agent and event type. Scrubbing and playback inspect bounded recorded observations while the ASP dock remains live. Return to live explicitly. Drag nodes to pin their positions; layout moves unpinned nodes. Fit selection and session groups support focused inspection without each incoming event resetting the viewport. Reduced-motion preferences disable route animation.

A [captured provider-free ASP journal](../tests/fixtures/asp-showcase.ndjson) contains 188 actual register, work, retry, wait, server-inference, independent-governance and shutdown observations. `tests/test_asp_replay_fixture.py` replays it with recorded reporter authority and receive time, then replays it again to verify durable event-ID deduplication. It contains state metadata, not prompts or generated conversation output.

**Save configuration** downloads editable JSON containing authored settings, prompts, remote URLs, positions and pins. Runtime output, status and credential fields are excluded; inspect authored text/URLs before sharing. **Load configuration JSON** validates the file before replacing an idle graph and never starts a provider call. Browser storage also retains the configuration; a page reload reconnects to the app's active session when one exists. **Configuration** hides/shows the sidebar, which starts collapsed on narrow screens.

## CLI and web controls

```sh
ofp-playground status --watch
ofp-playground status --json --agent Worker
ofp-playground personas reviewer --preview
ofp-playground models --json
```

Inside an interactive terminal session, `/status`, `/watch`, `/add`, `/remove NAME graceful`, `/remove NAME force`, `/quarantine NAME`, `/review NAME` and `/clear NAME` use shared services. See `ofp-playground start --help` and [CLI reference](cli.md). The optional Gradio interface exposes participant add/remove, governance, stop and multi-dimensional ASP observations; its dependency exception is documented in [dependencies](dependencies.md).

## Opt-in live example

`python examples/asp_live.py` prints its limits and makes no request. To choose an account-supported OpenAI Responses model and explicitly allow one billable request:

```sh
python examples/asp_live.py --run-paid --model YOUR_MODEL_ID
```

Set `OPENAI_API_KEY` in the environment. The example uses the shared session, actual floor grant and ASP work instrumentation. It caps output at 128 tokens, disables application and SDK retries, sets a 15-second transport timeout and a 30-second local session wait. These are request limits, not a guaranteed cost cap or remote cancellation guarantee. The SDK timeout/retry options follow the [official Python reference](https://developers.openai.com/api/reference/python). This example has provider-free/default-path checks; a paid run is deliberately opt-in.

## Shared ASP servers

The ASP project publishes a reference status server (`implementations/python-status-server` in [AgentStatusProtocol/spec](https://github.com/AgentStatusProtocol/spec)); a shared test instance runs on a free hosting tier. It uses one bearer token for every caller and takes the reporter role from each event's `source`. OFPlayground reads the same `ASP_SERVER` and `ASP_TOKEN` variables as its example clients. Put them in the environment or in `.env` (gitignored); the token is never accepted on the command line and is only sent in the `Authorization` header.

```sh
ofp-playground governance                                 # list agents (server from ASP_SERVER)
ofp-playground governance AGENT_ID quarantined            # operator: quarantine one agent
ofp-playground governance AGENT_ID normal                 # release it
```

States are `normal`, `sandboxed`, `quarantined`, `policy-violation` and `under-review`. As in the reference `governance.py`, the command reads the current value first and uses the current second as `seq`; the server ignores an operator event whose `seq` is not higher than the previous one, so one retry follows a short wait. The first request can take about a minute while a sleeping free instance starts. Nonlocal servers must use HTTPS.

**Mirror a session.** With `OFP_ASP_MIRROR=1` (plus `ASP_SERVER`/`ASP_TOKEN`), every terminal, web and Canvas session also copies its validated status events to that server. The local monitor stays authoritative and the Canvas dock is unchanged. Mirrored agent IDs get the prefix `urn:agent:ofplayground:` (override with `OFP_ASP_MIRROR_PREFIX`) so they do not collide with other participants; the local `ext` fields (speaker URIs, session IDs) are not sent. Configured agent names are. A slow, sleeping or failing shared server never blocks or fails the session: mirroring is best effort and drops events it cannot deliver.

On a shared server everyone with the token can read and change agent state. Change only agents you run, and keep the token private. Reporting governance does not enforce it: a mirrored agent is restricted only by this runtime's own quarantine, not by a remote operator's assertion.

## Service lifecycle and trust

The first instrumented session starts or reuses the independent monitor on `127.0.0.1:8787`. Closing the runner does not stop that monitor: it can still infer liveness failure. Run `ofp-status-server --help` for explicit service configuration. Credentials and the journal live in `~/.ofp-playground/status`; secrets never go in browser URLs or ASP events. File ownership is restricted on POSIX; Windows uses the user's profile directory. This is a local trust profile, not tenant isolation.

All ASP ingest, snapshots, history and streams require credentials. Canvas forwards authorized observations using its HttpOnly same-site browser session, without exposing the monitor's tokens to JavaScript. Publishers use agent-bound credentials; independent authority comes from credentials, rather than a caller-selected JSON label. Nonlocal standalone service binding requires TLS. The CLI's Canvas/Gradio launchers restrict their control surfaces to loopback.

Status contains allowlisted state/reason/activity identifiers and numeric metrics. Prompts, model output, exception text and credentials are excluded. Queue overflow stops publication, so a reassuring heartbeat cannot conceal lost task transitions. Service failure leaves conversation work running and marks observation unavailable/stale.

Local quarantine can still remove/cancel an agent when the status service is unavailable; its operation reports unknown acknowledgement alongside confirmed local enforcement. Clearing a restriction requires a status-service acknowledgement. During an outage it reports `clear-unconfirmed-no-restart`, retains the existing restriction and never restarts the agent. Retry only after observation recovers.

The journal retains 10,000 events by default; snapshots, ordering watermarks and deduplication IDs survive restart/retention. Gap controls accompany expired or future stream cursors. The deduplication table grows with accepted IDs: rotate/archive the local database deliberately for long-lived installations. This release does not implement active probes, OTLP ingest, federated credentials, public hosting, automatic compromise detection or execution-DAG routing.
