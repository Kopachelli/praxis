---
status: accepted
date: 2026-07-20
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
---
# ADR-012 — Bound webhook request bodies before HMAC processing

## Context and Problem Statement

The signed M1 webhook currently reads the complete unauthenticated request body before it can verify the HMAC. The API contract does not define a maximum body size or a `413` response. A hostile caller could therefore consume unnecessary memory on the public Function Compute endpoint even though the signature later fails.

Adding a limit changes endpoint semantics, so the operating contract requires an accepted ADR before implementation. What bound should Praxis enforce without rejecting realistic alert payloads?

## Decision Drivers

* The public webhook must fail safely before buffering unbounded attacker-controlled data.
* Typical Sentry-style alerts are far smaller than a few hundred KiB.
* The limit must be configurable and testable locally and on Function Compute.
* Error bodies must retain the API-wide `trace_id` contract.
* Existing exact-byte HMAC verification must remain unchanged for accepted bodies.

## Considered Options

* Leave request bodies unbounded for the hackathon.
* Set a fixed 64 KiB limit.
* Set a configurable 256 KiB default limit.

## Decision Outcome

Accepted: **set `MAX_WEBHOOK_BODY_BYTES=262144` (256 KiB) by default.**

Praxis will reject a declared or streamed body above the limit with `413 {"detail":"Payload too large","trace_id":"..."}` before JSON parsing or incident creation. The implementation must not log the body, signature, or secret; it may log the configured limit, observed byte count, incident placeholder, and trace ID. Bodies at or below the limit continue through the exact raw-byte HMAC flow.

Khristian accepted this decision on 2026-07-20. The implementation must add the configuration variable, ASGI size gate, tests, deployment wiring, and `413` API contract together so the accepted behavior remains reproducible.

### Consequences

* Good: unauthenticated callers cannot force unbounded request buffering.
* Good: 256 KiB leaves ample space for the deterministic demo and normal alert metadata.
* Good: the limit and error response become explicit and reproducible.
* Bad: unusually large alert payloads must be trimmed or rejected.
* Guardrail: the limit cannot be disabled or set below the deterministic seed payload size in production.
