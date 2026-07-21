---
status: proposed
date: 2026-07-21
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-004
---
# ADR-019 — Ground incident memory in verified execution outcomes

## Context and Problem Statement

The approved executor already produces a validated, ordered `ExecutionReport` whose step results distinguish success from failure and real action from dry-run. After a successful report, the runtime currently transitions the incident to `RESOLVED` and asks the memory service to construct an `IncidentMemory` from the remediation plan. That constructor describes planned steps as “completed” and does not receive the execution report.

As a result, persistent memory can overstate what happened: a caution/dangerous dry-run can read like a completed remediation, and planned arguments can be remembered without the executor's actual outcome. Recalled memory is later injected into Qwen as operational evidence, so this is a correctness and safety boundary. What evidence may be persisted as a resolution?

## Decision Drivers

* Persistent memory must describe observed execution, not model intent.
* Dry-run adapters must remain visibly labeled in every downstream representation.
* Failed reports must never create a successful IncidentMemory.
* Provider/tool exception text and arbitrary tool output must not enter embeddings.
* The existing Tablestore schema and deterministic memory ID should remain sufficient.
* The executor remains the authority for tool policy and result validation.

## Considered Options

* Keep constructing resolution text from the approved plan.
* Store the entire raw execution trail in IncidentMemory.
* Pass the validated `ExecutionReport` to memory and derive a bounded allowlisted summary.
* Remove resolution text and persist only incident metadata.

## Decision Outcome

Proposed: **derive IncidentMemory only from a validated successful `ExecutionReport`, using bounded allowlisted result fields.**

After explicit owner acceptance:

1. `ApprovedExecutionRunner` passes the exact successful `ExecutionReport` to `remember_resolution`; plan-only memory construction is removed.
2. Memory creation rejects a report whose incident ID differs, whose `succeeded` flag is false, whose result sequence is incomplete/non-contiguous, or whose incident is not `RESOLVED`.
3. Each result is summarized from server-owned fields: sequence, registered tool, risk level, success status, `dry_run`, and a small allowlist of tool-specific evidence already validated by the adapter. Arbitrary output values, exception text, and credentials are excluded.
4. Real safe actions are labeled `executed`; caution/dangerous adapters are labeled `dry-run simulated` and never phrased as completed state changes.
5. The existing `IncidentMemory.resolution` string and Tablestore schema remain unchanged; the deterministic `mem_<incident_id>` ID remains idempotent.
6. A failed or incomplete report records execution failure through the existing state machine and does not call the memory writer.

No memory payload, executor interface, or persistent row may change while this ADR is `proposed`.

### Consequences

* Good: recalled resolutions are grounded in the same evidence shown in the decision trail.
* Good: simulated and real actions cannot be conflated in embeddings or UI.
* Good: no Tablestore schema migration is required.
* Bad: memory construction becomes coupled to versioned executor result semantics.
* Bad: every real/dry-run adapter needs an explicit safe evidence allowlist.
* Guardrail: adding raw tool output or plan-only “completed” wording requires a later accepted ADR.
