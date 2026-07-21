---
status: accepted
date: 2026-07-19
decision-makers: Khristian Kopachelli
---
# ADR-003 — Track selection: Track 4 (Autopilot Agent)

## Context and Problem Statement
Five tracks; one solo dev; ~24h. Which track maximizes expected placement?

## Considered Options
* Track 4 Autopilot Agent
* Track 1 MemoryAgent
* Track 3 Agent Society

## Decision Outcome
Chosen: **Track 4**, because it explicitly rewards production-readiness, tool use, and human-in-the-loop — the exact strengths of a webhook/automation specialist — while Track 1 is the most crowded and Track 3 adds multi-agent demo fragility. Scored 92/100 vs 86 and 87 against the published rubric.

### Consequences
* Good: cleanest 3-minute demo; lowest execution risk.
* Good: a thin Tablestore memory slice (M4) honestly claims cross-track depth without switching tracks.
