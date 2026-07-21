from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.agent.runtime import AgentTaskManager
from app.incidents import Approval, ApprovalDecision, PlanEdit


class BlockingAgent:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[tuple[str, str]] = []

    async def run(self, incident_id: str, trace_id: str) -> None:
        self.calls.append((incident_id, trace_id))
        self.started.set()
        await self.release.wait()


async def _wait_inactive(manager: AgentTaskManager, incident_id: str) -> None:
    # The lifecycle worker runs each operation in a detached task (so a resisting
    # job cannot block the FIFO), so admission is released one scheduling hop
    # after the operation completes. Poll instead of assuming a fixed yield count.
    for _ in range(200):
        if not manager.is_active(incident_id):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("lifecycle job did not become inactive")


def _correction() -> Approval:
    return Approval(
        incident_id="inc-1",
        operator="demo-operator",
        decision=ApprovalDecision.EDIT,
        edits=(PlanEdit(seq=1, instruction="Use a bounded restart"),),
        timestamp=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )


def test_schedule_returns_immediately_and_coalesces_active_incident() -> None:
    async def exercise() -> None:
        agent = BlockingAgent()
        manager = AgentTaskManager(agent, logging.getLogger("praxis.test"))

        assert manager.schedule("inc-1", "trace-1") is True
        assert manager.schedule("inc-1", "trace-duplicate") is False
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        assert manager.is_active("inc-1") is True
        assert agent.calls == [("inc-1", "trace-1")]

        agent.release.set()
        await _wait_inactive(manager, "inc-1")
        assert manager.is_active("inc-1") is False
        await manager.shutdown()

    asyncio.run(exercise())


def test_shutdown_cancels_and_collects_outstanding_tasks() -> None:
    async def exercise() -> None:
        agent = BlockingAgent()
        manager = AgentTaskManager(agent, logging.getLogger("praxis.test"))

        manager.schedule("inc-1", "trace-1")
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        await manager.shutdown()

        assert manager.is_active("inc-1") is False

    asyncio.run(exercise())


def test_regeneration_coalesces_while_initial_triage_is_active() -> None:
    class RegeneratingAgent(BlockingAgent):
        def __init__(self) -> None:
            super().__init__()
            self.regeneration_started = asyncio.Event()
            self.regeneration_release = asyncio.Event()
            self.regeneration_calls: list[tuple[str, str, Approval]] = []

        async def regenerate(
            self,
            incident_id: str,
            trace_id: str,
            correction: Approval,
        ) -> None:
            self.regeneration_calls.append((incident_id, trace_id, correction))
            self.regeneration_started.set()
            await self.regeneration_release.wait()

    async def exercise() -> None:
        agent = RegeneratingAgent()
        manager = AgentTaskManager(agent, logging.getLogger("praxis.test"))

        assert manager.schedule("inc-1", "trace-initial") is True
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        assert manager.schedule_regeneration(
            "inc-1",
            "trace-correction",
            _correction(),
        ) is False
        await asyncio.sleep(0)
        assert agent.regeneration_started.is_set() is False

        agent.release.set()
        await _wait_inactive(manager, "inc-1")
        assert manager.is_active("inc-1") is False
        assert agent.regeneration_calls == []
        await manager.shutdown()

    asyncio.run(exercise())


def test_failure_is_redacted_and_does_not_escape(caplog) -> None:
    class FailingAgent:
        async def run(self, incident_id: str, trace_id: str) -> None:
            raise RuntimeError("provider-body-secret-sentinel")

    async def exercise() -> None:
        manager = AgentTaskManager(
            FailingAgent(),
            logging.getLogger("praxis.runtime-test"),
        )
        manager.schedule("inc-1", "trace-1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await manager.shutdown()

    with caplog.at_level(logging.ERROR, logger="praxis.runtime-test"):
        asyncio.run(exercise())

    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "agent_run_failed" in rendered
    assert "provider-body-secret-sentinel" not in rendered
    failure = next(record for record in caplog.records if record.getMessage() == "agent_run_failed")
    assert failure.error_type == "RuntimeError"
