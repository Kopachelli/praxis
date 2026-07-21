---
status: accepted
date: 2026-07-21
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-006, ADR-010
---
# ADR-028 — Fail closed when a real tool outcome cannot be audited

## Context and Problem Statement

Praxis records an execution attempt, calls the approved adapter, and then records its result. These steps cannot be one atomic transaction with an external system. A real adapter can change the target successfully and then the result-trail write can fail. The chronological executor now blocks every later step, but the current generic failure path returns the incident to `AWAITING_APPROVAL`; another approval can repeat an action whose prior outcome was not durably audited.

The isolated restart adapter returns a read-after-write boot-ID proof, but that proof is useful only if Praxis can associate it with a durable pre-action intent and never treats a transport/audit failure as proof of either success or failure. How should Praxis recover without inventing an outcome or replaying an uncertain state change?

## Decision Drivers

* Never automatically repeat a state-changing action with an uncertain outcome.
* Persist intent before effect and make every real execution attempt uniquely identifiable.
* Use read-only verification instead of repeating the write whenever verification is possible.
* Keep all remaining plan steps blocked until the uncertain step is reconciled.
* Preserve immutable Approval and execution evidence; never rewrite a failed history entry.
* Keep the v1 real-action scope limited to the isolated Function Compute restart.

## Considered Options

* Return to ordinary approval and allow the complete plan to run again.
* Treat the adapter's in-memory success result as resolved even when it cannot be persisted.
* Automatically retry every tool with the same arguments.
* Require idempotency-aware intent plus read-only verification, and enter an explicit manual-reconciliation state when durable outcome recording fails.

## Decision Outcome

Chosen: **introduce a fail-closed `RECONCILIATION_REQUIRED` state and never replay a real write until its prior operation is durably classified.**

Khristian accepted this decision on 2026-07-21.

After explicit owner acceptance:

1. Before a safe real adapter can run, Praxis durably appends an immutable execution intent containing a server-generated `operation_id`, incident ID, Approval ID, plan generation/hash, step sequence, exact allowlisted target, trace ID, and the adapter's bounded verification baseline. This write must succeed before the external action begins.
2. The v1 restart adapter's baseline is the exact pre-action target boot ID. The adapter performs the authenticated restart once, then uses only read-only health probes to determine whether the boot ID changed. A transport disconnect or acknowledgement without a changed boot ID is not success.
3. The adapter result and trail entry carry the same `operation_id` and one typed outcome: `VERIFIED_SUCCEEDED`, `VERIFIED_NOT_APPLIED`, or `UNKNOWN`. Persisting the result is idempotent: retrying the result write may not create a second entry, but Praxis never retries the external restart as part of that persistence retry.
4. If the result cannot be durably stored, the runner stops before the next plan step and attempts to persist `RECONCILIATION_REQUIRED`. No approval control or execution scheduler is available in that state. If even that state write is ambiguous, a recovered `EXECUTING` incident with an incomplete operation intent is treated as reconciliation-required, never as retryable work.
5. Reconciliation uses the adapter's read-only verifier only. For the isolated target, a current boot ID different from the durable baseline is `VERIFIED_SUCCEEDED`. An explicit rejection proven before dispatch can be `VERIFIED_NOT_APPLIED`. An unchanged boot ID at the verification deadline, a timeout, a 5xx, a disconnect, or an unavailable target remains `UNKNOWN`; none proves that the restart did not occur.
6. No public/manual reconciliation endpoint is added by this decision alone. After ADR-025 and ADR-027 are accepted and implemented, a separately reviewed authenticated workflow may record one immutable operator reconciliation plus the allowlisted server-side verifier evidence. Client-supplied free-form evidence cannot override the verifier.
7. A later reconciliation of `VERIFIED_SUCCEEDED` records the missing result and requires a fresh human review before any remaining plan steps continue. `VERIFIED_NOT_APPLIED` may return to plan correction/reapproval without automatically running the step. `UNKNOWN` leaves all execution blocked.
8. Caution and dangerous adapters remain labeled dry runs and cannot mutate an external system. Adding another real adapter requires it to define an operation identity, pre-action intent, bounded read-only verifier, and reconciliation semantics in a new accepted ADR.
9. API/state-machine/UI/demo documentation must distinguish a failed action from an uncertain action. No path may label an uncertain outcome `succeeded`, `failed`, or `resolved` merely to unblock the incident.

Accepted 2026-07-21: implementation may now proceed. The v1 execution intent and result are held in the process-local incident store on the single provisioned non-idle instance (ADR-024); cross-process durability that survives instance replacement remains ADR-027's responsibility, and a public/manual reconciliation endpoint remains out of scope for this decision (clause 6).

### Consequences

* Good: an audit failure cannot silently turn into a duplicate real action.
* Good: the existing boot-ID proof becomes a durable recovery primitive rather than camera-only evidence.
* Good: every remaining action stays behind an explicit human decision after reconciliation.
* Bad: adds a state, operation-intent model, verifier contract, operator workflow, and API/UI surface.
* Bad: some incidents can remain blocked indefinitely when the external state cannot be verified.
* Bad: result persistence still needs durable storage to survive process replacement; ADR-027 is the long-term authority.
* Guardrail: only evidence obtained by the registered read-only verifier may classify an uncertain real-tool outcome.
