---
status: accepted
date: 2026-07-21
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-001, ADR-010, ADR-014
---
# ADR-024 — Keep post-response work on active provisioned FC instances

## Context and Problem Statement

Praxis returns quickly from webhook and approval requests, then continues triage, correction regeneration, approved execution, post-resolution memory delivery, and the isolated target's delayed self-restart in Python coroutines. The current manifests set reserved concurrency, but they do not configure provisioned instances with idle mode disabled.

Alibaba documents that Function Compute freezes an ordinary instance after a response; background processes, threads, and coroutines then stop until another invocation arrives. Reserved concurrency limits capacity but does not make an instance continuously active. The accepted code therefore works in local tests yet cannot guarantee that its post-response work runs in FC.

Which supported FC lifecycle should Praxis use without violating the sub-500 ms webhook acknowledgement, the mandatory approval gate, or the accepted single-instance hackathon architecture?

## Decision Drivers

* Preserve the current FastAPI controller, state machine, Qwen-only runtime, and isolated target.
* Keep webhook acknowledgement below 500 ms instead of waiting for a complete Qwen run.
* Ensure accepted work continues immediately after the HTTP response, without depending on a later request to thaw the instance.
* Keep one controller instance and one disposable target instance for the single-operator demo.
* Avoid adding a second worker protocol before active incident state is durable across instances.
* Bound total process work and queue residence independently of Function Compute request concurrency.
* Make the fixed active-instance cost explicit and reversible.

## Considered Options

* Complete every Qwen/execution operation synchronously before returning its HTTP response.
* Move every logical job to a separate Function Compute asynchronous task function.
* Retain the existing lifecycle-owned coroutine manager on one provisioned controller instance with idle mode disabled, and provision the isolated target the same way.

## Decision Outcome

Chosen: **retain the v1 in-process task manager, but run the controller and isolated target on exactly one provisioned, non-idle Function Compute instance each.**

Khristian accepted this decision on 2026-07-21.

After explicit owner acceptance and cost authorization:

1. The deployment config creates one provisioned controller instance and one provisioned target instance, disables idle mode for both, keeps single-instance request concurrency at one, and prevents on-demand scale-out beyond the accepted single-instance boundary.
2. Request concurrency is not background-work concurrency. One process-wide lifecycle admission controller permits exactly one running job and at most three FIFO-pending jobs across initial triage, correction regeneration, approved execution, and its post-resolution memory attempt. Per-incident coalescing still applies. A lease is acquired before any intake/Approval/state mutation that requires a new job; a full queue returns a fixed trace-bearing `503` without reserving a new idempotency key, creating a new incident, recording an Approval, or changing state. A duplicate for an already retained key still returns its existing incident without consuming another lease. These fixed v1 bounds may change only through another accepted ADR.
3. A pending job expires after 300 wall-clock seconds. Once dequeued, the complete logical job has a 240-second application deadline that includes every model call, tool round, repository operation, and memory attempt; individual ADR-013 provider and adapter deadlines remain stricter where applicable. Every expiry appends one fixed secret-safe timeout event and has an explicit fail-closed disposition: initial triage remains `NEW`; correction regeneration remains `TRIAGED` with its immutable reject/edit Approval retained and no actionable plan; and an approved execution that expires before any external dispatch records the existing fixed execution-failure transition `EXECUTING → AWAITING_APPROVAL`. Its immutable approve record remains history but does not authorize another run, so any later execution requires a fresh Approval. No expiry adds a retry or restores a rejected plan. If a deadline crosses dispatch of a real external action, the outcome is uncertain and must follow an accepted and implemented ADR-028; this lifecycle change cannot be declared real-execution-ready without that gate.
4. The deployment verifier must distinguish reserved concurrency from active provisioned capacity and fail if either function can freeze between requests. It also verifies the fixed process admission and deadline configuration; an environment flag alone is not proof.
5. Webhook triage, correction regeneration, approved execution, and the current non-fatal post-resolution memory attempt remain lifecycle-owned jobs in the controller process under the admission and deadline rules above. If ADR-020 is later accepted and implemented, its bounded MNS enqueue replaces the direct embedding/Tablestore attempt as the final lifecycle-owned memory step; the dedicated consumer, redelivery, and dead-letter lifecycle belong to ADR-020 and are not controller jobs. ADR-024 does not authorize an MNS resource or a second application function.
6. The isolated target may return its authenticated acknowledgement and then perform the already accepted delayed process exit because its vCPU remains active after the response. The controller still declares success only after observing a different target boot ID; acknowledgement alone is never success.
7. Readiness and operations guidance must state that disabling provisioned non-idle mode, bypassing lifecycle admission, or disabling whole-job deadlines makes post-response processing unsupported and must remove the deployment from demo-ready status.
8. A cost runbook records how to enable the two active instances for rehearsals/recording and how to remove provisioned capacity when Praxis is intentionally taken offline. It may not silently leave the public app claiming readiness in the frozen configuration.
9. Moving jobs to FC asynchronous task mode remains the preferred multi-instance evolution. It requires an accepted durable incident repository such as ADR-027, a versioned job envelope, least-privilege invoke permissions, deterministic task IDs, retry semantics, and another accepted ADR before replacing this decision.

The implementation and deployment must satisfy every lifecycle, admission,
deadline, readiness, and cost guardrail above before the public build is called
recording-ready.

### Consequences

* Good: the current code and process-local repository gain the FC lifecycle they were designed to assume.
* Good: webhook latency and the explicit approval boundary remain unchanged.
* Good: process-wide admission, queue residence, and active runtime are explicitly bounded rather than inferred from request concurrency.
* Good: the target acknowledgement/boot-ID proof remains simple and camera-friendly.
* Bad: two provisioned non-idle instances incur continuous active compute charges while enabled.
* Bad: a process crash or deployment can still interrupt work; ADR-027 separately addresses durable state, not compute resumption.
* Bad: the single-instance design does not scale horizontally.
* Bad: overload or a stalled predecessor can produce an explicit `503` or fixed timeout instead of silently accepting work that cannot run.
* Guardrail: reserved concurrency by itself must never again be described as proof that post-response coroutines will run.

## References

* [Alibaba Cloud: background processes, threads, and coroutines after a response](https://www.alibabacloud.com/help/doc-detail/2527065.html)
* [Alibaba Cloud: Function Compute instance freeze lifecycle](https://www.alibabacloud.com/help/en/functioncompute/fc/instance-level-events)
* [Alibaba Cloud: provisioned instances and idle mode](https://www.alibabacloud.com/help/en/functioncompute/fc-2-0/user-guide/configure-provisioned-instances-and-auto-scaling-rules)
* [Alibaba Cloud: asynchronous task mode](https://www.alibabacloud.com/help/en/functioncompute/fc/asynchronous-task)
