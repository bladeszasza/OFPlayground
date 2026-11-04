"""Regression tests for floor logic and multi-agent delegation.

Each test pins one message-flow bug that used to stall or corrupt a session:
silent workers, REJECT ordering, parallel fan-out, unsolicited utterances,
strict round-robin, human moderation, turn limits and UI parity commands.
All tests are provider-free: envelopes are crafted and fed straight into the
floor manager, and agent queues are inspected for what was delivered.
"""
from __future__ import annotations

import asyncio

import pytest
from openfloor import (
    Conversation,
    DialogEvent,
    Envelope,
    Event,
    RequestFloorEvent,
    Sender,
    TextFeature,
    Token,
    UtteranceEvent,
)

from ofp_playground.agents import uris
from ofp_playground.agents.registry import AgentRegistry
from ofp_playground.bus.message_bus import FLOOR_MANAGER_URI, MessageBus
from ofp_playground.floor.manager import FloorManager
from ofp_playground.floor.policy import FloorController, FloorPolicy
from ofp_playground.session import SessionService, kickoff

P = "tag:ofp-playground.local,2025:"
ORCH = P + "llm-director"
WRITER = P + "llm-writer"
EDITOR = P + "llm-editor"
PAINTER = P + "image-painter"
HUMAN = P + "human-chair"


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


# ── helpers ──────────────────────────────────────────────────────────────────

def _env(sender: str, events: list) -> Envelope:
    return Envelope(
        sender=Sender(speakerUri=sender, serviceUrl="local://test"),
        conversation=Conversation(id="conv:test"),
        events=events,
    )


def utter(sender: str, text: str, media: tuple[str, str] | None = None) -> Envelope:
    features = {"text": TextFeature(tokens=[Token(value=text)])}
    if media:
        key, path = media
        features[key] = TextFeature(mimeType=f"{key}/png", tokens=[Token(value=path)])
    return _env(sender, [UtteranceEvent(dialogEvent=DialogEvent(speakerUri=sender, features=features))])


def yielded(sender: str) -> Envelope:
    return _env(sender, [Event(eventType="yieldFloor", reason="@complete")])


def requested(sender: str) -> Envelope:
    return _env(sender, [RequestFloorEvent(reason="test")])


def drain(queue: asyncio.Queue) -> list[tuple[str, str]]:
    """Return [(eventType, text)] for everything delivered to a queue."""
    out = []
    while not queue.empty():
        env = queue.get_nowait()
        for event in env.events or []:
            text = ""
            de = getattr(event, "dialogEvent", None)
            if de and de.features.get("text"):
                text = " ".join(t.value for t in de.features["text"].tokens if t.value)
            out.append((event.eventType, text))
    return out


async def make_floor(policy: FloorPolicy, *names: tuple[str, str]):
    bus = MessageBus()
    fm = FloorManager(bus, policy=policy)
    queues = {}
    for uri, name in names:
        queues[uri] = asyncio.Queue()
        await bus.register(uri, queues[uri])
        fm.register_agent(uri, name)
    return bus, fm, queues


async def showrunner_floor(*extra: tuple[str, str]):
    bus, fm, queues = await make_floor(
        FloorPolicy.SHOWRUNNER_DRIVEN, (ORCH, "Director"), (WRITER, "Writer"), *extra,
    )
    fm.register_orchestrator(ORCH)
    return bus, fm, queues


# ── FloorController ──────────────────────────────────────────────────────────

class TestStrictRoundRobin:
    def test_ring_members_never_jump_the_ring(self):
        fc = FloorController(FloorPolicy.ROUND_ROBIN)
        for uri in ("a", "b", "c"):
            fc.add_to_rotation(uri)
        assert fc.request_floor("a") is True  # idle floor: bootstrap
        assert fc.request_floor("c") is False
        assert fc.queue == []  # not queued — the ring reaches c anyway
        assert fc.yield_floor("a") == "b"  # successor, not the requester
        assert fc.yield_floor("b") == "c"
        assert fc.yield_floor("c") == "a"

    def test_successor_follows_actual_speaker(self):
        fc = FloorController(FloorPolicy.ROUND_ROBIN)
        for uri in ("a", "b", "c", "d"):
            fc.add_to_rotation(uri)
        fc.grant_to("c")  # e.g. a moderator override mid-ring
        assert fc.yield_floor("c") == "d"


