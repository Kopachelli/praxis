---
status: proposed
date: 2026-07-21
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-015
---
# ADR-022 — Recover failed agent work with one bounded automatic retry

## Context and Problem Statement

Initial triage and correction regeneration run in lifecycle-owned background tasks. When either task raises, Praxis records a fixed failure event and logs the failure, then removes the task from the manager. Initial work can remain `NEW` or `TRIAGED`; correction work remains `TRIAGED`. Duplicate webhook intake intentionally does not schedule a second agent run, and accepted ADR-015 removed the undefined public/manual plan trigger. The UI can therefore tell an operator that the failure is inspectable, but no reachable operation can retry it.

What recovery path restores transient agent failures without adding an anonymous model-cost endpoint, duplicating execution, or weakening the mandatory approval gate?

## Decision Drivers

* Preserve ADR-015's removal of the public manual-plan trigger.
* Bound extra Qwen cost and total background work deterministically.
* Keep one lifecycle-owned task per incident and one immutable correction payload per regeneration.
* Retry only before an incident can enter `AWAITING_APPROVAL`; never retry execution.
* Ensure a recovered plan still requires a fresh human decision.
* Record fixed, trace-correlated retry evidence without provider exception text.

## Considered Options

* Leave failed incidents inspectable but permanently stuck.
* Add an authenticated operator retry endpoint and define a new authorization model.
* Add a new terminal failure state plus a manual recovery transition.
* Retry eligible initial/regeneration work once inside the existing task manager.

## Decision Outcome

Proposed: **perform at most one automatic retry inside the existing lifecycle-owned task, with no new public endpoint.**

After explicit owner acceptance:

1. Each scheduled initial-triage or correction-regeneration operation may make at most two complete agent attempts: the original attempt plus one retry after a fixed one-second delay. No recursive scheduling or additional retry layer is allowed.
2. Initial triage keeps the incident in `NEW` while classification, memory recall, and plan generation are in progress. It transitions `NEW → TRIAGED` only immediately before storing a validated plan through the existing atomic plan transition. An eligible pre-plan failure therefore remains safely retryable as the same initial operation.
3. Correction regeneration retains the immutable `Approval` object already passed to the scheduled task. An eligible failure remains `TRIAGED`; the retry calls the same regeneration path with the same server-owned correction, never a reconstructed client payload.
4. Retry eligibility is limited to secret-safe transient/provider exhaustion reasons (`timeout`, `logical_timeout`, HTTP 429, and HTTP 5xx) plus exhaustion of the existing bounded plan-validation reprompts. Authentication/payment, unavailable-model, invalid-transition, configuration, policy, and programmer errors terminate without retry.
5. Before the retry delay, Praxis appends one fixed `THOUGHT` trail event containing `stage`, `status=retry_scheduled`, `attempt=2`, `max_attempts=2`, `delay_seconds=1`, and `trace_id`. It never stores an exception message, response body, or credential-bearing URL.
6. The task manager continues to deduplicate by incident ID. Duplicate webhook delivery returns the original incident and cannot create another task. Once a validated plan reaches `AWAITING_APPROVAL`, no automatic agent retry remains scheduled.
7. If the retry also fails, Praxis appends a fixed `retry_exhausted` event and leaves the incident in `NEW` or `TRIAGED` for audit. It does not invent approval, execute a tool, or expose a public retry action.
8. Process recycle can still interrupt this in-memory retry; durable cross-restart work delivery is outside this decision. Adding an authenticated manual retry, durable queue, different attempt count/backoff, or new failure state requires another accepted ADR.

No state transition, task retry, endpoint, or Qwen call behavior may change while this ADR is `proposed`.

### Consequences

* Good: common transient failures get one reachable recovery path without human timing.
* Good: worst-case additional model cost is exactly one complete logical agent attempt.
* Good: ADR-015 and the HITL boundary remain intact.
* Bad: persistent/provider-configuration failures still leave an incident stuck for audit.
* Bad: keeping initial work in `NEW` until a validated plan changes when `TRIAGED` becomes externally visible.
* Neutral: durable retry across an FC recycle remains future work.
