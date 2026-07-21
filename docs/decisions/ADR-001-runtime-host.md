---
status: accepted
date: 2026-07-19
decision-makers: Khristian Kopachelli
---
# ADR-001 — Runtime host: Alibaba Cloud Function Compute 3.0

## Context and Problem Statement
The hackathon mandates the backend run on Alibaba Cloud with verifiable proof. Which Alibaba service hosts Praxis fastest for a 24h solo build?

## Considered Options
* Function Compute 3.0 web function (HTTP trigger)
* ECS instance running the app directly
* Container Service (ACK/SAE)

## Decision Outcome
Chosen: **Function Compute 3.0**, because it yields a public `*.fcapp.run` URL in minutes, auto-injects Alibaba credentials via an execution role, deploys with one `s deploy`, and its FC response headers double as deployment evidence.

### Consequences
* Good: fastest path to proof; serverless demo cost ≈ zero.
* Bad: cold starts and per-instance state — mitigated by `instanceConcurrency: 1` and a warmed instance for the demo.
* Fallback: if FC provisioning blocks >45 min at M0, switch to a small ECS instance; proof requirement is still satisfied.