class TestModeratedController:
    def test_manual_mode_never_auto_grants(self):
        fc = FloorController(FloorPolicy.MODERATED)
        fc.manual = True
        assert fc.request_floor("a") is False
        assert fc.current_holder is None
        assert fc.queue == [("a", "")]

    def test_call_on_grants_or_queues_and_is_served_first(self):
        fc = FloorController(FloorPolicy.MODERATED)
        fc.manual = True
        fc.request_floor("waiting")
        assert fc.call_on("a") is True
        assert fc.call_on("b") is False
        assert fc.directed_queue == ["b"]
        # a yields → the called-on agent speaks, not the one who merely requested
        assert fc.yield_floor("a") == "b"
        assert fc.yield_floor("b") is None  # moderator decides again

    def test_without_moderator_behaves_fifo(self):
        fc = FloorController(FloorPolicy.MODERATED)
        assert fc.request_floor("a") is True
        assert fc.request_floor("b") is False
        assert fc.yield_floor("a") == "b"


# ── SHOWRUNNER_DRIVEN delegation ─────────────────────────────────────────────

class TestShowrunnerDelegation:
    async def test_silent_worker_returns_floor_to_orchestrator(self):
        _bus, fm, q = await showrunner_floor()
        await fm.process_envelope(utter(ORCH, "[ASSIGN Writer]: write chapter 1"))
        drain(q[ORCH])
        # Worker errors out: yields without ever producing an utterance.
        await fm.process_envelope(yielded(WRITER))
        events = drain(q[ORCH])
        assert any("Writer yielded the floor without delivering" in t for _, t in events)
        assert events[-1][0] == "grantFloor"
        assert fm._assigned_uri is None
        assert fm.floor_holder == ORCH

    async def test_worker_delivery_then_yield_is_not_double_counted(self):
        _bus, fm, q = await showrunner_floor()
        await fm.process_envelope(utter(ORCH, "[ASSIGN Writer]: write chapter 1"))
        drain(q[ORCH])
        await fm.process_envelope(utter(WRITER, "Chapter one."))
        await fm.process_envelope(yielded(WRITER))
        grants = [e for e, _ in drain(q[ORCH]) if e == "grantFloor"]
        assert len(grants) == 1
        assert fm._last_worker_text == "Chapter one."

    async def test_reject_sends_directive_before_grant_with_original_task(self):
        _bus, fm, q = await showrunner_floor()
        await fm.process_envelope(utter(ORCH, "[ASSIGN Writer]: write chapter 1"))
        await fm.process_envelope(utter(WRITER, "draft"))
        drain(q[WRITER])
        await fm._handle_orchestrator_directives("[REJECT Writer]: more dialogue")
        events = drain(q[WRITER])
        kinds = [e for e, _ in events]
        assert kinds.index("utterance") < kinds.index("grantFloor")
        directive = next(t for e, t in events if e == "utterance")
        assert "Revision requested — more dialogue" in directive
        assert "Original task: write chapter 1" in directive
        assert fm._last_worker_text == ""  # rejected draft can't be [ACCEPT]ed

    async def test_unsolicited_utterances_keep_assignment(self):
        _bus, fm, q = await showrunner_floor((EDITOR, "Editor"), (HUMAN, "Chair"))
        await fm.process_envelope(utter(ORCH, "[ASSIGN Writer]: write chapter 1"))
        drain(q[ORCH])
        await fm.process_envelope(utter(HUMAN, "make it spooky"))
        await fm.process_envelope(utter(EDITOR, "I have thoughts"))
        assert fm._assigned_uri == WRITER
        assert not [e for e, _ in drain(q[ORCH]) if e == "grantFloor"]
        assert fm._last_worker_text == ""

    async def test_human_input_wakes_idle_orchestrator(self):
        _bus, fm, q = await showrunner_floor((HUMAN, "Chair"))
        await fm.process_envelope(utter(HUMAN, "Let's start"))
        assert ("grantFloor", "") in drain(q[ORCH])

    async def test_text_worker_directive_carries_context_media_gets_bare_prompt(self):
        _bus, fm, q = await showrunner_floor((PAINTER, "Painter"))
        fm._artifact_store.save(agent_name="Writer", content="Earlier phase output")
        fm._memory_store.store("decisions", "palette", "teal and amber")
        await fm._handle_orchestrator_directives("[ASSIGN Writer]: continue the story")
        writer_directive = next(t for e, t in drain(q[WRITER]) if e == "utterance")
        assert "PHASE ARTIFACTS" in writer_directive
        assert "SESSION MEMORY" in writer_directive
        fm._assigned_uri = None
        await fm._handle_orchestrator_directives("[ASSIGN Painter]: a teal lighthouse at dusk")
        painter_directive = next(t for e, t in drain(q[PAINTER]) if e == "utterance")
        assert painter_directive == "[DIRECTIVE for Painter]: a teal lighthouse at dusk"

    async def test_parallel_assign_uses_plain_directives_and_combines_outputs(self):
        _bus, fm, q = await showrunner_floor((EDITOR, "Editor"))
        await fm.process_envelope(
            utter(ORCH, "[ASSIGN_PARALLEL Writer, Editor]: Pitch a title\nfor the book")
        )
        for uri, name in ((WRITER, "Writer"), (EDITOR, "Editor")):
            events = drain(q[uri])
            directive = next(t for e, t in events if e == "utterance")
            assert directive.startswith(f"[DIRECTIVE for {name}]: Pitch a title\nfor the book")
            assert ("grantFloor", "") in events
        drain(q[ORCH])
        await fm.process_envelope(utter(WRITER, "Title A"))
        await fm.process_envelope(yielded(WRITER))
        assert not [e for e, _ in drain(q[ORCH]) if e == "grantFloor"]  # Editor still pending
        await fm.process_envelope(yielded(EDITOR))  # Editor failed silently
        assert ("grantFloor", "") in drain(q[ORCH])
        await fm._handle_orchestrator_directives("[ACCEPT]")
        assert fm._manuscript == ["Title A"]

    async def test_parallel_accept_keeps_every_answer(self):
        _bus, fm, _q = await showrunner_floor((EDITOR, "Editor"))
        await fm._handle_orchestrator_directives("[ASSIGN_PARALLEL Writer, Editor]: Pitch a title")
        await fm.process_envelope(utter(WRITER, "Title A"))
        await fm.process_envelope(utter(EDITOR, "Title B"))
        await fm._handle_orchestrator_directives("[ACCEPT]")
        assert "[Writer]:\nTitle A" in fm._manuscript[0]
        assert "[Editor]:\nTitle B" in fm._manuscript[0]

    async def test_recovery_nudge_keeps_chain_alive(self, monkeypatch):
        from ofp_playground.floor import manager

        monkeypatch.setattr(manager, "ORCHESTRATOR_RECOVERY_BASE_DELAY_S", 0)
        _bus, fm, q = await showrunner_floor()
        await fm.grant_to(ORCH)
        await fm.process_envelope(yielded(ORCH))  # idle turn #1 → nudge + regrant
        assert fm.floor_holder == ORCH  # holder recorded, so the next yield counts
        drain(q[ORCH])
        await fm.process_envelope(yielded(ORCH))  # idle turn #2 must nudge again
        events = drain(q[ORCH])
        assert any("Recovery" in t or "yielded without issuing" in t for _, t in events)
        assert events[-1][0] == "grantFloor"

    async def test_session_not_started_alongside_assignment(self):
        _bus, fm, q = await showrunner_floor()
        called = []

        async def breakout(*args):
            called.append(args)
            return "done"

        fm._breakout_callback = breakout
        await fm._handle_orchestrator_directives(
            "[ASSIGN Writer]: write\n[BREAKOUT policy=round_robin max_rounds=2 topic=x]\n"
            "[BREAKOUT_AGENT -provider hf -name A -system a]\n"
            "[BREAKOUT_AGENT -provider hf -name B -system b]"
        )
        assert called == []
        assert any("NOT started" in t for _, t in drain(q[ORCH]))

    async def test_accept_music_variant_is_parsed(self):
        _bus, fm, _q = await showrunner_floor()
        fm._last_worker_text = "lyrics"
        await fm._handle_orchestrator_directives("[ACCEPT music]")
        assert fm._manuscript == []  # artifact only, manuscript stays clean
        assert fm._artifact_store.phase_count == 1


