---
status: accepted
date: 2026-07-22
decision-makers: Khristian Kopachelli
consulted: Claude Code
informed: Praxis contributors
amends: ADR-025
relates-to: ADR-029
---
# ADR-031 — Optional public read-only demo access

## Context and Problem Statement

ADR-025 protects every incident read behind an operator bearer token, and ADR-029 adds a distinct read-only viewer token so a recorder can watch without approval authority. For live hackathon judging, both still require distributing and rotating a credential, and the viewer token must be set as a deployed secret. That is more friction than a public demo of synthetic incidents warrants: judges want to open the dashboard and look, and the recorder tool wants to drive a read-only page — neither needs to change anything.

What is the least-privilege way to let anyone view the live demo dashboard with no token, without weakening the mutation boundary or making it the default for non-demo deployments?

## Decision Drivers

* Judges and the recorder should reach the read-only dashboard with zero credentials.
* No caller may ever approve, reject, edit, trigger, or otherwise change state without the operator token.
* The change must be opt-in and default to ADR-025's protected reads, so no non-demo deployment is silently opened.
* Only synthetic demo incidents are exposed; the shared redaction pipeline already strips credentials from alert evidence, and no operator/viewer token or provider secret appears in any read response.
* The owner remains the only actor who performs the two explicit approval actions.

## Considered Options

* Publish the ADR-029 viewer token value (a "universal" read-only token).
* Make incident reads unconditionally public (remove ADR-025 for reads).
* Add an opt-in flag that opens only the read endpoints to anonymous callers, defaulting off.
* Keep credentials and rely on the demo video only, with no live judge access.

## Decision Outcome

Chosen: **add an opt-in `PRAXIS_PUBLIC_DEMO_READS` flag that admits anonymous callers to the incident READ endpoints as a read-only `viewer`, while mutations stay operator-only. The flag defaults off, preserving ADR-025.**

Khristian selected this option on 2026-07-22 when choosing how judges and DemoSmith access the demo dashboard.

Implementation:

1. `PRAXIS_PUBLIC_DEMO_READS` is a boolean setting (default `False`). It is set as a literal, non-secret environment variable in the deployment manifest for the demo; no `.env` secret and no viewer token are required to enable it.
2. When the flag is on, the reader dependency admits a request with no valid token as role `viewer` for `GET /incidents`, `GET /incidents/{id}`, `GET /incidents/{id}/memory-match`, and `GET /session`. A valid operator token still resolves to `operator`; a valid viewer token still resolves to `viewer`.
3. `POST /incidents/{id}/approve` keeps the operator-only dependency, and `POST /webhook` keeps its HMAC signature boundary. The flag never affects any mutation surface, so a read-only visitor can never change state.
4. When the flag is off, every read endpoint behaves exactly as under ADR-025/029 (token required, fixed `401` challenge). The flag is the only difference.
5. The controller stamps the flag into the served HTML. When on, the operator UI auto-enters a read-only view with no token and also offers an explicit "view read-only demo" control; approve/reject controls stay hidden and the operator unlock path is unchanged. The UI attaches an `Authorization` header only when a token is present.
6. Tests prove: anonymous reads succeed and resolve to `viewer` only when the flag is on; anonymous reads are rejected when it is off; an anonymous mutation is always rejected and changes nothing; an operator token still resolves to `operator`; and the served HTML carries the correct flag value.

### Consequences

* Good: judges and the recorder reach the live read-only dashboard with no credential and no rotation step.
* Good: the mutation boundary and webhook authenticity are untouched; only reads open, and only when explicitly enabled.
* Good: default-off means every other deployment keeps ADR-025's protected reads.
* Bad: while enabled, anyone can read the (synthetic) demo incident collection and decision trails.
* Bad: adds a config flag, an anonymous-reader auth path, a UI auto-enter path, and their test surface.
* Guardrail: public access authorizes reads only; changing anything still requires the operator token, which is never shared or published.
