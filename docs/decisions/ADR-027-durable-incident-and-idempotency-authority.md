---
status: proposed
date: 2026-07-21
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-004
---
# ADR-027 — Make Tablestore the durable incident and idempotency authority

## Context and Problem Statement

FR-2 promises that the same idempotency key inside the 600-second window returns the existing incident and does not start a second agent run. Today the idempotency map, incident aggregate, plans, approvals, and trail all live in one Python process. Function Compute replacement creates an empty store, so the same key is accepted as new and can schedule another Qwen run. Reserved or provisioned capacity reduces replacement frequency but cannot make process memory durable.

Persisting only the key is insufficient: after replacement, Praxis must still return the existing incident, preserve its approval and trail, and prevent a second state-changing execution. Which durable authority should replace the process-local repository while retaining Alibaba deployment and the existing Tablestore investment?

## Decision Drivers

* Enforce FR-2 across instance replacement and concurrent requests.
* Preserve the atomic Approval-before-EXECUTING invariant and immutable audit trail.
* Keep all durable application data on Alibaba Cloud and reuse the existing Tablestore SDK/role pattern.
* Separate operational incident state from the semantic `praxis_memory` table and vector index.
* Fail closed on uncertain writes; never trade availability for duplicate execution.
* Keep secrets and raw idempotency keys out of primary keys, logs, and public responses.

## Considered Options

* Retain process-local state and qualify FR-2 as best effort.
* Store only idempotency keys durably while leaving incident state local.
* Use Function Compute task IDs as the only deduplication authority.
* Add a managed relational database.
* Add dedicated Tablestore operational tables with conditional claims, optimistic versions, and transactionally stored incident aggregates/events.

## Decision Outcome

Proposed: **make dedicated Tablestore operational tables the authoritative IncidentRepository and idempotency registry; process memory becomes only a bounded cache/test backend.**

After explicit owner acceptance and a regional capability probe:

1. Provision separate `praxis_idempotency` and `praxis_incidents` operational tables. They do not reuse or overload `praxis_memory`, whose accepted vector schema and semantic purpose remain unchanged.
2. Store only a versioned HMAC reference, never the raw authenticated identity. `IDEMPOTENCY_DIGEST_KEY_CURRENT` is the canonical unpadded base64url encoding of exactly 32 random bytes; any padding, invalid alphabet, non-canonical re-encoding, or decoded length other than 32 fails startup. It is a dedicated production secret and may not reuse the webhook-signing, provider, target, or Alibaba credential. Its non-secret `IDEMPOTENCY_DIGEST_KEY_CURRENT_ID` must match `[A-Za-z0-9][A-Za-z0-9_-]{0,15}`. The single Tablestore string primary key is exactly `digest_ref = key_id + ":" + lowercase_hex(HMAC-SHA256(decoded_key, b"praxis.idempotency.v1\x00" + identity_bytes))`; the ID grammar excludes `:`, and the digest is exactly 64 lowercase hexadecimal bytes, so the representation is unambiguous. Production startup fails closed if the current key/ID is absent, duplicated, or malformed.
3. Rotation uses one optional `IDEMPOTENCY_DIGEST_KEY_PREVIOUS` plus its distinct previous ID, with the identical encoding and validation; both previous fields must be present together or both absent, and decoded current/previous keys and IDs must differ. New claims use only the current reference; lookup computes current and previous references and returns the one existing unexpired claim. On the single provisioned controller, rotation is an explicit maintenance deployment that installs the same two-key ring before intake resumes; mixed rings or rolling multi-instance rotation are forbidden. The previous key remains available for at least `DEDUP_WINDOW_SECONDS` plus 60 seconds of clock skew after the final old-key claim, then is removed in a later deployment. Key values, identity bytes, and HMAC digests are never logged, echoed, or placed in trail content.
4. A conditional `PutRow` with `EXPECT_NOT_EXIST` claims a new digest. An existing unexpired claim returns its incident and cannot schedule another logical job. An expired claim may be replaced only by a compare-and-swap against the version that was read; concurrent claim losers return the winner.
5. The claim row contains the immutable normalized intake snapshot, incident ID, creation/expiry timestamps, digest key ID, and processing generation. This row is sufficient to satisfy FR-1 before `202` and to repair a later operational-row write without accepting a second incident.
6. The durable incident partition stores state, normalized incident data, current plan, immutable Approval records, memory-match snapshot, and append-only trail entries. Every mutation uses an expected aggregate version; stale writers fail and re-read instead of overwriting newer state.
7. State transition plus its required Approval/trail/job-intent records commit atomically within the incident partition using Tablestore local transactions. The implementation must first prove that local transactions are enabled and supported in the exact Singapore instance. If that capability is unavailable, implementation stops and a replacement ADR is required; no weaker multi-write approximation is permitted.
8. A new least-privilege controller role policy grants only the exact operational-table reads, conditional writes, and transaction operations required. Schema creation remains in an explicit provisioning script and outside the request role, matching the existing memory boundary.
9. The public API keeps the current incident IDs and response schemas unless another accepted ADR changes them. Reads come from the durable repository; a cache miss may read Tablestore but may never be treated as proof that no prior idempotency claim exists.
10. The async/background job for an incident carries a deterministic generation identity and claims its durable job intent before Qwen or execution. An Approval ID plus aggregate-version fence prevents two concurrent workers from starting the same generation. Those database fences do **not** prove exactly-once external effects after dispatch: an incomplete real-tool operation may not be resumed, replayed, or classified from repository state alone. Any such operation remains blocked until the verification-first reconciliation semantics in ADR-028 are separately accepted and implemented.
11. Retention, pagination, correction, and trail bounds remain governed by proposed ADR-023. ADR-027 implementation must either follow an accepted ADR-023 or propose equivalent fixed bounds before storing an unbounded aggregate/event stream.
12. Provisioning, migration, rollback, key-ring rotation, concurrency, expiry-boundary, FC-replacement, partial-write-repair, duplicate-execution, and fail-closed tests must pass before production switches away from the process-local backend.

No table, IAM permission, repository backend, incident identifier, state mutation, idempotency behavior, migration, or deployment configuration may change while this ADR is `proposed`.

### Consequences

* Good: deduplication and the human approval audit survive Function Compute replacement.
* Good: conditional claims and version fences prevent concurrent Qwen runs and concurrent starts of the same approved execution generation.
* Good: the design stays on Alibaba Cloud and reuses the project's Tablestore operational expertise.
* Bad: adds two tables, transaction/capability checks, repository code, IAM policy, migration, and operational cost.
* Bad: adds a dedicated versioned deployment secret and a maintenance-gated rotation procedure.
* Bad: every state mutation now has network latency and can fail because Tablestore is unavailable.
* Bad: local-transaction availability in the exact existing instance is a hard prerequisite.
* Bad: durable repository fencing cannot eliminate the external side-effect uncertainty addressed by ADR-028.
* Guardrail: on an ambiguous timeout or conditional-write result, Praxis must read authoritative state and fail closed before Qwen or a state-changing tool.

## References

* [Alibaba Cloud: Tablestore conditional updates](https://www.alibabacloud.com/help/en/tablestore/conditional-update)
* [Alibaba Cloud: Tablestore local transactions](https://www.alibabacloud.com/help/en/tablestore/local-transactions)
* [Alibaba Cloud: Python SDK data operations](https://www.alibabacloud.com/help/en/tablestore/developer-reference/operations-on-data-in-python/)