# ── Round-robin rounds, moderation, turn limits ─────────────────────────────

class TestRoundRobinRounds:
    async def test_director_opens_and_showrunner_closes_each_round(self):
        director, show = P + "director-dir", P + "llm-show"
        _bus, fm, _q = await make_floor(
            FloorPolicy.ROUND_ROBIN, (director, "Dir"), (show, "Show"),
            (WRITER, "Writer"), (EDITOR, "Editor"),
        )
        fm.register_director(director)
        fm.register_showrunner(show)
        assert fm._policy.rotation == [WRITER, EDITOR]
        assert fm.kickoff_target(has_topic=True, has_human=False) == director

        await fm.grant_to(director)
        order = []
        for speaker in (director, WRITER, EDITOR, show):
            await fm.process_envelope(utter(speaker, f"{speaker} speaks"))
            await fm.process_envelope(yielded(speaker))
            order.append(fm.floor_holder)
        assert order == [WRITER, EDITOR, show, director]
        assert fm._round_count == 1

    async def test_idle_ring_parks_instead_of_spinning(self):
        _bus, fm, _q = await make_floor(FloorPolicy.ROUND_ROBIN, (WRITER, "W"), (EDITOR, "E"))
        await fm.grant_to(WRITER)
        for _ in range(10):
            holder = fm.floor_holder
            if holder is None:
                break
            await fm.process_envelope(yielded(holder))
        assert fm.floor_holder is None


