---
status: proposed
date: 2026-07-21
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-013
---
# ADR-021 — Bound model output, plan shape, and embedding wall time

## Context and Problem Statement

ADR-013 bounds each provider attempt and complete logical chat call by wall clock. Praxis also caps tool rounds, plan reprompts, normalized incident fields, and embedding input characters. Three resource paths remain insufficiently bounded:

* chat requests omit an explicit output-token ceiling;
* `RemediationPlan` requires at least one step but does not cap step count or action/rollback text, and raw plan JSON has no application byte ceiling;
* the embedding HTTP client has phase timeouts but the complete request is not wrapped in an application wall-clock deadline.

Alibaba's current OpenAI-compatible Qwen API documents `max_completion_tokens` as the complete output limit including chain-of-thought and answer for supported Qwen thinking models. OpenRouter accepts the same parameter and treats reasoning tokens as output tokens. What limits keep classification and planning predictable across both accepted providers without changing the model family or routing order?

## Decision Drivers

* Preserve Qwen thinking quality while bounding cost, latency, parsing, and memory.
* Use one total-output field supported by both accepted provider APIs.
* Keep the fast classification role much smaller than primary planning.
* Reject oversized model output before JSON parsing or trail persistence.
* Keep tool argument validation authoritative in the registry.
* Ensure a slowly progressing embedding response cannot exceed its intended 15-second budget.

## Considered Options

* Rely only on existing wall-clock deadlines and provider defaults.
* Set `max_tokens`, which Alibaba documents as answer-only for thinking models.
* Set provider-neutral `max_completion_tokens` plus application plan/response bounds and a hard embedding deadline.
* Add provider-specific reasoning budgets for every model in the fallback matrix.

## Decision Outcome

Proposed: **use total-completion limits plus fail-closed application bounds, without introducing provider-specific reasoning controls.**

After explicit owner acceptance and live compatibility verification for every accepted provider/model pair:

1. Every chat payload includes `max_completion_tokens`: 512 for the fast classification role and 8,192 for primary/tool/planning calls. For supported thinking models this bounds reasoning plus visible answer; no non-Qwen model or route is added.
2. Praxis rejects a non-streamed provider response body larger than 256 KiB before JSON decoding and records a fixed `response_too_large` terminal reason without provider text.
3. A remediation plan may contain 1–8 steps. `action` is capped at 500 characters and `rollback` at 1,000 characters. Registered tool names and arguments remain governed by their strict registry schemas; the canonical JSON encoding of one step's `args` is additionally capped at 8 KiB.
4. Raw candidate plan JSON is capped at 64 KiB before Pydantic parsing. Oversize candidates consume the existing bounded reprompt budget with a fixed safe diagnostic.
5. `QwenCloudEmbeddingClient.embed` wraps the complete HTTP operation in an `asyncio` 15-second wall-clock deadline in addition to HTTPX phase timeouts. Timeout remains a secret-safe best-effort memory failure; embeddings still have no OpenRouter route.
6. Configuration values are fixed constants for v1, not client-controlled fields. Changing them or adding provider-specific reasoning budgets requires another accepted ADR.

No provider payload, plan schema, timeout, or validation implementation may change while this ADR is `proposed`.

### Consequences

* Good: normal and adversarial model responses have deterministic cost and memory ceilings.
* Good: the same total-output parameter is sent across the accepted Qwen Cloud/OpenRouter route.
* Good: embeddings gain the same hard wall-clock property as chat calls.
* Bad: a valid complex plan can be truncated or rejected and consume a reprompt.
* Bad: exact limits require live matrix verification because parameter handling can vary by model/provider.
* Neutral: provider-specific reasoning budgets remain available for a later measured optimization.

## References

* [Alibaba Model Studio OpenAI-compatible chat parameters](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions)
* [Alibaba Model Studio deep-thinking controls](https://www.alibabacloud.com/help/en/model-studio/deep-thinking)
* [Alibaba Model Studio text-embedding-v4 limits](https://www.alibabacloud.com/help/en/model-studio/embedding)
* [OpenRouter request parameters](https://openrouter.ai/docs/api/reference/parameters)
* [OpenRouter reasoning-token controls](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
