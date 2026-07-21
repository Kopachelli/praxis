---
status: accepted
date: 2026-07-20
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
---
# ADR-014 — Treat reject and edit as plan-correction cycles

## Context and Problem Statement

PRD FR-7 and the M3 BUILD_PLAN task require Praxis to feed either a rejected or edited plan back to Qwen and regenerate it. The lower-precedence API contract and architecture state table instead make `reject` terminal by transitioning to `REJECTED`; the current M1 state-machine foundation mirrors that terminal behavior. The approval request body also omits an operator field although the PRD requires every Approval record to contain an operator identity.

This contradiction changes endpoint semantics, state transitions, and the Approval/plan lifecycle, so it must be resolved before the M3 approval endpoint is implemented. What should `reject` mean, and how should the single-operator demo attribute the decision?

## Decision Drivers

* PRD FR-7 is the highest-precedence explicit requirement for reject/edit behavior.
* No rejection or edit may execute a state-changing tool.
* Every decision must create an immutable Approval/trail record before its state transition.
* The rejected proposal must not remain displayed as though it were still actionable.
* Correction input must be concrete enough for Qwen to regenerate a different plan.
* The hackathon build is explicitly single-operator and has no authentication system in scope.
* Regeneration must remain asynchronous so the approval response is bounded.

## Considered Options

* Keep `reject` terminal and implement regeneration for `edit` only.
* Treat both `reject` and `edit` as corrections that return to `TRIAGED` and regenerate.
* Add a fourth `close` decision so `reject` regenerates while `close` becomes terminal.

## Decision Outcome

Accepted: **treat both `reject` and `edit` as plan-correction cycles, exactly as required by FR-7.**

* `approve` is the only decision that records approval and enters `EXECUTING`.
* `reject` requires a non-empty `note`; `edit` requires at least one strict `{seq, instruction}` edit. Both record an Approval and correction payload, atomically clear the superseded plan, and transition `AWAITING_APPROVAL → TRIAGED`.
* The API schedules Qwen regeneration in the existing lifecycle-owned background task manager and returns the TRIAGED incident immediately. The correction is included as untrusted planning context. A validated replacement plan returns the incident to `AWAITING_APPROVAL`.
* A regeneration failure leaves the incident in `TRIAGED` with a trace-correlated, secret-safe failure entry; it never enters `EXECUTING`.
* The terminal `REJECTED` path is removed from the active M3 state machine. Adding a separate terminal close/cancel action is out of hackathon scope unless a later accepted ADR adds it.
* Because PRD NG3 fixes single-operator demo scope and the published request schema has no authenticated identity, Approval records use the server-owned constant `demo-operator`. Client input cannot spoof or override it. A future authenticated operator identity requires a later ADR.

Khristian accepted this decision on 2026-07-20.

### Consequences

* Good: implementation follows binding FR-7 and the already-published M3 checklist.
* Good: every non-approved proposal visibly cycles through Qwen instead of becoming a dead-end incident.
* Good: only `approve` can cross the mandatory HITL execution boundary.
* Good: server-owned attribution avoids inventing authentication or trusting spoofable operator input.
* Bad: there is no terminal operator-cancel action in the hackathon API.
* Bad: reject/edit incur another Qwen planning call and can add demo latency.
* Guardrail: changing decision meanings, accepting client-supplied operator identity, or adding a terminal close action requires a proposed ADR before implementation.