class TestModeration:
    async def test_chair_calls_on_agents_by_name(self):
        _bus, fm, _q = await make_floor(
            FloorPolicy.MODERATED, (HUMAN, "Chair"), (WRITER, "MacroAnalyst"), (EDITOR, "RiskAnalyst"),
        )
        await fm.process_envelope(requested(EDITOR))
        assert fm.floor_holder is None  # queued, never auto-granted
        await fm.process_envelope(utter(HUMAN, "RiskAnalyst and MacroAnalyst, your view?"))
        assert fm.floor_holder == EDITOR  # mentioned first
        assert fm.floor_queue[0] == WRITER
        await fm.process_envelope(utter(EDITOR, "Risky."))
        await fm.process_envelope(yielded(EDITOR))
        assert fm.floor_holder == WRITER

    async def test_kickoff_waits_for_the_chair(self):
        _bus, fm, _q = await make_floor(FloorPolicy.MODERATED, (HUMAN, "Chair"), (WRITER, "A"))
        assert fm.kickoff_target(has_topic=True, has_human=True) is None


class TestTurnLimit:
    async def test_max_turns_beyond_history_cap(self):
        _bus, fm, _q = await make_floor(FloorPolicy.FREE_FOR_ALL, (WRITER, "Writer"))
        fm.max_turns = 150  # history keeps only 100 entries
        for n in range(149):
            await fm.process_envelope(utter(WRITER, f"turn {n}"))
        assert not fm._stop_event.is_set()
        await fm.process_envelope(utter(WRITER, "turn 150"))
        assert fm._stop_event.is_set()
        assert fm.utterance_count == 150


# ── Session service: shared commands + removal ──────────────────────────────

@pytest.fixture
async def service():
    bus = MessageBus()
    floor = FloorManager(bus, policy=FloorPolicy.SHOWRUNNER_DRIVEN)
    svc = SessionService(floor, bus, AgentRegistry())
    await bus.register(floor.speaker_uri, floor._queue)
    yield svc
    await svc.close()


