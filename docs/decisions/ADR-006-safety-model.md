---
status: accepted
date: 2026-07-19
decision-makers: Khristian Kopachelli
---
# ADR-006 — Safety model: mandatory HITL + labeled dry-run adapters

## Context and Problem Statement
An autopilot that mutates infrastructure on camera must be both credible and safe; Track 4 explicitly requires human-in-the-loop.

## Considered Options
* Fully autonomous execution
* Mandatory approval gate + risk-tiered adapters

## Decision Outcome
Chosen: **mandatory approval gate**: no transition into EXECUTING without an Approval record (enforced in the state machine, FR-6); write tools carry `risk_level`; `safe` steps may use the real adapter, `caution|dangerous` use dry-run adapters that clearly label output (FR-9).

### Consequences
* Good: production-credible, judge-safe, demo-safe.
* Bad: no fully-autonomous path in v1 — by design (PRD NG2).
