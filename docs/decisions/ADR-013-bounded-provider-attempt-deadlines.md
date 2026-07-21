---
status: accepted
date: 2026-07-20
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-008
---
# ADR-013 — Bound provider attempts within the Function Compute deadline

## Context and Problem Statement

ADR-008 requires Praxis to exhaust three Qwen Cloud reasoning models before entering the three-model OpenRouter fallback chain. The first M2 client used the integration guide's 60-second timeout for every provider/model attempt. Three Qwen Cloud timeouts can therefore consume at least 180 seconds before the first OpenRouter request, while the deployed Function Compute function has a 120-second hard timeout. A Qwen Cloud network outage would terminate the invocation before the required fallback can run, and the unbounded logical route is incompatible with NFR-2's triage latency target.

Changing provider-attempt timing changes runtime failover behavior, so implementation is gated on an accepted architecture decision. What deadline should bound each attempt and the complete logical call?

## Decision Drivers

* OpenRouter must remain reachable during a Qwen Cloud network timeout, not only after immediate HTTP errors.
* The complete six-attempt reasoning route must leave cleanup and response headroom inside Function Compute's 120-second limit.
* The two-attempt fast route should fit within NFR-2's 30-second triage target even when Qwen Cloud times out.
* Thinking-mode responses need more than an aggressive connect-only timeout.
* A hard wall-clock deadline is required because HTTPX phase timeouts alone do not bound a slowly progressing response.
* Accepted timeout transitions must retain the same secret-safe `fallback` trail semantics.

## Considered Options

* Keep 60 seconds per attempt and accept that network-timeout fallback cannot reach OpenRouter on Function Compute.
* Apply only a 90-second overall deadline, allowing an early Qwen Cloud attempt to consume the entire budget.
* Apply a 15-second hard wall-clock deadline per attempt plus a 90-second deadline for the complete logical call.

## Decision Outcome

Accepted: **apply a 15-second hard wall-clock deadline to every provider/model attempt and a 90-second hard deadline to each logical client call.**

The production client will enforce the attempt deadline around the complete HTTP operation, retain HTTPX phase timeouts at or below that value, and add no inter-attempt sleep. A timeout remains an ADR-008 fallback class. The reasoning route has at most six attempts and can therefore complete or exhaust within 90 seconds; the fast route has at most two attempts and can complete or exhaust within 30 seconds. The logical deadline is a defensive cap across request setup, trail writes, and transition overhead. Timeout values may be injected by tests, but production defaults and deployment documentation must remain fixed to this accepted policy unless a later ADR changes them.

Khristian accepted this decision on 2026-07-20. Implementation requires synchronized client tests, the integration and architecture documents, deployment verification, Linear, and the worklog.

### Consequences

* Good: Qwen Cloud network timeouts can reach the same-Qwen OpenRouter route before Function Compute terminates the invocation.
* Good: worst-case reasoning traffic retains roughly 30 seconds of Function Compute headroom.
* Good: fast-role failover remains bounded by 30 seconds.
* Bad: a healthy provider/model that needs more than 15 seconds for one response will be abandoned and may consume duplicate provider work.
* Bad: the 90-second failure ceiling is higher than NFR-2's normal-path p95 target; tests and live measurements must still demonstrate the p95 target separately.
* Guardrail: changing either deadline, adding attempts, or adding an inter-attempt delay requires a proposed ADR before implementation.
