---
status: proposed
date: 2026-07-21
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-012
---
# ADR-026 — Bind webhook idempotency identity to authenticity

## Context and Problem Statement

Praxis currently authenticates only the raw request body with HMAC-SHA256, while the optional `X-Idempotency-Key` header independently selects the deduplication identity. Anyone who captures one valid body/signature pair can replay it with different idempotency headers, creating multiple incidents and Qwen runs. The body is authentic, but the caller-controlled identity that governs cost and replay protection is not.

How should the webhook contract cryptographically bind the idempotency identity without ambiguous concatenation or an unsafe legacy path?

## Decision Drivers

* Preserve caller-chosen idempotency keys for distinct alert occurrences.
* Keep body-derived identity available for simple clients that omit the header.
* Prevent one captured signature from authorizing arbitrary idempotency identities.
* Use a versioned, unambiguous byte-level signing contract.
* Permit a safe migration from body-only signatures without retaining the vulnerability.
* Keep verification bounded and constant-time after strict parsing.

## Considered Options

* Keep body-only HMAC and trust the unsigned header.
* Ignore the header and always deduplicate by raw-body SHA-256.
* Sign a delimiter-concatenated string containing the key and body.
* Introduce a versioned length-prefixed signing envelope and restrict the legacy signature to requests with no idempotency header.

## Decision Outcome

Proposed: **authenticate a versioned length-prefixed envelope whenever the caller supplies an idempotency key; retain legacy body-only verification only when that header is absent.**

After explicit owner acceptance:

1. A request with `X-Idempotency-Key` must use `X-Praxis-Signature: v1=<hex>` over the exact byte envelope `b"praxis.webhook.v1\x00" + uint32_be(len(key_bytes)) + key_bytes + raw_body`.
2. The server reads the exact raw ASGI header value bytes without trimming or text re-encoding. `key_bytes` must match the complete visible-ASCII grammar `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`: 1–128 bytes, no optional whitespace, internal whitespace, controls, or non-ASCII. The four-byte length prefix makes the envelope unambiguous; JSON and header values are never reparsed, normalized, or canonicalized for signing.
3. A request without `X-Idempotency-Key` derives its identity as `sha256(raw_body)` and may use the existing `sha256=<hex(hmac(secret, raw_body))>` form during the compatibility window. Because no unsigned identity input exists on that path, changing headers cannot bypass deduplication.
4. A legacy `sha256=` signature combined with any idempotency header fails with the fixed invalid-signature response. The server never silently ignores a supplied key and never falls back from failed `v1` verification to legacy verification.
5. The raw request must contain exactly one `X-Praxis-Signature` header and at most one `X-Idempotency-Key` header, compared case-insensitively by header name. Duplicate signature or idempotency headers are rejected before verification; comma-joining them is forbidden.
6. The seed client signs the exact bytes it sends, exposes separate deterministic/fresh/recurrence key modes, and has golden-vector tests for the minimum and maximum valid keys plus empty, leading/trailing whitespace, internal whitespace, non-ASCII, duplicate-header, malformed-prefix, and body-tamper rejection.
7. Signature parsing accepts one scheme only, requires the exact digest length and lowercase/uppercase hexadecimal equivalently, performs constant-time digest comparison, and logs only fixed failure labels plus request length/digest metadata already allowed by NFR-5.
8. After a documented migration window, removing the no-header legacy form may be proposed separately. Changing envelope version, key grammar/bounds, or fallback behavior requires another accepted ADR and API-contract update.

No signature scheme, idempotency-key validation, seed-client behavior, or API response may change while this ADR is `proposed`.

### Consequences

* Good: a captured request cannot be multiplied by changing an unsigned identity header.
* Good: callers can still distinguish identical payloads that represent different alert occurrences.
* Good: no-header clients retain a safe body-derived compatibility path.
* Bad: every caller that supplies a key must migrate its signing implementation atomically.
* Bad: callers must use the portable visible-ASCII key grammar; proxies or libraries that rewrite the key bytes will cause verification failure.
* Guardrail: failed `v1` verification must never downgrade to the legacy scheme.
