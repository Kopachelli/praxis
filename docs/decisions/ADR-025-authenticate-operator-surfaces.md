---
status: accepted
date: 2026-07-21
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-014
---
# ADR-025 — Authenticate operator incident and approval surfaces

## Context and Problem Statement

Accepted ADR-014 deliberately used server-owned attribution `demo-operator` because authentication was outside the 24-hour hackathon scope. The deployed controller is anonymous, `GET /incidents` reveals incident identifiers, and `POST /incidents/{id}/approve` accepts approve, reject, and edit decisions without proving that the caller is the operator. An Internet client can therefore cross the human gate and schedule the real isolated restart even though the state machine contains a valid Approval record.

The HITL invariant requires an explicit human decision, but an unauthenticated request is not evidence that the authorized human made it. What is the smallest browser-compatible authentication boundary that protects both operational evidence and state-changing decisions without putting a secret in public JavaScript?

## Decision Drivers

* Preserve explicit human review and the second native confirmation before approval.
* Protect the raw FC URL as well as the Cloudflare custom domain.
* Keep `/webhook` on its separate HMAC authentication boundary.
* Do not embed an operator credential in HTML, URLs, logs, trails, or browser storage.
* Retain the single-operator v1 scope; identity federation and multi-user authorization remain post-hackathon.
* Fail production startup when the operator credential is absent or trivial.

## Considered Options

* Keep the anonymous controller and rely on an unguessable incident ID.
* Put only the custom domain behind Cloudflare Access.
* Add a password/login endpoint with server sessions, cookies, and CSRF protection.
* Require one strong operator bearer token on every incident read and approval mutation, entered by the operator and retained only in page memory.

## Decision Outcome

Chosen: **require a strong application-level operator bearer token for incident reads and approval mutations.**

Khristian accepted this decision on 2026-07-21.

After explicit owner acceptance:

1. `GET /`, `GET /healthz`, and signed `POST /webhook` retain their current public boundaries. `GET /incidents`, `GET /incidents/{id}`, `GET /incidents/{id}/memory-match`, and `POST /incidents/{id}/approve` require `Authorization: Bearer <operator token>`.
2. The dependency-free UI initially renders a locked operator view. The human enters the token locally; JavaScript holds it only in memory, attaches it to protected requests, and discards it on reload/navigation. The value is never embedded, persisted to local/session storage, placed in a query string, or rendered back to the DOM.
3. Production loads `PRAXIS_OPERATOR_TOKEN` from the environment/FC secret boundary and applies the existing non-trivial, bounded, visible-ASCII secret policy. Missing, weak, placeholder, whitespace-containing, non-visible-ASCII, or overlong values fail startup without echoing the value.
4. Missing or invalid credentials receive one fixed trace-bearing `401` response with `WWW-Authenticate: Bearer`; authorization failures do not disclose whether an incident exists and do not create an Approval, correction, Qwen task, or execution task.
5. The server continues to record the role identity `demo-operator`; this token authenticates the one operator role and does not add user-selectable attribution. A named/multi-user identity model requires another ADR.
6. Constant-time comparison is used only after both configured and supplied tokens pass safe byte/character validation. Request-boundary logs contain fixed outcome labels and trace/incident context only, never the header or exception text.
7. Cloudflare Access may later add defense in depth, but it cannot replace application-level enforcement because the native FC trigger URL is also reachable. Disabling the native URL or adding federated identity requires another accepted ADR.

The public build must not expose a fresh approval candidate until the
application-level operator boundary and its browser workflow are deployed and
verified through both the custom domain and native Function Compute URL.

### Consequences

* Good: an anonymous Internet client can no longer inspect incidents or cross the approval boundary.
* Good: protection applies identically through the custom domain and native FC URL.
* Good: the change does not add autonomous approval or weaken the two-click human flow.
* Bad: the operator must enter the token after every page reload.
* Bad: bearer-token compromise grants the single operator role until the token is rotated.
* Bad: every screenshot/rehearsal now needs an explicit unlock step that must remain off camera or be safely framed.
* Guardrail: no first-party page, document, demo prompt, or log may contain the operator token.
