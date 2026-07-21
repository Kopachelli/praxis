---
status: superseded
date: 2026-07-19
decision-makers: Khristian Kopachelli
superseded-by: ADR-008
---
# ADR-007 — Provider routing: Qwen Cloud primary, OpenRouter fallback (payment resilience)

> Superseded by ADR-008 on 2026-07-20. ADR-008 preserves the dual Qwen-only providers and failover classes but makes Qwen Cloud first in every environment.

## Context and Problem Statement
Hackathon rules require the project to use the Qwen Cloud API and deploy on Alibaba Cloud. Khristian's South African payment methods may fail Alibaba's card binding, and the $40 voucher window is closed. The same Qwen models are available via OpenRouter (which, for qwen3.7-max, routes to Alibaba Cloud Int. as sole provider), where he holds a funded account. How do we stay compliant AND payment-resilient?

## Decision Drivers
* Stage-1 compliance gate: "must use Qwen Cloud API" — OpenRouter alone fails it.
* Real risk of Alibaba card-binding failure from SA (3DS/international/risk controls).
* Qwen Cloud free-trial tokens (~1M/model) cover the whole hackathon if preserved.
* FR-12 requires model fallback anyway; provider fallback is the same machinery.

## Considered Options
* Qwen Cloud only
* OpenRouter only
* Dual-provider chain over the same Qwen model family

## Decision Outcome
Chosen: **dual-provider chain, Qwen-family models only.** Deployed/demo: `PROVIDER_ORDER=qwencloud,openrouter`. Local dev: `openrouter,qwencloud` to preserve trial tokens. Failover on 401/402/403/404/429/5xx/timeout, logged to the decision trail. Embeddings and `deploy/alibaba_proof.py` are Qwen-Cloud-only. Non-Qwen models are forbidden in `app/` under all circumstances (extends ADR-005).

### Consequences
* Good: compliant primary path + demo that survives payment/quota failures on camera.
* Good: dev spends OpenRouter credits, not scarce trial tokens.
* Bad: Alibaba **deployment** has no equivalent fallback — account/payment blockage is stop-the-line and must be escalated at M0, not discovered at M5.
* Guardrail: any change to providers or models requires a new proposed ADR.
