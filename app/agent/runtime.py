"""Process-wide lifecycle admission and background execution [NFR-2, ADR-024]."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.incidents import Approval, IncidentStore


LIFECYCLE_MAX_RUNNING_JOBS = 1
LIFECYCLE_MAX_PENDING_JOBS = 3
LIFECYCLE_PENDING_TIMEOUT_SECONDS = 300.0
LIFECYCLE_JOB_TIMEOUT_SECONDS = 240.0
LIFECYCLE_CAPACITY_ERROR_DETAIL = "Lifecycle capacity unavailable"

# ADR-028 is accepted and implemented: a real external dispatch records durable
# intent (with the pre-action boot-id baseline) before crossing its boundary,
# verifies via read-after-write boot-id, and fails closed to
# RECONCILIATION_REQUIRED on an uncertain outcome — never auto-retrying an
# uncertain write. Real dispatch is therefore reconciliation-ready.
REAL_DISPATCH_TIMEOUT_RECONCILIATION_READY = True


class LifecycleJobKind(str, Enum):
    """The complete logical jobs admitted by ADR-024's single controller."""

    INITIAL_TRIAGE = "initial_triage"
    CORRECTION_REGENERATION = "correction_regeneration"
    APPROVED_EXECUTION = "approved_execution"


class LifecycleAdmissionStatus(str, Enum):
    ADMITTED = "admitted"
    COALESCED = "coalesced"
    FULL = "full"


class IncidentAgent(Protocol):
    """Narrow interface implemented by the Qwen triage orchestrator."""

    async def run(self, incident_id: str, trace_id: str) -> None: ...

    async def regenerate(
        self,
        incident_id: str,
        trace_id: str,
        correction: Approval,
    ) -> None: ...


JobOperation = Callable[["LifecycleJobContext"], Awaitable[None]]


@dataclass(slots=True)
class _Reservation:
    token: str
    coalesce_key: str
    job_kind: LifecycleJobKind
    trace_id: str
    acquired_at: float


@dataclass(slots=True)
class _LifecycleJob:
    token: str
    incident_id: str
    coalesce_key: str
    job_kind: LifecycleJobKind
    trace_id: str
    acquired_at: float
    operation: JobOperation
    pending_expiry: asyncio.TimerHandle | None = None
    external_dispatch_started: bool = False


class LifecycleLease:
    """One pre-mutation capacity reservation owned by the shared controller."""

    __slots__ = ("_controller", "_token")

    def __init__(self, controller: "LifecycleTaskManager", token: str) -> None:
        self._controller = controller
        self._token = token

    def bind(self, incident_id: str) -> None:
        """Bind a successful store mutation to the reserved incident job."""

        self._controller.bind(self, incident_id)

    def release(self) -> None:
        """Return an unused reservation. Submitted work is unaffected."""

        self._controller.release(self)


@dataclass(frozen=True, slots=True)
class LifecycleAdmission:
    """Result of a non-blocking admission attempt."""

    status: LifecycleAdmissionStatus
    lease: LifecycleLease | None = None

    @property
    def admitted(self) -> bool:
        return self.status is LifecycleAdmissionStatus.ADMITTED


class LifecycleJobContext:
    """Allow a running execution job to mark the real-dispatch boundary."""

    __slots__ = ("_controller", "_revoked", "_token")

    def __init__(self, controller: "LifecycleTaskManager", token: str) -> None:
        self._controller = controller
        self._token = token
        self._revoked = False

    @property
    def revoked(self) -> bool:
        """Whether the job has crossed its immutable lifecycle deadline."""

        return self._revoked

    def raise_if_revoked(self) -> None:
        """Fail closed before any mutation if the deadline has already fired.

        A cancellation-suppressing operation that survives past the whole-job
        deadline must not mutate incident state or dispatch. Callers consult this
        immediately before every state change so a fenced job aborts instead of
        writing after its disposition was already recorded.
        """

        if self._revoked:
            raise RuntimeError("lifecycle job deadline has expired")

    def mark_external_dispatch(self) -> None:
        """Conservatively mark that a real external action may have started."""

        if self._revoked:
            raise RuntimeError("lifecycle job deadline has expired")
        self._controller.mark_external_dispatch(self._token)

    def _revoke(self) -> None:
        """Permanently fence this capability before cancelling its operation."""

        self._revoked = True


