---
status: accepted
date: 2026-07-20
decision-makers: Khristian Kopachelli
supersedes: ADR-007
---
# ADR-008 — Provider routing: Qwen Cloud first in every environment

## Context and Problem Statement

ADR-007 selected Qwen Cloud first for deployed/demo traffic and OpenRouter first for local development, primarily to preserve Model Studio free-trial tokens during the build.

Khristian has purchased a Qwen coding plan and requested that development exercise Qwen Cloud before OpenRouter. Alibaba's coding-plan key and promotional credits are separate from the Praxis application runtime: official terms restrict coding-plan keys to supported interactive coding agents, so Praxis continues to use a general Model Studio API key and endpoint. The commercial promotion is not a runtime dependency.

How should Praxis route normal local, deployed, rehearsal, and demo model traffic while preserving a resilient fallback?

## Decision Drivers

* Hackathon compliance requires genuine Qwen Cloud API use.
* Development should continuously exercise the same compliance-critical primary path used in production.
* FR-12 requires recovery when the primary provider/model chain encounters an eligible authentication/payment, unavailable-model, quota/rate-limit, 5xx, or timeout failure.
* Every runtime model must remain in the Qwen family.
* Coding-plan entitlement, promotional pricing, and individual model availability must never be inferred; runtime access is verified with the general Model Studio credential.

## Considered Options

* Keep OpenRouter first locally and Qwen Cloud first only when deployed.
* Remove OpenRouter and use Qwen Cloud exclusively.
* Use Qwen Cloud first in every environment and retain OpenRouter as the same-Qwen availability fallback.

## Decision Outcome

Chosen: **Qwen Cloud first in every environment, with OpenRouter retained only as an automatic same-Qwen fallback.**

`PROVIDER_ORDER=qwencloud,openrouter` applies to local development, Function Compute deployments, rehearsals, and the demo. Normal requests begin with the configured Qwen Cloud model chain. Praxis does not proactively route ordinary development traffic through OpenRouter. The OpenRouter chain is entered only after a verified FR-12 failure class: authentication or payment failure, unavailable model, quota or rate limiting, 5xx response, or timeout. Other request/transport failures terminate safely without inventing a provider transition. Every attempted pair and actual transition is recorded in the decision trail.

Embeddings and `deploy/alibaba_proof.py` remain Qwen-Cloud-only. Both provider paths remain restricted to Qwen-family models. This decision changes provider priority only; ADR-005's verified model chain remains unchanged. A newly claimed model such as `qwen3.8-max` requires credentialed verification and a separate proposed ADR before it can enter runtime configuration.

### Consequences

* Good: development continuously validates the compliance-critical Qwen Cloud path.
* Good: local, deployed, rehearsal, and demo routing have production parity.
* Good: OpenRouter still protects the demo if Qwen Cloud reaches a limit or becomes unavailable.
* Good: no non-Qwen runtime provider or model is introduced.
* Bad: general Model Studio quota or spend is consumed during development.
* Bad: the separate coding-plan promotion cannot be counted as Praxis runtime capacity.
* Guardrail: changing provider order, provider set, fallback conditions, or model chain requires another proposed ADR.
