---
status: accepted
date: 2026-07-20
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-005
---
# ADR-009 — Provider-specific FAST_MODEL routing

## Context and Problem Statement

ADR-005 assigns routine classification to `FAST_MODEL=qwen-flash`, and ADR-008 requires Qwen Cloud first with same-Qwen OpenRouter fallback. The two providers do not expose the fast role under the same model identifier.

Credentialed probes on 2026-07-20 found:

* Qwen Cloud `qwen-flash` works.
* OpenRouter `qwen/qwen-flash` fails with HTTP 400 and is absent from its model catalog.
* OpenRouter `qwen/qwen3.6-flash` works and is a Qwen-family fast model.

Without an explicit mapping, a Qwen Cloud failure during routine classification would send an invalid model ID to OpenRouter. How should Praxis preserve NFR-6's low-cost fast role and ADR-008's provider failover semantics?

## Decision Drivers

* Every runtime model must remain in the Qwen family.
* Qwen Cloud remains the first provider for every environment.
* OpenRouter is entered only after an ADR-008 failure from the Qwen Cloud fast call.
* Routine classification should not consume the primary reasoning model under normal conditions.
* Provider/model attempts and transitions must remain explicit in the decision trail.
* Live-verified identifiers are safer than inferred aliases.

## Considered Options

* Send the same `qwen-flash` identifier to both providers.
* Use `qwen3.7-max` for classification on both providers.
* Map the fast role to a live-verified Qwen identifier per provider.

## Decision Outcome

Accepted: **map the fast role per provider.**

* Qwen Cloud fast model: `qwen-flash`.
* OpenRouter fast model: `qwen/qwen3.6-flash`.
* Keep `FAST_MODEL=qwen-flash` as the Qwen Cloud/default fast-role setting and add `OPENROUTER_FAST_MODEL=qwen/qwen3.6-flash` for the provider-specific fallback identifier.
* A routine classification call starts with Qwen Cloud `qwen-flash`. Praxis attempts the OpenRouter fast model only after an ADR-008 failure class: authentication/payment failure, unavailable model, quota/rate limiting, 5xx, or timeout.
* This decision does not change the ADR-005 primary reasoning chain, embedding model, provider order, or HITL rules.
* The decision trail records the actual provider and model identifier for every attempt and transition.

Khristian accepted this decision on 2026-07-20. Production implementation remains scheduled for M2 so the M0 milestone gate is preserved.

### Consequences

* Good: both fast-role paths use identifiers proven live with the configured accounts.
* Good: routine calls retain lower latency and cost on either provider.
* Good: OpenRouter remains Qwen-only and fallback-only.
* Bad: configuration now contains a provider-specific fast-model field instead of one universally portable identifier.
* Bad: the two fast models are not the same dated model generation, so classification output can vary after failover.
* Guardrail: changing either fast identifier or adding another provider requires a proposed ADR before implementation.