class TestSessionCommands:
    async def test_floor_commands(self, service):
        service.floor.register_agent(WRITER, "Writer")
        assert await service.floor_command("hello") is None
        assert await service.floor_command("/spawn x") is None  # not a floor command
        assert "Floor granted to Writer" == await service.floor_command("/grant writer")
        assert service.floor.floor_holder == WRITER
        assert "Writer" in await service.floor_command("/floor")
        assert "not found" in await service.floor_command("/grant Ghost")

    async def test_removing_bystander_does_not_regrant_orchestrator(self, service):
        floor = service.floor
        orch_q, editor_q = asyncio.Queue(), asyncio.Queue()
        await service.bus.register(ORCH, orch_q)
        await service.bus.register(EDITOR, editor_q)
        floor.register_agent(ORCH, "Director")
        floor.register_orchestrator(ORCH)
        floor.register_agent(EDITOR, "Editor")
        await floor.grant_to(ORCH)
        drain(orch_q)
        await service._detach(EDITOR)  # e.g. the orchestrator's own [KICK Editor]
        assert not [e for e, _ in drain(orch_q) if e == "grantFloor"]

    async def test_kickoff_grants_orchestrator_and_seeds_topic(self, service):
        floor = service.floor
        orch_q = asyncio.Queue()
        await service.bus.register(ORCH, orch_q)
        floor.register_agent(ORCH, "Director")
        floor.register_orchestrator(ORCH)
        await kickoff(floor, service.bus, topic="Write a novel", delay=0)
        events = drain(orch_q)
        assert events[0] == ("utterance", "Write a novel")
        assert events[-1][0] == "grantFloor"


# ── Agents ──────────────────────────────────────────────────────────────────

class TestAgentDirectiveParsing:
    def _agent(self, name="Writer"):
        from ofp_playground.agents.llm.anthropic import AnthropicAgent

        return AnthropicAgent(name=name, synopsis="s", bus=MessageBus(), conversation_id="c", api_key="k")

    def test_full_directive_with_context_is_captured(self):
        agent = self._agent()
        agent._parse_showrunner_message(
            "[DIRECTIVE for Writer]: Write chapter 2\nkeep it short\n\n"
            "--- PHASE ARTIFACTS (1 unique) ---\n  01_x.md\n--- END PHASE ARTIFACTS ---"
        )
        assert "keep it short" in agent._current_director_instruction
        assert "PHASE ARTIFACTS" in agent._current_director_instruction

    def test_showrunner_multi_directive_stops_at_next_agent(self):
        agent = self._agent()
        agent._parse_showrunner_message(
            "STORY SO FAR: ...\n[DIRECTIVE for Writer]: fight scene\n"
            "[DIRECTIVE for Editor]: trim\n[IMAGE]: a ring"
        )
        assert agent._current_director_instruction == "fight scene"

    async def test_relevance_filter_only_in_free_for_all(self):
        agent = self._agent()
        checks = []

        async def check(text):
            checks.append(text)
            return False

        agent._check_relevance = check
        agent._name_registry = {EDITOR: "Editor"}
        agent.set_floor_policy(FloorPolicy.SEQUENTIAL)
        await agent._handle_utterance(utter(EDITOR, "hi"))
        assert checks == [] and agent._pending_floor_request
        agent._pending_floor_request = False
        agent.set_floor_policy(FloorPolicy.FREE_FOR_ALL)
        await agent._handle_utterance(utter(EDITOR, "hi again"))
        assert checks == ["hi again"] and not agent._pending_floor_request

    async def test_orchestrator_control_broadcast_ignored(self):
        agent = self._agent()
        await agent._handle_utterance(utter(ORCH, "[ACCEPT]\n[SPAWN -provider hf -name X]"))
        assert agent._conversation_history == []
        assert not agent._pending_floor_request

    async def test_perception_output_is_context_not_media(self):
        agent = self._agent()
        await agent._handle_utterance(utter(P + "imageclassification-cls", "cat: 0.98"))
        assert agent._conversation_history  # used to be dropped as "image" output


