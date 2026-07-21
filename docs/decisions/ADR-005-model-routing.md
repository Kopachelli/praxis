---
status: accepted
date: 2026-07-19
decision-makers: Khristian Kopachelli
---
# ADR-005 — Model routing: configurable chain, qwen3.8-max-preview primary

## Context and Problem Statement
Qwen3.8-Max-Preview surfaced the morning of the build (2026-07-19) but its Model Studio API availability is unconfirmed; the demo cannot depend on an unverified ID, yet using the newest flagship strengthens the Innovation score.

## Considered Options
* Single hardcoded model
* Env-configurable fallback chain

## Decision Outcome
Chosen: **env-configurable chain** `PRIMARY_MODEL=qwen3.8-max-preview` → `qwen3.7-max` → `qwen3-max` → `qwen-plus`, with `FAST_MODEL=qwen-flash` for classification and `text-embedding-v4@1024` for embeddings. Fallbacks are recorded to the decision trail (FR-12), turning resilience into a demoable feature.

### Consequences
* Good: newest model when available, verified models otherwise; zero-downtime demo.
* Good: token cost discipline (NFR-6) via routing.
* Verify: exact primary ID confirmed at M0 (OQ-5) and recorded in .env + worklog.

## M0 Verification Outcome

On 2026-07-19, credentialed calls found `qwen3.8-max-preview` unavailable on both configured providers (Qwen Cloud HTTP 404; OpenRouter HTTP 400). Per the accepted fallback decision, Praxis selects `PRIMARY_MODEL=qwen3.7-max`; `qwen3-max` and `qwen-plus` remain verified fallbacks. All three selected model pairs returned successful chat completions on both providers.
