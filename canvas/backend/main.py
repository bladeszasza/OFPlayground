"""FastAPI app for the canvas backend."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, Field
from rich.console import Console

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from canvas.backend.auth import CanvasAccess
from canvas.backend.session_bridge import SessionBridge
from ofp_playground import __version__
from ofp_playground.agents.registry import AgentRegistry
from ofp_playground.bus.message_bus import MessageBus
from ofp_playground.config.settings import Settings
from ofp_playground.floor.manager import FloorManager
from ofp_playground.floor.policy import FloorPolicy
from ofp_playground.renderer.terminal import TerminalRenderer

logger = logging.getLogger(__name__)

RESULT_DIR = (_REPO_ROOT / "result").resolve()
_DIST_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"


# ── Request / Response models ──────────────────────────────────────────────────

class NodeData(BaseModel):
    policy: str = "SEQUENTIAL"
    topic: str = ""
    maxTurns: int | None = Field(default=None, gt=0)
    showFloorEvents: bool = False
    noHuman: bool = True
    provider: str = ""
    name: str = ""
    model: str = ""
    systemPrompt: str = Field(default="", validation_alias=AliasChoices("systemPrompt", "system"))
    slug: str = ""
    humanName: str = "User"
    agentType: str = ""
    remoteTarget: str = ""
    # Same per-agent tuning as the CLI spec flags -timeout / -max-retries / -max-tokens
    timeout: float | None = Field(default=None, gt=0)
    maxRetries: int | None = Field(default=None, ge=0)
    maxTokens: int | None = Field(default=None, gt=0)


class CanvasNode(BaseModel):
    id: str
    type: str
    data: NodeData


class CanvasEdge(BaseModel):
    source: str
    target: str


class StartRequest(BaseModel):
    nodes: list[CanvasNode]
    edges: list[CanvasEdge]


class AddAgentRequest(BaseModel):
    provider: str
    name: str
    model: str = ""
    systemPrompt: str = ""
    slug: str = ""
    agentType: str = ""
    remoteTarget: str = ""
    timeout: float | None = Field(default=None, gt=0)
    maxRetries: int | None = Field(default=None, ge=0)
    maxTokens: int | None = Field(default=None, gt=0)


class HumanMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class GovernanceRequest(BaseModel):
    value: str
    source: str = "operator"


class PersonaPreviewRequest(BaseModel):
    reference: str = Field(min_length=2, max_length=5000)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_media_path(rel_path: str) -> Path:
    from pathlib import PureWindowsPath

    if Path(rel_path).is_absolute() or PureWindowsPath(rel_path).drive:
        raise HTTPException(status_code=400, detail="Absolute paths not allowed")
    # Interpret both separators so saved Windows paths are also safe on Linux.
    resolved = (RESULT_DIR / rel_path.replace("\\", "/")).resolve()
    try:
        resolved.relative_to(RESULT_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Path outside result dir") from exc
    return resolved


def _parse_policy(value: str) -> FloorPolicy:
    try:
        return FloorPolicy[value.upper()]
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"Unknown floor policy: {value}") from exc


def _scope_status_frame(lines: list[str], agent_ids: set[str]) -> bytes | None:
    """Keep stream snapshots/events within this Canvas session's identities."""
    result = list(lines)
    for index, line in enumerate(lines):
        if not line.startswith("data: "):
            continue
        try:
            data = json.loads(line[6:])
        except ValueError:
            return None
        if "agentId" in data and data["agentId"] not in agent_ids:
            return None
        if data.get("kind") == "snapshot":
            data["agents"] = [agent for agent in data.get("agents", []) if agent["agentId"] in agent_ids]
            result[index] = "data: " + json.dumps(data, separators=(",", ":"))
    return ("\n".join(result) + "\n\n").encode()


