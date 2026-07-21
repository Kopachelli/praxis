---
status: accepted
date: 2026-07-21
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
amends: ADR-025
---
# ADR-029 — Give a third-party demo recorder least-privilege read access

## Context and Problem Statement

Proposed ADR-025 protects every incident read and approval mutation with one strong operator bearer token. The selected recording workflow uses DemoSmith as a read-only camera operator after Khristian has approved and resolved the exact demo incident. Giving a third-party recorder the operator token would also authorize approve, reject, and edit requests; leaving incident reads anonymous would undo ADR-025 on both the custom domain and the native Function Compute URL.

The current DemoSmith brief therefore says both “login credentials: none” and “never give DemoSmith an operator token.” Those instructions work only while the public incident surface remains unauthenticated. What is the smallest access boundary that can show the real resolved application without granting remediation authority, exposing a credential in recording artifacts, or assuming an unverified vendor secret-handling feature?

## Decision Drivers

* DemoSmith must never approve, reject, edit, trigger, or otherwise schedule a state-changing action.
* The owner remains the only actor who performs the two explicit approval actions.
* Protect incident evidence through both the Cloudflare domain and the native FC URL.
* Never place a credential in a prompt, URL, screenshot, recording, DOM, browser storage, application log, incident trail, or repository file.
* Record the real deployed UI and one already-resolved incident rather than a synthetic replacement.
* Keep the single-operator hackathon scope and make access short-lived and removable.
* Fail closed if DemoSmith's credential-entry and retention behavior cannot be verified before use.

## Considered Options

* Give DemoSmith the operator token from ADR-025.
* Keep incident reads anonymous while protecting only approval mutations.
* Publish a redacted immutable snapshot endpoint for recording.
* Use only a manually captured authenticated owner session.
* Add a separate strong viewer bearer token accepted only by protected read endpoints, with manual owner capture as the mandatory fallback when vendor handling is not verified.

## Decision Outcome

Chosen: **add a distinct, short-lived viewer bearer role for protected incident reads only; never share the operator credential, and fall back to manual authenticated owner capture unless DemoSmith's secret-entry path is verified as non-recording and non-persistent.**

Khristian accepted this decision on 2026-07-21 to give judges a safe read-only view of the live app.

After explicit owner acceptance, and only after ADR-025 is accepted and implemented:

1. Production loads a separate `PRAXIS_VIEWER_TOKEN` through the same FC secret boundary and validates it with the same strong, bounded, visible-ASCII policy as the operator token. The two configured values must not be equal; equality fails startup without echoing either value.
2. A valid viewer token may call only `GET /incidents`, `GET /incidents/{id}`, and `GET /incidents/{id}/memory-match`. It is never accepted by `POST /webhook`, `POST /incidents/{id}/approve`, a future correction/reconciliation endpoint, or any tool/target endpoint. Server-side authorization is authoritative even when controls are hidden.
3. The UI can unlock into a viewer mode held only in page memory. Viewer mode fetches protected evidence but does not render or enable approve, reject, edit, trigger, or retry controls. Reloading or navigating discards the token.
4. Neither role token may appear in HTML, JavaScript bundles, query strings, fragments, cookies, local/session storage, DOM text or attributes, logs, trails, errors, prompts, screenshots, or recording instructions. Invalid credentials receive the fixed ADR-025 authentication response; a valid viewer attempting a mutation receives one fixed, non-disclosing authorization failure and creates no state/task/audit side effect.
5. Before supplying the viewer token to DemoSmith, Khristian must verify from current vendor documentation or the product UI that the value can be entered through a credential mechanism excluded from prompts, generated narration, screenshots, recordings, exports, and retained run logs. If that cannot be verified, DemoSmith receives no live credential and the owner records the authenticated browser segment manually.
6. The viewer credential is enabled only for the recording window, rotated or removed immediately after the export is retrieved, and never reused as an operator credential. Rotation/removal and a post-rotation denial check are recorded in the worklog without the value.
7. DemoSmith may open only after the owner has verified the exact incident is `RESOLVED`; it remains a read-only camera operator. The human approval moment stays in a separate owner-controlled capture and is never delegated to recording automation.
8. The v1 viewer role can read the bounded incident collection available to the single-operator controller during its short recording window. Per-incident capability tokens, public snapshots, multi-user identity, and vendor federation are out of scope and require a separate accepted ADR.
9. Tests must prove every viewer-allowed GET, every forbidden mutation, operator/viewer token separation, in-memory-only UI handling, fixed failure responses, zero side effects, redaction, and credential rotation behavior before any third-party access is used.

Accepted 2026-07-21: implementation may now proceed. The viewer role is read-only and server-authoritative; the operator credential is never shared with it, and the owner-only two-action approval remains unchanged. Credential distribution and rotation for any specific recorder (e.g. DemoSmith) remain the operational guardrails in clauses 5–7.

### Consequences

* Good: the recorder can inspect the real resolved application without receiving remediation authority.
* Good: operator authentication remains enforced on the native and custom URLs.
* Good: an unverified vendor credential path fails closed to manual owner recording.
* Bad: adds a second role, secret, UI state, authorization matrix, rotation step, and test surface.
* Bad: the short-lived viewer can read the bounded single-operator incident collection rather than one incident only.
* Bad: recording requires either verified vendor secret handling or an additional manual browser capture.
* Guardrail: a recorder credential can authorize reads only; an operator credential is never shared with recording automation.
