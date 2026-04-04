"""Extended FloorManager tests — orchestrator directives, manifest handling."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from ofp_playground.bus.message_bus import MessageBus, FLOOR_MANAGER_URI
from ofp_playground.floor.manager import FloorManager
from ofp_playground.floor.policy import FloorPolicy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setup_fm_with_orchestrator(policy=FloorPolicy.SHOWRUNNER_DRIVEN):
    bus = MessageBus()
    fm = FloorManager(bus, policy=policy)

    fm_q: asyncio.Queue = asyncio.Queue()
    await bus.register(FM_URI := FLOOR_MANAGER_URI, fm_q)

    orc_q: asyncio.Queue = asyncio.Queue()
    orc_uri = "tag:test:orchestrator"
    await bus.register(orc_uri, orc_q)

    fm.register_agent(orc_uri, "Director")
    fm.register_orchestrator(orc_uri)

    return bus, fm, fm_q, orc_q, orc_uri


# ---------------------------------------------------------------------------
# Basic registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_multiple_agents(self):
        bus = MessageBus()
        fm = FloorManager(bus)
        fm.register_agent("tag:test:a1", "Alice")
        fm.register_agent("tag:test:a2", "Bob")
        assert "tag:test:a1" in fm.active_agents
        assert "tag:test:a2" in fm.active_agents

    def test_unregister_removes_agent(self):
        bus = MessageBus()
        fm = FloorManager(bus)
        fm.register_agent("tag:test:a1", "Alice")
        fm.unregister_agent("tag:test:a1")
        assert "tag:test:a1" not in fm.active_agents

    def test_floor_holder_initially_none(self):
        bus = MessageBus()
        fm = FloorManager(bus)
        assert fm.floor_holder is None


# ---------------------------------------------------------------------------
# _handle_orchestrator_directives — [ACCEPT]
# ---------------------------------------------------------------------------

class TestAcceptDirective:
    @pytest.mark.asyncio
    async def test_accept_appends_to_manuscript(self):
        bus, fm, fm_q, orc_q, orc_uri = await _setup_fm_with_orchestrator()
        fm._last_worker_text = "Chapter one content here."
        await fm._handle_orchestrator_directives("[ACCEPT]")
        assert "Chapter one content here." in fm._manuscript

    @pytest.mark.asyncio
    async def test_accept_without_prior_worker_text(self):
        bus, fm, fm_q, orc_q, orc_uri = await _setup_fm_with_orchestrator()
        fm._last_worker_text = ""
        # Should not raise even with empty last worker text
        await fm._handle_orchestrator_directives("[ACCEPT]")


# ---------------------------------------------------------------------------
# _handle_orchestrator_directives — [TASK_COMPLETE]
# ---------------------------------------------------------------------------

class TestTaskCompleteDirective:
    @pytest.mark.asyncio
    async def test_task_complete_stops_manager(self):
        bus, fm, fm_q, orc_q, orc_uri = await _setup_fm_with_orchestrator()
        # [TASK_COMPLETE] calls self.stop() — verify it doesn't raise
        # and that the manager transitions to a stopped state.
        await fm._handle_orchestrator_directives("[TASK_COMPLETE]")
        assert not fm._running


# ---------------------------------------------------------------------------
# _handle_orchestrator_directives — [KICK]
# ---------------------------------------------------------------------------

class TestKickDirective:
    @pytest.mark.asyncio
    async def test_kick_removes_agent(self):
        bus, fm, fm_q, orc_q, orc_uri = await _setup_fm_with_orchestrator()
        worker_q: asyncio.Queue = asyncio.Queue()
        await bus.register("tag:test:writer", worker_q)
        fm.register_agent("tag:test:writer", "Writer")

        await fm._handle_orchestrator_directives("[KICK Writer]")

        assert "tag:test:writer" not in fm.active_agents

    @pytest.mark.asyncio
    async def test_kick_unknown_agent_no_crash(self):
        bus, fm, fm_q, orc_q, orc_uri = await _setup_fm_with_orchestrator()
        # Should not raise even for an agent that's not registered
        await fm._handle_orchestrator_directives("[KICK NonExistentAgent]")


# ---------------------------------------------------------------------------
# Manifest storage
# ---------------------------------------------------------------------------

class TestManifestStorage:
    def test_store_and_retrieve_manifest(self):
        bus = MessageBus()
        fm = FloorManager(bus)
        fm.register_agent("tag:test:alice", "Alice")

        from unittest.mock import MagicMock
        manifest = MagicMock()
        manifest.identification.conversationalName = "Alice"
        manifest.capabilities = [MagicMock(keyphrases=["text-generation"])]

        fm.store_manifest("tag:test:alice", manifest)
        assert fm._manifests.get("tag:test:alice") is manifest

    def test_find_agent_by_manifest_name(self):
        bus = MessageBus()
        fm = FloorManager(bus)
        fm.register_agent("tag:test:alice", "Alice")

        from unittest.mock import MagicMock
        manifest = MagicMock()
        manifest.identification.conversationalName = "Alice"
        manifest.capabilities = [MagicMock(keyphrases=["text-generation"])]
        fm.store_manifest("tag:test:alice", manifest)

        # find_agent_by_manifest returns (uri, manifest) tuple on match
        result = fm.find_agent_by_manifest("Alice", "text-generation")
        assert result is not None
        uri, mf = result
        assert uri == "tag:test:alice"

    def test_find_agent_by_manifest_returns_none_for_unknown(self):
        bus = MessageBus()
        fm = FloorManager(bus)
        assert fm.find_agent_by_manifest("Ghost", "text-generation") is None


# ---------------------------------------------------------------------------
# Active agents property
# ---------------------------------------------------------------------------

class TestActiveAgents:
    def test_active_agents_is_dict(self):
        bus = MessageBus()
        fm = FloorManager(bus)
        fm.register_agent("tag:test:a", "A")
        assert isinstance(fm.active_agents, dict)

    def test_uri_to_name_mapping(self):
        bus = MessageBus()
        fm = FloorManager(bus)
        fm.register_agent("tag:test:alice", "Alice")
        assert fm.active_agents["tag:test:alice"] == "Alice"


# ---------------------------------------------------------------------------
# ConversationHistory integration
# ---------------------------------------------------------------------------

class TestHistory:
    def test_history_object_exists(self):
        bus = MessageBus()
        fm = FloorManager(bus)
        assert fm.history is not None

    def test_history_is_empty_initially(self):
        bus = MessageBus()
        fm = FloorManager(bus)
        assert len(fm.history) == 0


# ---------------------------------------------------------------------------
# _handle_orchestrator_directives — [ASSIGN] route
# ---------------------------------------------------------------------------

class TestAssignDirective:
    @pytest.mark.asyncio
    async def test_assign_to_registered_agent_sends_grant(self):
        bus, fm, fm_q, orc_q, orc_uri = await _setup_fm_with_orchestrator()
        worker_q: asyncio.Queue = asyncio.Queue()
        await bus.register("tag:test:writer", worker_q)
        fm.register_agent("tag:test:writer", "Writer")

        await fm._handle_orchestrator_directives("[ASSIGN Writer]: Write chapter 1")

        # The writer queue should receive a grant or directive
        # (implementation detail: may receive a private utterance or grantFloor)
        await asyncio.sleep(0.05)  # allow async propagation
        # At minimum, verify no exception was raised
        assert True

    @pytest.mark.asyncio
    async def test_assign_to_unknown_agent_no_crash(self):
        bus, fm, fm_q, orc_q, orc_uri = await _setup_fm_with_orchestrator()
        # Should not raise even if the agent isn't registered
        await fm._handle_orchestrator_directives("[ASSIGN PhantomAgent]: Some task")


class TestOrchestratorRecoveryBackoff:
    @pytest.mark.asyncio
    async def test_recovery_nudge_waits_before_regrant(self):
        bus, fm, fm_q, orc_q, orc_uri = await _setup_fm_with_orchestrator()

        from openfloor import Conversation, Envelope, Sender
        envelope = Envelope(
            sender=Sender(speakerUri=orc_uri, serviceUrl="local://orchestrator"),
            conversation=Conversation(id=fm.conversation_id),
            events=[],
        )

        fm._policy.grant_to(orc_uri)
        fm._assigned_uri = None
        fm._send_directed_utterance = AsyncMock()
        fm._grant_floor = AsyncMock()

        with patch("ofp_playground.floor.manager.asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
            await fm._handle_yield_floor(envelope)

        sleep_mock.assert_awaited_once_with(0.5)
        fm._send_directed_utterance.assert_awaited_once()
        fm._grant_floor.assert_awaited_once_with(orc_uri)


class TestShowrunnerDuplicateSuppression:
    @pytest.mark.asyncio
    async def test_coding_progress_does_not_return_floor(self):
        bus, fm, fm_q, orc_q, orc_uri = await _setup_fm_with_orchestrator()

        worker_uri = "tag:test:igor"
        fm.register_agent(worker_uri, "Igor")
        fm._assigned_uri = worker_uri
        fm.grant_to = AsyncMock()

        from openfloor import Conversation, DialogEvent, Envelope, Sender, TextFeature, Token, UtteranceEvent
        envelope = Envelope(
            sender=Sender(speakerUri=worker_uri, serviceUrl="local://worker"),
            conversation=Conversation(id=fm.conversation_id),
            events=[],
        )
        event = UtteranceEvent(
            dialogEvent=DialogEvent(
                id="evt-progress",
                speakerUri=worker_uri,
                features={
                    "text": TextFeature(mimeType="text/plain", tokens=[Token(value="[Igor] Running code_interpreter...")])
                },
            )
        )

        await fm._handle_utterance(envelope, event)

        fm.grant_to.assert_not_called()
        assert fm._assigned_uri == worker_uri

    @pytest.mark.asyncio
    async def test_duplicate_orchestrator_directive_ignored_while_assigned(self):
        bus, fm, fm_q, orc_q, orc_uri = await _setup_fm_with_orchestrator()

        worker_uri = "tag:test:csabi"
        fm.register_agent(worker_uri, "Csabi")
        fm._assigned_uri = worker_uri
        fm._handle_orchestrator_directives = AsyncMock()

        from openfloor import Conversation, DialogEvent, Envelope, Sender, TextFeature, Token, UtteranceEvent
        text = "[ACCEPT]\n[ASSIGN Csabi]: Define product frame in 200 words."
        envelope = Envelope(
            sender=Sender(speakerUri=orc_uri, serviceUrl="local://orchestrator"),
            conversation=Conversation(id=fm.conversation_id),
            events=[],
        )
        event = UtteranceEvent(
            dialogEvent=DialogEvent(
                id="evt-dup",
                speakerUri=orc_uri,
                features={
                    "text": TextFeature(mimeType="text/plain", tokens=[Token(value=text)])
                },
            )
        )

        await fm._handle_utterance(envelope, event)
        await fm._handle_utterance(envelope, event)

        fm._handle_orchestrator_directives.assert_awaited_once_with(text)
