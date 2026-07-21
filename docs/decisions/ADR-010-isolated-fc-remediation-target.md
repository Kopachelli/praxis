---
status: accepted
date: 2026-07-20
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-001, ADR-006
---
# ADR-010 — Isolate the real remediation target on Function Compute

## Context and Problem Statement

FR-8 requires one approved remediation to execute for real, while FR-6, FR-9, and ADR-006 prohibit unsafe or unapproved state changes. OQ-4 asked which single safe tool should execute on camera. Khristian authorized an isolated Function Compute demo target on 2026-07-20.

Restarting the Praxis API itself would make the approval loop fragile and couple the control plane to its remediation target. A local-only target would weaken the deployed demo. How should Praxis demonstrate a real state-changing tool without risking unrelated infrastructure?

## Decision Drivers

* Every state change remains behind the HITL approval gate.
* The camera demo must show a real, deployed action.
* The target must be isolated from the Praxis API and unrelated workloads.
* The adapter must be narrowly scoped and auditable.
* Risky or non-demo targets remain labeled dry-run only.

## Considered Options

* Restart the primary `praxis-api` Function Compute function.
* Restart a local mock service.
* Provision a dedicated isolated Function Compute demo target and allow only that target through the real adapter.

## Decision Outcome

Accepted: **use a dedicated isolated Function Compute demo target.**

* Provision a separate demo-only Function Compute target, proposed resource name `praxis-demo-target`.
* The real remediation adapter may restart only that exact allowlisted target and only after an approved plan enters `EXECUTING`.
* Praxis records the target, approval, attempt, and result in the incident trail without logging credentials.
* Every other state-changing target or action uses a clearly labeled dry-run adapter.
* The target is disposable demo infrastructure and contains no production or user data.

Khristian accepted this exact boundary on 2026-07-20. M3 may provision the target and implement the narrowly allowlisted real adapter.

### Consequences

* Good: the demo proves a real cloud action without restarting the controller.
* Good: exact-target allowlisting makes the safety boundary easy to test and explain.
* Good: the action stays entirely on Alibaba Cloud.
* Bad: the submission needs one additional Function Compute resource.
* Bad: the target adds deployment and rehearsal steps during a tight deadline.
* Guardrail: neither the agent nor operator input may override the allowlisted target.
