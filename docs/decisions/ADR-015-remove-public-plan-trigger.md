---
status: accepted
date: 2026-07-20
decision-makers: Khristian Kopachelli
---
# ADR-015 — Remove the public manual plan trigger from v1

## Context and Problem Statement

The PRD and API contract listed `POST /incidents/{id}/plan`, but the accepted build plan and application never defined or implemented its request body, operator authorization, eligible states, rate limit, or cost controls. Praxis already generates the initial plan internally after a signed webhook and regenerates correction plans through the HITL `reject`/`edit` path required by FR-7 and ADR-014. Exposing an anonymous manual Qwen trigger now would add undefined state semantics and a cost-amplification surface without helping the demo.

## Considered Options

* Define and implement the public/manual endpoint before submission.
* Keep the stale contract entry while leaving the endpoint unimplemented.
* Remove the public/manual endpoint from v1 while retaining all internally orchestrated planning and correction regeneration.

## Decision Outcome

Chosen: **remove `POST /incidents/{id}/plan` from the public v1 contract**. Initial Qwen planning remains an internal background task started only by accepted alert intake. Rejected or edited plans continue to regenerate only through `POST /incidents/{id}/approve` under ADR-014. A future authenticated manual-replan API requires a new ADR defining authorization, idempotency, state transitions, request bounds, and cost controls.

Khristian accepted this decision on 2026-07-20.

### Consequences

* Good: the public contract matches the deployed code and the camera flow.
* Good: no anonymous endpoint can trigger extra model work outside signed intake or the explicit HITL correction cycle.
* Good: FR-3 and FR-7 remain fully implemented through internal orchestration.
* Bad: operators cannot manually request a fresh plan through a standalone public endpoint in v1.
