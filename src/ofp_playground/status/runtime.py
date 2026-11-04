"""Status instrumentation at owned agent and floor execution boundaries."""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ofp_playground.status.client import AgentStatusPublisher, StatusConnection

if TYPE_CHECKING:
    from ofp_playground.agents.base import BasePlaygroundAgent


class SessionStatusRuntime:
    def __init__(self, connection: StatusConnection, conversation_id: str, *, parent: SessionStatusRuntime | None = None):
        self.connection = connection
        self.conversation_id = conversation_id
        self.publishers: dict[str, AgentStatusPublisher] = {}
        self.agents: dict[str, BasePlaygroundAgent] = {}
        self.pending_floors: dict[str, list[str]] = {}
        self.floor_states: dict[str, str] = {}
        # Supervisor sequence is shared across conversations in this connection.
        # A unique principal per session is unnecessary: ordering is per agent.
        self.supervisor_sequences: dict[str, int] = {}
        self.parent = parent
        self.children: list[SessionStatusRuntime] = []
        self._historical_agents: dict[str, BasePlaygroundAgent] = {}

    def child(self, conversation_id: str) -> SessionStatusRuntime:
        runtime = SessionStatusRuntime(self.connection, conversation_id, parent=self)
        self.children.append(runtime)
        return runtime

    def all_agents(self) -> list[BasePlaygroundAgent]:
        return [*self._historical_agents.values(), *self.agents.values(),
                *(agent for child in self.children for agent in child.all_agents())]

    def track(self, agent: BasePlaygroundAgent) -> None:
        previous = self.agents.get(agent.speaker_uri)
        if previous is not None and previous.instance_id != agent.instance_id:
            self._historical_agents[previous.instance_id] = previous
        self.agents[agent.speaker_uri] = agent

    def report(self, agent_id: str, dimension: str, value: str, previous: str,
               *, reason: str, conversation_id: str | None = None, ext: dict | None = None) -> None:
        seq = self.supervisor_sequences.get(agent_id, -1) + 1
        self.supervisor_sequences[agent_id] = seq
        payload = {
            "aspVersion": "0.1", "type": "floor_change" if dimension == "floor" else "state_change",
            "agentId": agent_id, "eventId": str(uuid.uuid4()), "seq": seq,
            "ts": datetime.now(UTC).isoformat(), "source": "supervisor",
            "from": previous, "to": value, "reason": reason,
            **({"conversationId": conversation_id} if dimension == "floor" else {"dimension": dimension}),
            **({"ext": ext} if ext else {}),
        }
        self.connection.submit(payload, self.connection.config["supervisorToken"])

    def floor_transition(self, speaker_uri: str, value: str) -> None:
        agent = self.agents.get(speaker_uri)
        if agent is None:
            return  # a remote/legacy participant without instrumentation is unknown
        if speaker_uri not in self.publishers:
            pending = self.pending_floors.setdefault(speaker_uri, [])
            if len(pending) < 128:
                pending.append(value)
            return
        previous = self.floor_states.get(speaker_uri, "joined")
        if previous == value:
            return
        self.report(agent.instance_id, "floor", value, previous, reason="floor-change",
                    conversation_id=self.conversation_id)
        self.floor_states[speaker_uri] = value

    async def run_agent(self, agent: BasePlaygroundAgent, runner: Callable[[], Awaitable[None]] | None = None) -> None:
        # Remote adapters cannot truthfully report the remote process's self state.
        from ofp_playground.agents.demo import DemoAgent
        from ofp_playground.agents.remote import RemoteOFPAgent

        if isinstance(agent, RemoteOFPAgent):
            await (runner() if runner else agent.run())
            return
        token = await self.connection.issue(agent.instance_id)
        if token is None:
            await (runner() if runner else agent.run())  # observation failure must not break the conversation
            return
        publisher = AgentStatusPublisher(self.connection, agent.instance_id, token)
        agent._status = publisher
        self.publishers[agent.speaker_uri] = publisher
        model_task = getattr(agent, "task_type", "text-generation")
        long_work = any(word in model_task for word in ("image", "video", "music", "code"))
        interval = getattr(agent, "_status_heartbeat_interval", 1) if isinstance(agent, DemoAgent) else 2
        stall = getattr(agent, "_status_stall_threshold", 4) if isinstance(agent, DemoAgent) else (1800 if long_work else 120)
        extension = {"speakerUri": agent.speaker_uri, "sessionId": self.conversation_id,
                     "instrumentation": "local-runtime"}
        if self.parent:
            extension["parentConversationId"] = self.parent.conversation_id
        publisher.emit("register", name=agent.name, heartbeatIntervalSec=interval,
                       stallAfterSec=stall,
                       protocols=["OFP/1.1.0", "ASP/0.1"],
                       ext={"local.ofp-playground": extension})
        publisher.transition("floor", "joined", conversation_id=self.conversation_id)
        for value in self.pending_floors.pop(agent.speaker_uri, []):
            self.floor_transition(agent.speaker_uri, value)
        enabled = (lambda: not getattr(agent, "_status_silenced", False)) if isinstance(agent, DemoAgent) else None
        heartbeat = asyncio.create_task(publisher.heartbeat(interval, enabled=enabled),
                                        name=f"asp:heartbeat:{agent.instance_id}")
        failed = False
        try:
            await (runner() if runner else agent.run())
        except asyncio.CancelledError:
            raise
        except Exception:
            failed = True
            self.report(agent.instance_id, "lifecycle", "crashed", "online", reason="process-exit")
            raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if not failed:
                publisher.transition("floor", "left", conversation_id=self.conversation_id)
                publisher.emit("deregister", reason="shutdown")
            agent._status = None
