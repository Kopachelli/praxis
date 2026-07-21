---
status: accepted
date: 2026-07-19
decision-makers: Khristian Kopachelli
---
# ADR-002 — Language/runtime: Python 3.10 + FastAPI

## Context and Problem Statement
Khristian's home stack is Elixir/Phoenix/BEAM, but the build window is 24h and the Qwen ecosystem must be exercised deeply.

## Considered Options
* Python 3.10 + FastAPI
* Node/TypeScript
* Elixir/Phoenix

## Decision Outcome
Chosen: **Python + FastAPI**, because the OpenAI-compatible SDK, DashScope examples, Tablestore SDK, and FC runtimes are first-class in Python, minimizing integration risk under deadline.

### Consequences
* Good: every code sample in Qwen docs ports directly; FC python3.10 runtime is native.
* Bad: not the durable stack — post-hackathon rewrite of the core on BEAM is planned (PRD §6.3).