class LifecycleTaskManager:
    """Admit exactly one running job plus three FIFO-pending jobs process-wide."""

    def __init__(
        self,
        store: IncidentStore | None,
        logger: logging.Logger,
        *,
        max_pending_jobs: int = LIFECYCLE_MAX_PENDING_JOBS,
        pending_timeout_seconds: float = LIFECYCLE_PENDING_TIMEOUT_SECONDS,
        job_timeout_seconds: float = LIFECYCLE_JOB_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not isinstance(max_pending_jobs, int)
            or isinstance(max_pending_jobs, bool)
            or max_pending_jobs < 0
        ):
            raise ValueError("max_pending_jobs must be a non-negative integer")
        if pending_timeout_seconds <= 0:
            raise ValueError("pending_timeout_seconds must be greater than zero")
        if job_timeout_seconds <= 0:
            raise ValueError("job_timeout_seconds must be greater than zero")
        if not callable(clock):
            raise TypeError("clock must be callable")

        self._store = store
        self._logger = logger
        self._max_pending_jobs = max_pending_jobs
        self._pending_timeout_seconds = float(pending_timeout_seconds)
        self._job_timeout_seconds = float(job_timeout_seconds)
        self._clock = clock
        self._reservations: dict[str, _Reservation] = {}
        self._bound: dict[tuple[str, LifecycleJobKind, str], str] = {}
        self._active_keys: dict[str, str] = {}
        self._pending: deque[_LifecycleJob] = deque()
        self._pending_by_token: dict[str, _LifecycleJob] = {}
        self._running: _LifecycleJob | None = None
        self._worker: asyncio.Task[None] | None = None
        self._cancellation_watchdogs: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def max_running_jobs(self) -> int:
        return LIFECYCLE_MAX_RUNNING_JOBS

    @property
    def max_pending_jobs(self) -> int:
        return self._max_pending_jobs

    @property
    def pending_timeout_seconds(self) -> float:
        return self._pending_timeout_seconds

    @property
    def job_timeout_seconds(self) -> float:
        return self._job_timeout_seconds

    @property
    def running_count(self) -> int:
        return int(self._running is not None)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def outstanding_count(self) -> int:
        return (
            len(self._reservations)
            + len(self._pending)
            + int(self._running is not None)
        )

    @property
    def real_dispatch_timeout_reconciliation_ready(self) -> bool:
        return REAL_DISPATCH_TIMEOUT_RECONCILIATION_READY

    def acquire(
        self,
        coalesce_key: str,
        job_kind: LifecycleJobKind,
        trace_id: str,
    ) -> LifecycleAdmission:
        """Reserve capacity before any incident/idempotency/Approval mutation."""

        _require_nonempty("coalesce_key", coalesce_key)
        _require_nonempty("trace_id", trace_id)
        if not isinstance(job_kind, LifecycleJobKind):
            raise TypeError("job_kind must be a LifecycleJobKind")
        if self._closed:
            return LifecycleAdmission(LifecycleAdmissionStatus.FULL)
        if coalesce_key in self._active_keys:
            return LifecycleAdmission(LifecycleAdmissionStatus.COALESCED)
        capacity = LIFECYCLE_MAX_RUNNING_JOBS + self._max_pending_jobs
        if self.outstanding_count >= capacity:
            return LifecycleAdmission(LifecycleAdmissionStatus.FULL)

        token = uuid.uuid4().hex
        reservation = _Reservation(
            token=token,
            coalesce_key=coalesce_key,
            job_kind=job_kind,
            trace_id=trace_id,
            acquired_at=self._clock(),
        )
        self._reservations[token] = reservation
        self._active_keys[coalesce_key] = token
        return LifecycleAdmission(
            LifecycleAdmissionStatus.ADMITTED,
            LifecycleLease(self, token),
        )

    def bind(self, lease: LifecycleLease, incident_id: str) -> None:
        """Attach an admitted lease to the incident created/mutated under it."""

        token = self._lease_token(lease)
        _require_nonempty("incident_id", incident_id)
        reservation = self._reservations.get(token)
        if reservation is None:
            raise RuntimeError("lifecycle lease is no longer reservable")
        existing = self._active_keys.get(incident_id)
        if existing is not None and existing != token:
            raise RuntimeError("incident already has admitted lifecycle work")

        if reservation.coalesce_key != incident_id:
            if self._active_keys.get(reservation.coalesce_key) == token:
                self._active_keys.pop(reservation.coalesce_key, None)
            reservation.coalesce_key = incident_id
            self._active_keys[incident_id] = token

        bound_key = (incident_id, reservation.job_kind, reservation.trace_id)
        prior = self._bound.get(bound_key)
        if prior is not None and prior != token:
            raise RuntimeError("incident already has a bound lifecycle lease")
        self._bound[bound_key] = token

    def release(self, lease: LifecycleLease) -> None:
        """Release a reservation that was not submitted to the FIFO."""

        token = self._lease_token(lease)
        reservation = self._reservations.pop(token, None)
        if reservation is None:
            return
        self._remove_bound_token(token)
        if self._active_keys.get(reservation.coalesce_key) == token:
            self._active_keys.pop(reservation.coalesce_key, None)

    def submit(
        self,
        incident_id: str,
        job_kind: LifecycleJobKind,
        trace_id: str,
        operation: JobOperation,
    ) -> bool:
        """Consume a bound lease, or admit direct callers, into the FIFO."""

        _require_nonempty("incident_id", incident_id)
        _require_nonempty("trace_id", trace_id)
        if not isinstance(job_kind, LifecycleJobKind):
            raise TypeError("job_kind must be a LifecycleJobKind")
        if not callable(operation):
            raise TypeError("operation must be callable")
        if self._closed:
            return False

        bound_key = (incident_id, job_kind, trace_id)
        token = self._bound.pop(bound_key, None)
        if token is None:
            admission = self.acquire(incident_id, job_kind, trace_id)
            if not admission.admitted or admission.lease is None:
                return False
            admission.lease.bind(incident_id)
            token = self._bound.pop(bound_key, None)
        if token is None:
            return False

        reservation = self._reservations.pop(token, None)
        if reservation is None:
            return False
        job = _LifecycleJob(
            token=token,
            incident_id=incident_id,
            coalesce_key=reservation.coalesce_key,
            job_kind=job_kind,
            trace_id=trace_id,
            acquired_at=reservation.acquired_at,
            operation=operation,
        )
        self._pending.append(job)
        self._pending_by_token[token] = job

        loop = asyncio.get_running_loop()
        expires_at = reservation.acquired_at + self._pending_timeout_seconds
        job.pending_expiry = loop.call_at(
            expires_at,
            self._expire_pending,
            token,
        )
        self._ensure_worker()
        return True

    def is_active(self, incident_id: str) -> bool:
        return incident_id in self._active_keys

    def mark_external_dispatch(self, token: str) -> None:
        """Mark the irreversible boundary before the real adapter is invoked."""

        running = self._running
        if running is None or running.token != token:
            raise RuntimeError("external dispatch marker has no running lifecycle job")
        if running.job_kind is not LifecycleJobKind.APPROVED_EXECUTION:
            raise RuntimeError("only approved execution can mark external dispatch")
        running.external_dispatch_started = True

    async def shutdown(self) -> None:
        """Cancel and collect the one worker without treating shutdown as expiry."""

        if self._closed and self._worker is None:
            return
        self._closed = True
        for job in tuple(self._pending):
            if job.pending_expiry is not None:
                job.pending_expiry.cancel()
        self._pending.clear()
        self._pending_by_token.clear()
        self._reservations.clear()
        self._bound.clear()

        worker = self._worker
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        # Cancelling the worker can spawn one final watchdog for the operation it
        # was running, so collect watchdogs only after the worker has settled.
        watchdogs = tuple(self._cancellation_watchdogs)
        for watchdog in watchdogs:
            watchdog.cancel()
        if watchdogs:
            await asyncio.gather(*watchdogs, return_exceptions=True)
        self._cancellation_watchdogs.clear()
        self._worker = None
        self._running = None
        self._active_keys.clear()

    def _ensure_worker(self) -> None:
        if self._closed:
            return
        if self._worker is not None and not self._worker.done():
            return
        self._worker = asyncio.create_task(
            self._run_fifo(),
            name="praxis-lifecycle-worker",
        )

    async def _run_fifo(self) -> None:
        try:
            while self._pending and not self._closed:
                job = self._pending.popleft()
                self._pending_by_token.pop(job.token, None)
                if job.pending_expiry is not None:
                    job.pending_expiry.cancel()
                    job.pending_expiry = None

                if (
                    self._clock() - job.acquired_at
                    >= self._pending_timeout_seconds
                ):
                    self._release_job_key(job)
                    self._record_expiry(job, phase="pending")
                    continue

                self._running = job
                context = LifecycleJobContext(self, job.token)
                operation_task = asyncio.create_task(
                    job.operation(context),
                    name=f"praxis-lifecycle-operation-{job.token}",
                )
                try:
                    done, _ = await asyncio.wait(
                        {operation_task},
                        timeout=self._job_timeout_seconds,
                    )
                    if operation_task not in done:
                        # Revoke before cancellation. A coroutine that swallows
                        # CancelledError must never regain the dispatch capability,
                        # and the accepted timeout disposition must be durable at
                        # the deadline rather than after that coroutine cooperates.
                        context._revoke()
                        operation_task.cancel()
                        self._record_expiry(job, phase="running")
                        self._start_cancellation_watchdog(operation_task, job)
                        continue
                    await operation_task
                except asyncio.CancelledError:
                    # Manager shutdown cancels the scheduler. Fence and collect
                    # the independently-owned operation without mistaking it for
                    # a lifecycle deadline.
                    context._revoke()
                    if not operation_task.done():
                        operation_task.cancel()
                        self._start_cancellation_watchdog(operation_task, job)
                    else:
                        self._consume_operation_result(operation_task, job)
                    raise
                except Exception as exc:
                    # AgentTaskManager normally contains operation failures. This
                    # guard keeps arbitrary callback text out of boundary logs.
                    self._logger.error(
                        "lifecycle_job_failed",
                        extra={
                            "incident_id": job.incident_id,
                            "trace_id": job.trace_id,
                            "job_kind": job.job_kind.value,
                            "error_type": type(exc).__name__,
                        },
                    )
                finally:
                    self._running = None
                    self._release_job_key(job)
        finally:
            self._worker = None
            if self._pending and not self._closed:
                self._ensure_worker()

    def _start_cancellation_watchdog(
        self,
        operation_task: asyncio.Task[None],
        job: _LifecycleJob,
    ) -> None:
        """Repeatedly cancel a detached expired operation until it terminates.

        Asyncio cancellation is cooperative: an adversarial or buggy operation
        may catch the first ``CancelledError``. The FIFO must nevertheless move
        on immediately, so cleanup is detached and keeps injecting cancellation
        at every subsequent suspension point.
        """

        watchdog = asyncio.create_task(
            self._cancel_until_done(operation_task, job),
            name=f"praxis-lifecycle-cancellation-{job.token}",
        )
        self._cancellation_watchdogs.add(watchdog)
        watchdog.add_done_callback(self._cancellation_watchdogs.discard)

    async def _cancel_until_done(
        self,
        operation_task: asyncio.Task[None],
        job: _LifecycleJob,
    ) -> None:
        resisted_once = False
        try:
            while not operation_task.done():
                operation_task.cancel()
                await asyncio.sleep(0)
                if not operation_task.done() and not resisted_once:
                    resisted_once = True
                    self._logger.warning(
                        "lifecycle_job_resisted_cancellation",
                        extra={
                            "incident_id": job.incident_id,
                            "trace_id": job.trace_id,
                            "job_kind": job.job_kind.value,
                        },
                    )
        finally:
            if not operation_task.done():
                operation_task.cancel()
            if operation_task.done():
                self._consume_operation_result(operation_task, job)

    def _consume_operation_result(
        self,
        operation_task: asyncio.Task[None],
        job: _LifecycleJob,
    ) -> None:
        """Retrieve a detached task result without leaking exception content."""

        try:
            operation_task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._logger.error(
                "lifecycle_cancelled_job_failed",
                extra={
                    "incident_id": job.incident_id,
                    "trace_id": job.trace_id,
                    "job_kind": job.job_kind.value,
                    "error_type": type(exc).__name__,
                },
            )

    def _expire_pending(self, token: str) -> None:
        job = self._pending_by_token.pop(token, None)
        if job is None:
            return
        try:
            self._pending.remove(job)
        except ValueError:
            return
        job.pending_expiry = None
        self._release_job_key(job)
        self._record_expiry(job, phase="pending")

    def _record_expiry(self, job: _LifecycleJob, *, phase: str) -> None:
        if self._store is not None:
            try:
                self._store.record_lifecycle_timeout(
                    job.incident_id,
                    job_kind=job.job_kind.value,
                    phase=phase,
                    trace_id=job.trace_id,
                    external_dispatch_started=job.external_dispatch_started,
                )
            except Exception as exc:
                self._logger.error(
                    "lifecycle_timeout_disposition_failed",
                    extra={
                        "incident_id": job.incident_id,
                        "trace_id": job.trace_id,
                        "job_kind": job.job_kind.value,
                        "error_type": type(exc).__name__,
                    },
                )
        self._logger.warning(
            "lifecycle_job_timed_out",
            extra={
                "incident_id": job.incident_id,
                "trace_id": job.trace_id,
                "job_kind": job.job_kind.value,
                "timeout_phase": phase,
                "external_dispatch_started": job.external_dispatch_started,
            },
        )

    def _release_job_key(self, job: _LifecycleJob) -> None:
        if self._active_keys.get(job.coalesce_key) == job.token:
            self._active_keys.pop(job.coalesce_key, None)

    def _remove_bound_token(self, token: str) -> None:
        for key, value in tuple(self._bound.items()):
            if value == token:
                self._bound.pop(key, None)

    def _lease_token(self, lease: LifecycleLease) -> str:
        if not isinstance(lease, LifecycleLease) or lease._controller is not self:
            raise TypeError("lease does not belong to this lifecycle manager")
        return lease._token


