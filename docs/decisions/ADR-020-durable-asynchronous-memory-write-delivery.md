---
status: proposed
date: 2026-07-21
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-004
---
# ADR-020 — Deliver memory writes through a durable asynchronous queue

## Context and Problem Statement

Praxis deliberately does not roll back a successful remediation when post-resolution memory persistence fails. Today the controller logs and trails the failure, but the incident store and retry opportunity are process-local. A Function Compute recycle or transient Qwen Cloud/Tablestore outage can therefore lose that IncidentMemory permanently.

In-process retries reduce transient failures but are not durable. Using Tablestore itself as an outbox cannot cover a Tablestore outage. Blocking state-changing remediation until optional memory is available would couple the safety action to an unrelated availability dependency. How should Praxis retry without weakening the execution outcome or storing secrets in a message?

Proposed ADR-024 separately owns the controller's supported post-response lifecycle and currently includes the direct best-effort embedding/Tablestore attempt in that job. If both decisions are accepted, they must compose as one delivery path rather than running both the direct write and the queue consumer.

## Decision Drivers

* A successful remediation remains successful even if memory is delayed.
* Delivery must survive controller recycle and transient downstream outages.
* The request must be idempotent and contain only bounded, secret-safe evidence.
* Qwen-family models remain the only model runtime; embeddings remain Qwen Cloud only.
* Failed delivery must become observable and recoverable instead of silently disappearing.
* A new Alibaba service/function and permission boundary require explicit owner approval.

## Considered Options

* Keep best-effort single-attempt writes.
* Add bounded in-process retries only.
* Store a pending record in the same Tablestore instance.
* Publish a secret-safe request to Alibaba Cloud MNS and process it in a dedicated FC memory-writer function with a dead-letter path.
* Block incident resolution until the memory row is stored.

## Decision Outcome

Proposed: **use Alibaba Cloud MNS as the durable delivery boundary and a dedicated Function Compute memory-writer consumer.**

After explicit owner acceptance and service-cost authorization:

1. The controller constructs a versioned `MemoryWriteRequest` only from the verified execution outcome defined by ADR-019. The message contains no API keys, raw payload, reasoning trace, or unbounded tool output.
2. The controller publishes the request to an MNS queue after the incident reaches `RESOLVED`. Under ADR-024, that bounded enqueue is the final lifecycle-owned memory step and remains inside the complete-job deadline. A successful publish records `memory_write: queued`; it does not claim `stored`.
3. A dedicated FC consumer receives the queue message, calls Qwen Cloud `text-embedding-v4`, and performs an idempotent Tablestore put using deterministic ID `mem_<incident_id>`.
4. MNS redelivery plus deterministic identity provides at-least-once delivery without duplicate memories. The consumer records delivery attempts using secret-safe fixed reason labels.
5. A bounded retry/redrive policy moves exhausted messages to a dead-letter queue. Operations documentation defines inspect and replay commands that never expose message bodies by default.
6. The consumer receives only the minimum Qwen/Tablestore/MNS permissions; the remediation target receives none. OpenRouter is not used for embeddings.
7. If publish itself fails, the controller records `memory_write: enqueue_unavailable`; the incident remains resolved. A separate reconciliation command can reconstruct a request only from retained verified evidence—never from a plan.
8. When both ADR-020 and ADR-024 are accepted and implemented, this decision supersedes only ADR-024's direct in-process embedding/Tablestore memory attempt. The dedicated MNS consumer, redelivery, and dead-letter lifecycle are not controller coroutines and do not consume the controller's lifecycle-admission slot; ADR-024 continues to own triage, correction, execution, and the bounded enqueue.

No MNS resource, dependency, function, permission, deployment manifest, or runtime code may change while this ADR is `proposed`.

### Consequences

* Good: transient embedding/Tablestore failures can recover after controller recycle.
* Good: remediation success remains independent from memory availability.
* Good: delivery state becomes explicit: queued, stored, or dead-lettered.
* Bad: adds MNS, a second application function, IAM policy, deployment steps, monitoring, and cost.
* Bad: at-least-once delivery requires strict idempotency and version compatibility.
* Bad: enqueue failure still needs an operator reconciliation path because no system can durably queue through an unavailable queue.
* Guardrail: no queue message may contain credentials, raw alert payloads, provider responses, or unvalidated tool output.
