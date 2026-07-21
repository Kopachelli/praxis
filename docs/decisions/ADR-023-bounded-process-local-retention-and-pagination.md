---
status: proposed
date: 2026-07-21
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
---
# ADR-023 — Bound process-local retention and paginate incident listing

## Context and Problem Statement

Praxis intentionally keeps active incidents, approvals, plans, decision trails, memory-match snapshots, and idempotency keys in one process-local repository for the hackathon controller. The store never evicts records, correction cycles can append trail entries indefinitely, and `GET /incidents` returns the complete collection every time the UI polls. A long-lived or adversarial deployment can therefore grow both memory and response size without bound.

What retention, admission, audit, and pagination rules bound the single-instance store while preserving active work, deduplication guarantees, and the complete retained incident trail?

## Decision Drivers

* Preserve every active incident and every idempotency key inside its configured dedup window.
* Never execute when the corresponding approval/plan/trail cannot be retained.
* Keep the v1 process-local architecture; do not introduce a database through this decision.
* Bound controller memory, correction cycles, trail size, and list-response size.
* Make overload explicit and trace-correlated rather than silently evicting active state.
* Define stable pagination before changing the public list contract.

## Considered Options

* Keep all process-local state and rely on FC recycling.
* Evict oldest records regardless of state or dedup age.
* Move all incident state to a durable database.
* Use fixed process-local capacity, terminal-only eviction, bounded correction/trail growth, and cursor pagination.

## Decision Outcome

Proposed: **retain the accepted process-local store but impose fixed safe bounds and cursor pagination.**

After explicit owner acceptance:

1. The controller retains at most 200 incidents total, of which at most 50 may be non-terminal (`NEW`, `TRIAGED`, `AWAITING_APPROVAL`, or `EXECUTING`). These are fixed v1 safety constants rather than client input.
2. Before admitting a new unique webhook, the store lazily removes expired idempotency entries and evicts the oldest `RESOLVED` incident only when its dedup window has also expired. Its approval, plan, trail, and memory-match snapshots are removed atomically with it. Active incidents and terminal incidents still inside the dedup window are never evicted.
3. If capacity remains full, intake returns a fixed trace-bearing `503` capacity error, does not reserve the idempotency key, and does not schedule Qwen. A replay for an already-retained key still returns its existing incident.
4. One incident may undergo at most ten reject/edit correction cycles. A further correction returns a fixed `409` without recording a new Approval or scheduling Qwen. Approval of the current validated plan remains available.
5. A retained decision trail has a hard ceiling of 512 entries. Praxis checks capacity before any operation that could cross the ceiling; if the required attempt/result/audit entries cannot all be retained, the operation fails closed before a model call or state-changing tool. Existing entries are never truncated or overwritten.
6. `GET /incidents` becomes `GET /incidents?limit=<1..100>&cursor=<opaque>` with a default of 50. The response becomes `{incidents, next_cursor}`; order remains newest-first and the cursor is derived from the last `(created_at, id)` pair. Invalid cursors return the existing trace-bearing `422` validation shape.
7. The browser loads only the first page for its live demo sidebar; explicit pagination UI is not added unless separately approved. API clients may follow `next_cursor`.
8. `docs/API_CONTRACT.md`, `docs/ARCHITECTURE.md`, PRD scope/NFR wording, BUILD_PLAN/Linear, and tests must change in the same implementation turn. Moving state to durable storage or changing any fixed bound requires another accepted ADR.

No retention, admission, correction-limit, trail-limit, list-response, or UI behavior may change while this ADR is `proposed`.

### Consequences

* Good: controller memory and polled response size become deterministic.
* Good: active work and the complete retained audit trail are never silently evicted.
* Good: overload fails before idempotency reservation, model cost, or execution.
* Bad: a burst of active incidents can produce explicit 503 admission failures.
* Bad: pagination changes the public list response and every consumer must migrate atomically.
* Bad: a long correction conversation can hit the ten-cycle limit.
* Neutral: process restart still clears all process-local state, as in the accepted v1 architecture.