class AgentTaskManager:
    """Route triage/execution operations through one lifecycle controller."""

    def __init__(
        self,
        agent: IncidentAgent,
        logger: logging.Logger,
        *,
        lifecycle: LifecycleTaskManager | None = None,
        job_kind: LifecycleJobKind = LifecycleJobKind.INITIAL_TRIAGE,
    ) -> None:
        if not isinstance(job_kind, LifecycleJobKind):
            raise TypeError("job_kind must be a LifecycleJobKind")
        self._agent = agent
        self._logger = logger
        self._lifecycle = lifecycle or LifecycleTaskManager(None, logger)
        self._job_kind = job_kind

    @property
    def lifecycle(self) -> LifecycleTaskManager:
        return self._lifecycle

    def bind_lifecycle(
        self,
        lifecycle: LifecycleTaskManager,
        *,
        job_kind: LifecycleJobKind,
    ) -> None:
        """Bind an application-owned scheduler to the process-wide controller."""

        if not isinstance(lifecycle, LifecycleTaskManager):
            raise TypeError("lifecycle must be a LifecycleTaskManager")
        if self._lifecycle is not lifecycle and self._lifecycle.outstanding_count:
            raise RuntimeError("cannot rebind an active task manager")
        if not isinstance(job_kind, LifecycleJobKind):
            raise TypeError("job_kind must be a LifecycleJobKind")
        self._lifecycle = lifecycle
        self._job_kind = job_kind

    def schedule(self, incident_id: str, trace_id: str) -> bool:
        """Admit an agent run without awaiting it; coalesce the incident."""

        return self._lifecycle.submit(
            incident_id,
            self._job_kind,
            trace_id,
            lambda context: self._run_safely(incident_id, trace_id, context),
        )

    def schedule_regeneration(
        self,
        incident_id: str,
        trace_id: str,
        correction: Approval,
    ) -> bool:
        """Admit one corrected-plan regeneration for an otherwise idle incident."""

        if not isinstance(correction, Approval):
            raise TypeError("correction must be an Approval")
        correction_snapshot = correction.model_copy(deep=True)
        return self._lifecycle.submit(
            incident_id,
            LifecycleJobKind.CORRECTION_REGENERATION,
            trace_id,
            lambda _context: self._run_regeneration_safely(
                incident_id,
                trace_id,
                correction_snapshot,
            ),
        )

    def is_active(self, incident_id: str) -> bool:
        return self._lifecycle.is_active(incident_id)

    async def shutdown(self) -> None:
        await self._lifecycle.shutdown()

    async def _run_safely(
        self,
        incident_id: str,
        trace_id: str,
        context: LifecycleJobContext,
    ) -> None:
        self._logger.info(
            "agent_run_started",
            extra={"incident_id": incident_id, "trace_id": trace_id},
        )
        try:
            lifecycle_run = getattr(self._agent, "run_with_lifecycle", None)
            if callable(lifecycle_run):
                await lifecycle_run(incident_id, trace_id, context)
            else:
                await self._agent.run(incident_id, trace_id)
        except asyncio.CancelledError:
            self._logger.info(
                "agent_run_cancelled",
                extra={"incident_id": incident_id, "trace_id": trace_id},
            )
            raise
        except Exception as exc:
            # Never attach arbitrary provider/tool exception text at this boundary.
            self._logger.error(
                "agent_run_failed",
                extra={
                    "incident_id": incident_id,
                    "trace_id": trace_id,
                    "error_type": type(exc).__name__,
                },
            )
        else:
            self._logger.info(
                "agent_run_finished",
                extra={"incident_id": incident_id, "trace_id": trace_id},
            )

    async def _run_regeneration_safely(
        self,
        incident_id: str,
        trace_id: str,
        correction: Approval,
    ) -> None:
        self._logger.info(
            "agent_regeneration_started",
            extra={"incident_id": incident_id, "trace_id": trace_id},
        )
        try:
            await self._agent.regenerate(incident_id, trace_id, correction)
        except asyncio.CancelledError:
            self._logger.info(
                "agent_regeneration_cancelled",
                extra={"incident_id": incident_id, "trace_id": trace_id},
            )
            raise
        except Exception as exc:
            self._logger.error(
                "agent_regeneration_failed",
                extra={
                    "incident_id": incident_id,
                    "trace_id": trace_id,
                    "error_type": type(exc).__name__,
                },
            )
        else:
            self._logger.info(
                "agent_regeneration_finished",
                extra={"incident_id": incident_id, "trace_id": trace_id},
            )


def _require_nonempty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
