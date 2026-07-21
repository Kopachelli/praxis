---
status: proposed
date: 2026-07-21
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-014
---
# ADR-018 — Preserve the superseded plan as bounded correction context

## Context and Problem Statement

Accepted ADR-014 makes reject and edit correction cycles: Praxis records the operator decision, clears the actionable plan, returns the incident to `TRIAGED`, and asks Qwen for a replacement. The current store correctly clears the plan before scheduling, but the immutable Approval passed to regeneration contains only the correction note or edit instructions. Qwen therefore cannot see the exact validated proposal that the operator rejected or edited.

The missing context can make a correction ambiguous, especially an edit that refers to a step number. Restoring context must not make the old plan actionable, carry an old approval forward, or trust operator text as instructions. How should Praxis retain precisely one superseded proposal for correction?

## Decision Drivers

* FR-7 requires the correction to produce a materially revised plan.
* The old plan must stop being actionable atomically with the reject/edit decision.
* The snapshot must be server-owned, immutable, and already validated against the tool registry.
* Operator notes and edits remain untrusted data.
* Regeneration must remain bounded and must not accumulate an unbounded plan history.
* Every replacement must pass the normal plan/tool/risk validation and receive a new approval.

## Considered Options

* Continue sending only the correction and discard the old plan.
* Keep the old plan active in the incident until regeneration succeeds.
* Copy the current validated plan into the immutable correction record, then clear the active plan.
* Add a separate persistent plan-revision history and public revision API.

## Decision Outcome

Proposed: **copy exactly one bounded, validated superseded-plan snapshot into the server-created correction record, then clear the active plan as today.**

After explicit owner acceptance:

1. Reject/edit atomically deep-copy the current `RemediationPlan` into the immutable Approval/correction record before clearing the active plan and entering `TRIAGED`.
2. The regeneration prompt labels the snapshot `previously_validated_but_not_authorized` and labels the note/edits `untrusted_operator_input`. Neither block is a system instruction.
3. The snapshot uses the accepted plan bounds and includes only `seq`, `action`, `tool`, validated `args`, `risk_level`, and `rollback`. It never includes provider output, raw alert payloads, secrets, prior reasoning, or older revisions.
4. The superseded snapshot is not returned as the current incident plan and cannot be passed to the executor. Only a newly validated plan stored after regeneration can return the incident to `AWAITING_APPROVAL`.
5. No Approval is transferable: the regenerated plan requires a fresh explicit `approve` decision before execution.
6. If no current validated plan exists, reject/edit fails closed instead of constructing correction context.

No data model, prompt, state-machine, or endpoint implementation may change while this ADR is `proposed`.

### Consequences

* Good: step-specific corrections have the exact proposal they refer to.
* Good: the old plan remains non-actionable and cannot inherit approval.
* Good: one bounded snapshot avoids an unbounded revision-history feature.
* Bad: immutable Approval records become larger and couple correction records to the plan schema.
* Bad: tests must prove deep-copy isolation and reject forged/oversized snapshots.
* Neutral: a future visible plan-revision history remains out of scope and requires another ADR.
