# Interfaces: CLI, web and canvas

All three interfaces run the same session machinery: `FloorManager`, `SessionService`, `session.kickoff()`, `cli._spawn_llm_agent()` and `cli._connect_remote_agent()`. Behavior is identical unless this page says otherwise; only *how* you reach an option differs. Verified by `tests/test_floor_delegation.py` (`TestSurfaceParity`, `TestSessionCommands`), `tests/test_interface_session_cleanup.py` and `tests/test_canvas_api.py`.

## Session options

| Option | `start` (terminal) | `web` (Gradio) | `canvas` |
|---|---|---|---|
| Floor policy | `--policy` | `--policy` | Floor node *policy* (or `canvas --policy`) |
| Agents | `--agent SPEC` (repeatable) | `--agent SPEC`; spawn panel at runtime | Agent nodes (or `canvas --agent`); add dialog at runtime |
| Agent task type | `provider:<type>:Name` / `-type` | `--agent`; spawn panel **Type** | Agent node *agentType* |
| Model | spec `:model` / `-model` | spec; spawn panel **Model** | Agent node *model* |
| Persona `@category/slug` | spec description | spec; spawn panel library picker | Agent node *slug* / prompt picker |
| `-timeout` / `-max-retries` / `-max-tokens` | spec flags | spec flags; spawn panel **Extra flags** | Agent node `timeout` / `maxRetries` / `maxTokens` (preset JSON / API, carried by `canvas --agent`) |
| Remote OFP agents | `--remote` | `--remote`; spawn panel provider **remote** | Agent node provider *remote* (or `canvas --remote`) |
| Topic | `--topic` | `--topic` | Floor node *topic* |
| Turn limit | `--max-turns` | `--max-turns` | Floor node *maxTurns* |
| Human participant / name | default on; `--no-human`, `--human-name` | same | Human node (`canvas --no-human`, `--human-name`) |
| Floor events | `--show-floor-events` | `--show-floor-events` (system events in the chat) | Floor node *showFloorEvents* (or `canvas --show-floor-events`) |

`--max-turns` is counted by the floor manager on every agent utterance and stops the floor exactly on the N-th. Conversation history keeps only 100 entries, which is why the old `len(history)` polling never stopped for N > 100.

## Starting a session

`session.kickoff()` is the only start-up path:
1. Wait briefly so freshly spawned agents can register.
2. Seed the topic and the memory goal.
3. Release a floor the human held at start-up, so agents answer the topic.
4. Give the first grant to `FloorManager.kickoff_target()`: orchestrator, then director, then nobody under a MODERATED chair, then the first ring member under ROUND_ROBIN, then the first agent of an autonomous topic-less session.

The three interfaces used to carry three diverging copies of this logic. For example, `web` never started an orchestrator without a topic, and a director never went first when a show runner was present.

## Runtime controls

| Control | Terminal | Web | Canvas |
|---|---|---|---|
| Speak | type a line | chat box | Human node input (`POST /session/message`) |
| Give an agent the floor | `/grant NAME`, or address it by name (MODERATED) | `/grant NAME` in the chat box, or address it by name | `/grant NAME` in the human input, `POST /session/floor/grant/{name}`, or address it by name |
| Pass your turn | `/pass` | `/pass` | `/pass` |
| Floor status | `/floor`, `/agents` | `/floor`, `/agents`; status panel | `/floor`, `/agents`; floor snapshot / status dock |
| Add an agent | `/add SPEC`, `/spawn …` | spawn panel | add dialog (`POST /session/agent/add`) |
| Connect a remote | `/connect slug`, `/spawn remote slug` | spawn panel, provider *remote* | add dialog, provider *remote* |
| Remove an agent | `/kick`, `/remove NAME [graceful\|force]` | participant controls | node remove (`DELETE /session/agent/{name}`) |
| Quarantine / review / clear | `/quarantine`, `/review`, `/clear` | governance controls | governance dock (`POST /session/governance/{id}`) |
| ASP status | `/status`, `/watch`, `ofp-playground status` | status panel | status dock |
| History | `/history [n]` | chat transcript | conversation / trace nodes |
| Stop | `/quit`, Ctrl+C | Stop | Stop (`DELETE /session/stop`) |

`/grant`, `/pass`, `/floor` and `/agents` are implemented once, in `SessionService.floor_command()`. Every human input routes slash text there before treating it as an utterance.

## Human behavior

The terminal `HumanAgent` and the web/canvas `WebHumanAgent` follow the same floor rules:

- **SEQUENTIAL:** request the floor at start and after others speak; yield after sending.
- **ROUND_ROBIN:** take the ring turn when granted ("your turn"); yield after sending.
- **MODERATED:** the human is the chair. It never needs the floor and gives it away by name or `/grant`.
- **FREE_FOR_ALL / SHOWRUNNER_DRIVEN:** speak at any time. An orchestrator `[ASSIGN Human]` is shown to the human and their reply counts as the delivery.
- **Any granted turn** is released after the human speaks or `/pass`-es. Previously the web/canvas human never yielded, which deadlocked round-robin.

## Known differences

- The canvas frontend has no dedicated inputs yet for `timeout` / `maxRetries` / `maxTokens` or for a grant button. Use preset JSON, `canvas --agent "… -timeout 30"`, the REST API, or `/grant` in the human input.
- The canvas backend sends floor-command results and "your turn" notices as `system_event` WebSocket events, which the current frontend ignores. Floor state is still visible through `floor_snapshot` / `floor_grant`, and `POST /session/message` returns the command result in its JSON response.
- Graph edges in the canvas are informational; they do not define routing.
- `web` and `canvas` bind to loopback only in this local profile.
