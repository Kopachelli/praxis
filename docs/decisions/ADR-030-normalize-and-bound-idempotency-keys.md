---
status: proposed
date: 2026-07-21
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-012
---
# ADR-030 — Normalize and bound webhook idempotency keys

## Context and Problem Statement

POST /webhook accepts an optional X-Idempotency-Key; when it is absent, Praxis
derives the key from sha256(raw_body). The current implementation treats a
whitespace-only header as present and stores it unchanged. Two different,
correctly signed payloads carrying the same whitespace-only value can therefore
collapse into one process-local incident during the deduplication window.
Arbitrarily long caller-supplied values are also retained in both the incident
record and the deduplication map.

ADR-012 bounds the request body but does not define a header-value bound.
Proposed ADR-026 would later bind idempotency identity to webhook authenticity,
and proposed ADR-027 would make the authority durable, but neither proposal
should leave the current parsing boundary ambiguous. What exact v1 rule prevents
blank-key collisions and unbounded retention without inventing client identity?

## Decision Drivers

* Different signed bodies must not deduplicate merely because a caller supplied
  optional whitespace.
* The server must bound every retained caller-controlled identifier.
* Missing and effectively blank optional headers should use the existing
  body-hash fallback.
* Valid demo and conventional UUID-style keys must remain straightforward.
* Validation must occur before incident reservation and must never echo the key.
* ADR-026 and ADR-027 must remain independently gated proposals.

## Considered Options

* Preserve every non-empty header value exactly as supplied.
* Treat only whitespace-only values as absent and retain all other values
  without a length bound.
* Trim optional HTTP whitespace, treat an empty result as absent, and validate a
  bounded visible-ASCII identifier.
* Remove caller-supplied idempotency keys and always hash the request body.

## Decision Outcome

Proposed: **trim optional HTTP whitespace, use the body-hash fallback when the
result is empty, and otherwise accept only 1–200 visible ASCII characters.**

After explicit owner acceptance:

1. If X-Idempotency-Key is absent or its stripped value is empty, Praxis uses
   sha256(raw_body), exactly as it already does for an absent header.
2. A non-blank key is stripped once, then must contain 1–200 characters in the
   visible ASCII range U+0021 through U+007E. Control characters, internal
   whitespace, non-ASCII text, and values over 200 characters are rejected.
3. Rejection occurs after the existing request-body and HMAC checks but before
   incident creation or deduplication reservation. The response uses one fixed
   422 invalid_idempotency_key error and carries the ordinary trace header; it
   never echoes the rejected value.
4. The normalized value is the sole process-local deduplication key and the
   value stored on the incident. Leading or trailing optional whitespace cannot
   create a second identity.
5. The server-derived body hash remains a lowercase 64-character hexadecimal
   value and is valid under the same retained-value bound.
6. scripts/fire_alert.py continues to generate visible-ASCII keys below the
   bound. Tests cover missing, empty, whitespace-only, boundary-length,
   over-bound, non-ASCII, control-character, and normalized replay cases.
7. API, architecture, README, demo, and security documentation must distinguish
   this parsing/bounding rule from ADR-026 authenticity and ADR-027 durability.
8. No signature format, deduplication window, persistence backend, provider,
   model, endpoint path, or response success schema changes under this decision.

No webhook validation, error response, normalization, storage, test expectation,
or deployment may change while this ADR is proposed.

### Consequences

* Good: whitespace-only optional keys can no longer merge unrelated payloads.
* Good: caller-controlled retained identity has a deterministic memory bound.
* Good: blank-key behavior matches the existing absent-header fallback.
* Bad: clients using spaces, non-ASCII text, or more than 200 characters must
  change their key format.
* Bad: introduces a new fixed validation error that clients may need to handle.
* Guardrail: this decision does not claim the separate key is authenticated or
  durable; ADR-026 and ADR-027 remain proposed.