class TestWebHumanFloor:
    async def test_granted_turn_is_released_after_sending(self):
        from ofp_playground.agents.web_human import WebHumanAgent

        bus = MessageBus()
        fm_q = asyncio.Queue()
        await bus.register(FLOOR_MANAGER_URI, fm_q)
        human = WebHumanAgent("Chair", bus, "c", floor_policy="round_robin")
        notices = []
        human.notice_callback = notices.append
        await human._handle_floor_events(_env(FLOOR_MANAGER_URI, [Event(eventType="grantFloor")]))
        assert human.has_floor and notices
        await human.send_text("my turn")
        assert [e for e, _ in drain(fm_q)] == ["utterance", "yieldFloor"]
        assert not human.has_floor

    async def test_pass_turn(self):
        from ofp_playground.agents.web_human import WebHumanAgent

        human = WebHumanAgent("Chair", MessageBus(), "c", floor_policy="sequential")
        assert await human.pass_turn() is False
        human._has_floor = True
        assert await human.pass_turn() is True


class TestUriKinds:
    @pytest.mark.parametrize("uri,kind", [
        (P + "gimage-x", uris.IMAGE), (P + "gmusic-x", uris.AUDIO), (P + "sora-x", uris.VIDEO),
        (P + "imageclassification-x", uris.PERCEPTION), (P + "avision-x", uris.VISION),
        (P + "human-x", uris.HUMAN), (P + "remote-x", uris.REMOTE), (P + "llm-x", uris.TEXT),
        (FLOOR_MANAGER_URI, uris.FLOOR),
    ])
    def test_uri_kind(self, uri, kind):
        assert uris.uri_kind(uri) == kind


class TestOrchestratorFollowUpTools:
    def test_follow_up_turn_keeps_coding_and_breakout_sessions(self):
        from ofp_playground.agents.llm.showrunner import AnthropicOrchestratorAgent

        orch = AnthropicOrchestratorAgent(
            name="Director", mission="m", bus=MessageBus(), conversation_id="c",
            api_key="k", model="claude-haiku-4-5-20251001",
        )
        orch.set_name_registry({})
        coding = orch._tool_call_to_directive("create_coding_session", {
            "topic": "build", "agents": [{"provider": "anthropic", "name": "Dev", "system": "code"}],
        })
        assert coding.startswith("[CODING_SESSION")
        assert orch._tool_call_to_directive("read_artifact", {"slug": "x"}) == ""


class TestSurfaceParity:
    def test_canvas_config_carries_remotes_and_tuning(self):
        from ofp_playground import cli

        config = cli._build_canvas_config(
            policy="round_robin", topic="t", no_human=True, max_turns=5, human_name="U",
            agents=("-provider hf -name A -system x -timeout 30 -max-retries 2 -max-tokens 900",),
            remotes=("polly",), show_floor_events=True,
        )
        floor = next(n for n in config["nodes"] if n["type"] == "FloorNode")
        assert floor["data"]["showFloorEvents"] is True
        agent = next(n for n in config["nodes"] if n["data"].get("name") == "A")["data"]
        assert (agent["timeout"], agent["maxRetries"], agent["maxTokens"]) == (30.0, 2, 900)
        remote = next(n for n in config["nodes"] if n["data"].get("provider") == "remote")
        assert remote["data"]["remoteTarget"] == "polly"

    async def test_hf_text_to_music_is_rejected(self, monkeypatch):
        from ofp_playground import cli
        from ofp_playground.config.settings import Settings
        from ofp_playground.renderer.terminal import TerminalRenderer

        monkeypatch.setenv("HF_API_KEY", "x")
        bus = MessageBus()
        floor = FloorManager(bus)
        with pytest.raises(ValueError, match="text-to-music"):
            await cli._spawn_llm_agent(
                "hf:text-to-music", "Music", "pop", floor, bus, AgentRegistry(),
                TerminalRenderer(), Settings.load(),
            )

    def test_web_and_canvas_commands_accept_remotes(self):
        from click.testing import CliRunner

        from ofp_playground import cli

        for command in ("web", "canvas", "start"):
            help_text = CliRunner().invoke(cli.main, [command, "--help"]).output
            assert "--remote" in help_text, command
            assert "--show-floor-events" in help_text, command