async def _cancel_task(task: asyncio.Task[Any] | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def _floor_policy_from_config(config: dict) -> str:
    for node in config.get("nodes", []):
        if node.get("type") == "FloorNode":
            return node.get("data", {}).get("policy", "")
    return ""


async def _spawn_agent_for_canvas(
    provider: str,
    name: str,
    description: str,
    model: str,
    agent_type: str,
    floor: FloorManager,
    bus: MessageBus,
    registry: AgentRegistry,
    renderer: TerminalRenderer,
    settings: Settings,
    bridge: SessionBridge,
    timeout: float | None = None,
    max_retries: int | None = None,
    max_tokens: int | None = None,
) -> None:
    """Spawn an LLM agent and register it on the running floor."""
    # Lazy import to avoid circular deps at module load time
    from ofp_playground.cli import _spawn_llm_agent

    bridge.push_raw({"type": "agent_spawning", "name": name, "provider": provider})
    try:
        resolved_type = f"{provider}:{agent_type}" if agent_type else provider
        agent = await _spawn_llm_agent(
            agent_type=resolved_type,
            name=name,
            description=description or f"I am {name}, an AI assistant.",
            floor=floor,
            bus=bus,
            registry=registry,
            renderer=renderer,
            settings=settings,
            model_override=model or None,
            max_tokens_override=max_tokens,
            timeout_override=timeout,
            max_retries_override=max_retries,
        )
        bridge.register_agent(agent.speaker_uri, agent.name)
        bridge.push_raw({
            "type": "agent_joined", "name": agent.name, "provider": provider,
            "instance_id": agent.instance_id, "speaker_uri": agent.speaker_uri,
        })
    except Exception as exc:
        logger.error("Failed to spawn agent %s: %s", name, exc)
        bridge.push_raw({"type": "error", "agent": name, "message": str(exc), "retryable": False})
        raise


async def _spawn_remote_for_canvas(
    target: str,
    floor: FloorManager,
    bus: MessageBus,
    registry: AgentRegistry,
    renderer: TerminalRenderer,
    bridge: SessionBridge,
) -> None:
    """Connect a remote OFP agent (by slug or URL) and register it with the bridge."""
    from ofp_playground.cli import _connect_remote_agent, _resolve_remote

    display_name, _url = _resolve_remote(target)
    bridge.push_raw({"type": "agent_spawning", "name": display_name, "provider": "remote"})
    try:
        agent = await _connect_remote_agent(target, floor, bus, registry, renderer)
        bridge.register_agent(agent.speaker_uri, agent.name)
        bridge.push_raw({
            "type": "agent_joined", "name": agent.name, "provider": "remote",
            "instance_id": agent.instance_id, "speaker_uri": agent.speaker_uri,
        })
    except Exception as exc:
        logger.error("Failed to connect remote agent %s: %s", target, exc)
        bridge.push_raw({
            "type": "error", "agent": display_name,
            "message": str(exc), "retryable": False,
        })
        raise


async def _run_canvas_session(
    *,
    bridge: SessionBridge,
    nodes: list[CanvasNode],
    policy: FloorPolicy,
    topic: str,
    max_turns: int | None,
    show_floor_events: bool,
    no_human: bool,
    human_name: str,
    settings: Settings,
    state: dict[str, Any],
) -> None:
    """Start a full OFP session from a canvas graph."""
    null_console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
    renderer = TerminalRenderer(console=null_console, show_floor_events=show_floor_events)

    bus = MessageBus()
    floor = FloorManager(bus, policy=policy, renderer=renderer)
    floor._on_artifact_saved = bridge.push_raw

    bridge.set_inner_collector(floor.trace_collector)
    bus.set_collector(bridge)

    registry = AgentRegistry()

    # Attach spawn/breakout callbacks so orchestrators can spawn agents
    from ofp_playground.cli import _attach_floor_callbacks
    session = await _attach_floor_callbacks(floor, bus, registry, settings, renderer)

    state["bus"] = bus
    state["floor"] = floor
    state["registry"] = registry
    state["collector"] = floor.trace_collector
    state["session"] = session

    try:
        # Spawn agents from the graph before starting the floor loop
        agent_nodes = [n for n in nodes if n.type == "AgentNode"]
        human_nodes = [n for n in nodes if n.type == "HumanNode"]

        spawn_tasks = []
        for node in agent_nodes:
            d = node.data
            if not d.provider:
                continue
            if d.provider == "remote":
                target = d.remoteTarget or d.name
                if not target:
                    continue
                spawn_tasks.append(
                    _spawn_remote_for_canvas(
                        target=target,
                        floor=floor,
                        bus=bus,
                        registry=registry,
                        renderer=renderer,
                        bridge=bridge,
                    )
                )
            else:
                if not d.name:
                    continue
                description = d.systemPrompt or (f"@{d.slug.lstrip('@')}" if d.slug else "")
                spawn_tasks.append(
                    _spawn_agent_for_canvas(
                        provider=d.provider,
                        name=d.name,
                        description=description,
                        model=d.model,
                        agent_type=d.agentType,
                        floor=floor,
                        bus=bus,
                        registry=registry,
                        renderer=renderer,
                        settings=settings,
                        bridge=bridge,
                        timeout=d.timeout,
                        max_retries=d.maxRetries,
                        max_tokens=d.maxTokens,
                    )
                )

        if spawn_tasks:
            results = await asyncio.gather(*spawn_tasks, return_exceptions=True)
            errors = [r for r in results if isinstance(r, Exception)]
            if errors:
                bridge.push_raw(
                    {
                        "type": "error",
                        "message": f"Failed to spawn {len(errors)} agent(s); aborting session.",
                    }
                )
                return

        # Human agent (WebHumanAgent queue-based, not stdin)
        if not no_human and human_nodes:
            from ofp_playground.agents.web_human import WebHumanAgent
            human_name_val = human_nodes[0].data.humanName or human_name
            human = WebHumanAgent(
                name=human_name_val,
                bus=bus,
                conversation_id=floor.conversation_id,
                floor_policy=policy.value,  # same turn-taking as the terminal human
            )
            human.notice_callback = lambda text: bridge.push_raw(
                {"type": "system_event", "text": text, "ts": time.time() * 1000}
            )
            floor.register_agent(human.speaker_uri, human.name)
            registry.register(human)
            state["human_agent"] = human
            bridge.register_agent(human.speaker_uri, human.name)
            bridge.push_raw({"type": "agent_joined", "name": human.name, "provider": "human",
                             "instance_id": human.instance_id, "speaker_uri": human.speaker_uri})
            registry.start(human)

        # Seed topic + max_turns watchdog + initial floor kick-start.
        # Always run _orchestrate so the floor is kick-started even without a topic.
        human_agent = state.get("human_agent")

        # Same start-up and turn limit as `start` / `web` (session.kickoff).
        floor.max_turns = max_turns
        from ofp_playground.cli import _seed_topic
        from ofp_playground.session import kickoff

        def _notify(text: str) -> None:
            bridge.push_raw({"type": "system_event", "text": text, "ts": time.time() * 1000})

        async def _observe_floor() -> None:
            previous = ""
            while not floor._stop_event.is_set():
                snapshot = {
                    "type": "floor_snapshot", "conversation_id": floor.conversation_id,
                    "policy": floor._policy.policy.value, "holder": floor.floor_holder,
                    "holders": list(floor.floor_holders),
                    "holder_name": floor._agents.get(floor.floor_holder or ""),
                    "queue": floor.floor_queue,
                    "queue_names": [floor._agents.get(uri, uri) for uri in floor.floor_queue],
                    "participants": [{"speaker_uri": uri, "name": name} for uri, name in floor._agents.items()],
                    "restricted": sorted(floor._draining_agents),
                    "turn_count": floor.utterance_count,
                }
                current = json.dumps(snapshot, sort_keys=True)
                if current != previous:
                    bridge.push_raw({**snapshot, "ts": time.time() * 1000})
                    previous = current
                await asyncio.sleep(0.25)

        await session.run(
            kickoff(floor, bus, topic=topic, human=human_agent, notify=_notify, seed=_seed_topic),
            _observe_floor(),
        )
    finally:
        await session.close()
        bridge.push_raw({"type": "session_ended"})


# ── App factory ────────────────────────────────────────────────────────────────

def build_app(initial_config: dict | None = None, *, authenticate: bool = True) -> FastAPI:
    bridge = SessionBridge()
    settings = Settings.load()
    control_lock = asyncio.Lock()
    state: dict[str, Any] = {
        "session_id": None,
        "session_task": None,
        "broadcast_task": None,
        "floor": None,
        "bus": None,
        "registry": None,
        "collector": None,
        "human_agent": None,
        "initial_config": initial_config,
    }

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            floor: FloorManager | None = state.get("floor")
            if floor is not None:
                floor.stop()

            session_task: asyncio.Task[Any] | None = state.get("session_task")
            if session_task is not None and not session_task.done():
                with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(session_task, timeout=2.0)
            await _cancel_task(state.get("broadcast_task"))

    app = FastAPI(title="OFP Canvas", version=__version__, lifespan=lifespan)
    access = CanvasAccess(authenticate)
    app.state.access = access
    app.middleware("http")(access.middleware)

    @app.get("/auth/bootstrap")
    async def authorize_browser():
        return {"authorized": True}

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8765", "http://127.0.0.1:8765",
                       "http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    # ── WebSocket ──────────────────────────────────────────────────────────────

    @app.websocket("/ws/{session_id}")
    async def ws_endpoint(websocket: WebSocket, session_id: str) -> None:
        if access.enabled and (
            not access.authorized(websocket.headers, websocket.cookies)
            or not access.allowed_origin(websocket.headers.get("origin"), websocket.headers.get("host", ""),
                                         "https" if websocket.url.scheme == "wss" else "http")
        ):
            await websocket.close(code=1008, reason="Canvas authorization required")
            return
        active_session_id = state.get("session_id")
        if active_session_id is None or session_id != active_session_id:
            await websocket.close(code=1008, reason="Unknown session")
            return

        try:
            after = int(websocket.query_params.get("after", "0"))
            if after < 0:
                raise ValueError("negative cursor")
        except ValueError:
            await websocket.close(code=1008, reason="Invalid replay cursor")
            return
        await bridge.connect(websocket, after=after)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            bridge.disconnect(websocket)

    # ── Session lifecycle ──────────────────────────────────────────────────────

    @app.get("/session/status")
    async def session_status() -> dict[str, Any]:
        session_task: asyncio.Task[Any] | None = state.get("session_task")
        return {
            "running": bool(session_task and not session_task.done()),
            "session_id": state.get("session_id"),
            "started_at": state.get("started_at"),
        }

    @app.get("/session/initial")
    async def session_initial() -> dict | None:
        return state.get("initial_config")

    _PRESETS_DIR = Path(__file__).resolve().parents[2] / "canvas" / "presets"

    @app.get("/presets/list")
    async def presets_list() -> list[dict]:
        result = []
        for f in sorted(_PRESETS_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                result.append({
                    "name": f.stem,
                    "title": data.get("_title", f.stem),
                    "description": data.get("_description", ""),
                    "policy": _floor_policy_from_config(data),
                })
            except Exception:
                logger.exception("Operation failed at an application error boundary")
        return result

    @app.get("/presets/{name}")
    async def presets_get(name: str) -> dict:
        if re.fullmatch(r"[\w-]+", name) is None:
            raise HTTPException(status_code=400, detail="Invalid preset name")
        path = _PRESETS_DIR / f"{name}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Preset '{name}' not found")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.post("/session/start")
    async def session_start(req: StartRequest) -> dict[str, str]:
        async with control_lock:
            session_task: asyncio.Task[Any] | None = state.get("session_task")
            if session_task is not None and not session_task.done():
                raise HTTPException(status_code=409, detail="Session already running")

            floors = [node for node in req.nodes if node.type == "FloorNode"]
            if len(floors) != 1:
                raise HTTPException(status_code=422, detail="Exactly one FloorNode is required")
            floor_node = floors[0]
            policy = _parse_policy(floor_node.data.policy)
            names = [n.data.name.strip().casefold() for n in req.nodes if n.type == "AgentNode"]
            if any(not n for n in names) or len(set(names)) != len(names):
                raise HTTPException(status_code=422, detail="Agent names must be nonempty and unique")
            ids = [n.id for n in req.nodes]
            if len(set(ids)) != len(ids):
                raise HTTPException(status_code=422, detail="Node IDs must be unique")

            await _cancel_task(state.get("broadcast_task"))
            bridge.reset()

            session_id = uuid.uuid4().hex[:8]
            state["session_id"] = session_id
            state["started_at"] = time.time()
            state["session"] = None
            state["initial_config"] = req.model_dump()
            state["floor"] = None
            state["bus"] = None
            state["registry"] = None
            state["collector"] = None
            state["human_agent"] = None
            state["broadcast_task"] = asyncio.create_task(bridge.broadcast_loop())
            state["session_task"] = asyncio.create_task(
                _run_canvas_session(
                    bridge=bridge,
                    nodes=req.nodes,
                    policy=policy,
                    topic=floor_node.data.topic,
                    max_turns=floor_node.data.maxTurns,
                    show_floor_events=floor_node.data.showFloorEvents,
                    no_human=floor_node.data.noHuman,
                    human_name=floor_node.data.humanName or "User",
                    settings=settings,
                    state=state,
                )
            )

            return {"session_id": session_id}

    @app.delete("/session/stop")
    async def session_stop() -> dict[str, bool]:
        async with control_lock:
            floor: FloorManager | None = state.get("floor")
            if floor is not None:
                floor.stop()

            session_task: asyncio.Task[Any] | None = state.get("session_task")
            if session_task is not None and not session_task.done():
                try:
                    await asyncio.wait_for(session_task, timeout=2.0)
                except TimeoutError:
                    await _cancel_task(session_task)

            state["session_task"] = None
            state["floor"] = None
            state["bus"] = None
            state["registry"] = None
            state["human_agent"] = None
            return {"ok": True}

    # ── Agent management ────────────────────────────────────────────────────────

    @app.post("/session/agent/add")
    async def agent_add(req: AddAgentRequest) -> dict[str, str]:
        async with control_lock:
            floor: FloorManager | None = state.get("floor")
            bus: MessageBus | None = state.get("bus")
            registry: AgentRegistry | None = state.get("registry")

            task = state.get("session_task")
            if (floor is None or bus is None or registry is None
                    or task is None or task.done() or floor._stop_event.is_set()):
                raise HTTPException(status_code=409, detail="No active session")

            null_console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
            renderer = TerminalRenderer(console=null_console, show_floor_events=False)

            try:
                if req.provider == "remote":
                    target = req.remoteTarget or req.name
                    await _spawn_remote_for_canvas(
                        target=target,
                        floor=floor,
                        bus=bus,
                        registry=registry,
                        renderer=renderer,
                        bridge=bridge,
                    )
                else:
                    description = req.systemPrompt or (f"@{req.slug.lstrip('@')}" if req.slug else "")
                    await _spawn_agent_for_canvas(
                        provider=req.provider,
                        name=req.name,
                        description=description,
                        model=req.model,
                        agent_type=req.agentType,
                        floor=floor,
                        bus=bus,
                        registry=registry,
                        renderer=renderer,
                        settings=settings,
                        bridge=bridge,
                        timeout=req.timeout,
                        max_retries=req.maxRetries,
                        max_tokens=req.maxTokens,
                    )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except Exception as exc:
                logger.exception("Canvas participant add failed")
                raise HTTPException(status_code=500, detail="Agent could not be added; check server logs") from exc

            return {"name": req.name}

    @app.delete("/session/agent/{name}")
    async def agent_kick(name: str, mode: str = "force", timeout: float = 10) -> dict[str, Any]:
        async with control_lock:
            floor: FloorManager | None = state.get("floor")
            registry: AgentRegistry | None = state.get("registry")

            if floor is None or registry is None:
                raise HTTPException(status_code=409, detail="No active session")

            # Resolve URI by name
            agent_uri = floor._resolve_agent_uri_by_name(name)
            if agent_uri is None:
                raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

            if mode not in {"force", "graceful"} or not 0 < timeout <= 60:
                raise HTTPException(status_code=422, detail="Use force or graceful removal with timeout 0–60 seconds")
            session = state.get("session")
            if session is None:
                raise HTTPException(status_code=409, detail="Session controls are initializing")
            operation = await session.remove(agent_uri, mode=mode, timeout=timeout)
            if state.get("human_agent") and state["human_agent"].speaker_uri == agent_uri:
                state["human_agent"] = None
            bridge.push_raw({"type": "agent_kicked", "name": name})
            return {"name": name, "operation": operation}

    @app.post("/session/governance/{agent_id}")
    async def agent_governance(agent_id: str, req: GovernanceRequest) -> dict[str, Any]:
        async with control_lock:
            session = state.get("session")
            runtime = session.status if session else None
            if runtime is None or session is None:
                raise HTTPException(status_code=409, detail="Status and session controls are unavailable")
            uri = next((uri for uri, a in runtime.agents.items() if a.instance_id == agent_id), None)
            if uri is None:
                raise HTTPException(status_code=404, detail="Agent not found in this session")
            if req.value not in {"normal", "quarantined", "under-review"} or req.source not in {"operator", "guardrail"}:
                raise HTTPException(status_code=422, detail="Unsupported governance action")
            try:
                operation = await session.set_governance(uri, req.value, source=req.source)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if req.value == "quarantined" and operation["enforced"]:
                bridge.push_raw({"type": "agent_kicked", "name": runtime.agents[uri].name})
                if state.get("human_agent") and state["human_agent"].speaker_uri == uri:
                    state["human_agent"] = None
            return operation

    @app.post("/session/floor/grant/{name}")
    async def floor_grant(name: str) -> dict[str, Any]:
        """Moderator control: give an agent the floor (same as /grant in the CLI)."""
        async with control_lock:
            session = state.get("session")
            floor = state.get("floor")
            if session is None or floor is None or floor._stop_event.is_set():
                raise HTTPException(status_code=409, detail="No active session")
            if floor._resolve_agent_uri_by_name(name) is None:
                raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
            result = await session.floor_command(f"/grant {name}")
            bridge.push_raw({"type": "system_event", "text": result, "ts": time.time() * 1000})
            return {"ok": True, "result": result}

    @app.post("/session/message")
    async def session_message(req: HumanMessageRequest) -> dict[str, Any]:
        async with control_lock:
            human = state.get("human_agent")
            floor = state.get("floor")
            if human is None or floor is None or human.speaker_uri not in floor.active_agents:
                raise HTTPException(status_code=409, detail="No human agent in this session")

            if not req.text.strip():
                raise HTTPException(status_code=422, detail="Enter a message")

            session = state.get("session")
            if session is None:
                raise HTTPException(status_code=409, detail="Session controls are initializing")
            if req.text.strip().startswith("/"):
                # Same floor commands as the terminal and web chat.
                result = await session.floor_command(req.text, human.speaker_uri)
                if result is None:
                    raise HTTPException(
                        status_code=422,
                        detail="Unknown command — use /grant NAME, /pass, /floor or /agents",
                    )
                bridge.push_raw({"type": "system_event", "text": result, "ts": time.time() * 1000})
                return {"ok": True, "result": result}
            try:
                await session.enqueue_input(human.speaker_uri, req.text)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"ok": True}

    # ── Live trace ─────────────────────────────────────────────────────────────

    @app.get("/trace/live")
    async def trace_live(session: str = "") -> HTMLResponse:
        from ofp_playground.trace.collector import EventCollector
        from ofp_playground.trace.renderer import render_trace_html
        collector: EventCollector | None = state.get("collector")
        if collector is None:
            collector = EventCollector(conversation_id="preview")
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        render_trace_html(collector, tmp_path, live_session_id=session or None)
        content = tmp_path.read_text(encoding="utf-8")
        tmp_path.unlink(missing_ok=True)
        return HTMLResponse(content=content)

    # ── Media serving ──────────────────────────────────────────────────────────

    @app.get("/media/{path:path}")
    async def serve_media(path: str) -> FileResponse:
        resolved = _safe_media_path(path)
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(resolved)

    # ── Agent library & model catalog ─────────────────────────────────────────

    @app.get("/session/configuration")
    async def provider_configuration():
        """Public model choices and key presence, never the credentials themselves."""
        from fastapi.responses import JSONResponse

        tasks = {
            "anthropic": ["text-generation", "image-to-text", "orchestrator", "code-generation"],
            "openai": ["text-generation", "text-to-image", "image-to-text", "text-to-video", "orchestrator", "code-generation"],
            "google": ["text-generation", "text-to-image", "image-to-text", "text-to-music", "text-to-video", "orchestrator", "code-generation"],
            "huggingface": ["text-generation", "text-to-image", "text-to-video", "image-text-to-text", "image-classification", "object-detection", "image-segmentation", "image-to-text", "text-classification", "token-classification", "summarization", "showrunner", "orchestrator"],
            "demo": ["normal", "rate-limit", "input-wait", "silent", "stalled"],
        }
        providers = {}
        for provider, supported in tasks.items():
            defaults = {}
            for task, prefix in {"text-generation": "llm", "orchestrator": "llm", "showrunner": "llm",
                                 "text-to-image": "image", "image-to-text": "vision", "text-to-video": "video", "text-to-music": "music"}.items():
                value = getattr(settings.defaults, f"{prefix}_model_{provider}", None)
                if value is not None and task in supported:
                    defaults[task] = value
            providers[provider] = {"tasks": supported, "defaults": defaults,
                                   "credentialConfigured": bool(getattr(settings, f"get_{provider}_key")()) if provider != "demo" else True}
        return JSONResponse({"providers": providers, "availability": "not-checked"}, headers={"Cache-Control": "no-store"})

    @app.post("/agents/preview")
    async def persona_preview(req: PersonaPreviewRequest):
        import click

        from ofp_playground.cli import _resolve_agent_slug

        if not req.reference.startswith("@"):
            raise HTTPException(status_code=422, detail="Choose an @category/persona reference")
        try:
            resolved = _resolve_agent_slug(req.reference)
        except click.BadParameter as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        from fastapi.responses import JSONResponse
        return JSONResponse({"reference": req.reference, "prompt": resolved}, headers={"Cache-Control": "no-store"})

    @app.get("/remote-agents/list")
    async def remote_agents_list() -> list[dict]:
        from ofp_playground.agents.remote import KNOWN_REMOTE_AGENTS
        return [
            {"slug": slug, "name": info[0], "description": info[2]}
            for slug, info in KNOWN_REMOTE_AGENTS.items()
        ]

    @app.get("/agents/list")
    async def agents_list() -> list[dict]:
        from ofp_playground.agents.library import load as _load_agents
        library = _load_agents()
        return sorted(
            [
                {
                    "slug": slug,
                    "display_name": entry["display_name"],
                    "category": entry["category"],
                }
                for slug, entry in library.items()
            ],
            key=lambda x: x["slug"],
        )

    @app.get("/models/list")
    async def models_list() -> dict[str, list[str]]:
        from ofp_playground.agents.llm.model_catalog import MODEL_CATALOG
        result: dict[str, list[str]] = {
            "anthropic": [], "openai": [], "google": [], "huggingface": [],
        }
        for model_id in MODEL_CATALOG:
            if model_id.startswith("claude"):
                result["anthropic"].append(model_id)
            elif model_id.startswith("gpt"):
                result["openai"].append(model_id)
            elif model_id.startswith("gemini"):
                result["google"].append(model_id)
        return result

    # ── Static frontend (must be last) ────────────────────────────────────────

    @app.get("/status/snapshot")
    async def status_snapshot():
        import httpx

        session = state.get("session")
        runtime = session.status if session else None
        if runtime is None or session is None:
            return {"available": False, "problem": (session.status_problem or "Status instrumentation initializing or disabled") if session else "Start a session to observe agents",
                    "agents": [], "cursor": 0, "participants": []}
        connection = runtime.connection
        participants = [{"agentId": agent.instance_id, "name": agent.name, "speakerUri": agent.speaker_uri}
                        for agent in runtime.all_agents()]
        try:
            async with httpx.AsyncClient(base_url=connection.client.base_url, timeout=2, trust_env=False) as client:
                response = await client.get("/v1/agents", headers={"Authorization": "Bearer " + connection.config["observerToken"]})
                response.raise_for_status()
            snapshot = response.json()
        except (httpx.HTTPError, ValueError):
            return {"available": False, "problem": "Status service disconnected; retained state is stale",
                    "agents": [], "cursor": 0, "participants": participants}
        snapshot["agents"] = [a for a in snapshot["agents"] if a["agentId"] in {p["agentId"] for p in participants}]
        return {**snapshot, "available": True, "problem": connection.problem,
                "operations": getattr(session, "operations", []),
                "activeAgentIds": [a.instance_id for a in session.registry.all()],
                "agentProblems": connection.agent_problems, "participants": participants}

    @app.get("/status/history/{agent_id:path}")
    async def status_history(agent_id: str):
        import httpx

        session = state.get("session")
        runtime = session.status if session else None
        if runtime is None or agent_id not in {a.instance_id for a in runtime.all_agents()}:
            raise HTTPException(status_code=404, detail="Agent status not found")
        connection = runtime.connection
        try:
            async with httpx.AsyncClient(base_url=connection.client.base_url, timeout=2, trust_env=False) as client:
                response = await client.get("/v1/history", params={"agentId": agent_id, "limit": 200, "latest": "true", "transitions": "true"},
                                            headers={"Authorization": "Bearer " + connection.config["observerToken"]})
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Agent status history unavailable") from exc

    @app.get("/status/stream")
    async def status_stream(request: Request):
        import httpx

        from ofp_playground.status.api import BoundedStreamingResponse

        session = state.get("session")
        runtime = session.status if session else None
        if runtime is None:
            raise HTTPException(status_code=503, detail="Status observation has not started")
        connection = runtime.connection
        headers = {"Authorization": "Bearer " + connection.config["observerToken"]}
        last = request.headers.get("last-event-id")
        if last is not None:
            if not last.isdecimal() or len(last) > 20:
                raise HTTPException(status_code=400, detail="Invalid status stream cursor")
            headers["Last-Event-ID"] = last

        async def events():
            try:
                async with (
                    httpx.AsyncClient(base_url=connection.client.base_url, timeout=httpx.Timeout(15, connect=2), trust_env=False) as client,
                    client.stream("GET", "/v1/stream", headers=headers) as response,
                ):
                    response.raise_for_status()
                    lines: list[str] = []
                    async for line in response.aiter_lines():
                        if line:
                            lines.append(line)
                        else:
                            frame = _scope_status_frame(lines, {a.instance_id for a in runtime.all_agents()})
                            lines = []
                            if frame is not None:
                                yield frame
            except httpx.HTTPError:
                yield b"event: unavailable\ndata: {}\n\n"

        return BoundedStreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    if _DIST_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_DIST_DIR), html=True), name="static")

    return app
