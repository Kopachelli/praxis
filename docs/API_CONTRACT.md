# API_CONTRACT.md — Praxis

Base URL (prod): `https://praxis.kopachelli.dev` · FC trigger fallback: `https://<trigger-id>.<region>.fcapp.run` · Local: `http://localhost:8000`
API content type: `application/json`; `GET /` serves `text/html`. API responses include `trace_id`.

---

## Authentication boundaries [FR-6, FR-13, FR-14, ADR-025, ADR-029]

`GET /`, `GET /healthz`, and signed `POST /webhook` are public at the application boundary. The read surfaces `GET /incidents`, `GET /incidents/{id}`, `GET /incidents/{id}/memory-match`, and `GET /session` accept exactly one `Authorization: Bearer <token>` header that matches **either** the operator token **or** the separate read-only viewer token (ADR-029). The mutation surface `POST /incidents/{id}/approve` requires the operator token only; a viewer token is rejected there with the same fixed challenge, so read access never confers execution authority.

Missing, malformed, duplicate, weak, or incorrect credentials receive the same fixed `401` body, `{"detail":"Operator authentication required","trace_id":"<32 lowercase hex>"}`, plus `WWW-Authenticate: Bearer` and the matching `X-Trace-Id` header. Authentication runs before incident lookup or approval-body processing, so failure neither reveals whether an incident exists nor creates an Approval, correction, Qwen job, execution job, or state transition. The presented token authenticates a least-privilege server-owned role — `operator` (reads plus approvals) or `viewer` (reads only) — reported by `GET /session`; neither adds user-selectable identity. `PRAXIS_VIEWER_TOKEN` must be configured distinctly from `PRAXIS_OPERATOR_TOKEN`; the process refuses to start if they are equal.

## GET /  [FR-14]

Serves the dependency-free single-operator UI in a locked state. The human enters a token (operator or viewer) locally; JavaScript holds it only in memory, attaches it to protected requests, and discards it on reload/navigation. It is never embedded, stored in local/session storage, placed in a URL, or rendered back into the DOM. After unlock, the UI calls `GET /session` to resolve its role, then polls the incident read endpoints below. Decision controls appear only while an incident is `AWAITING_APPROVAL` **and** the resolved role is `operator`; a viewer token renders every incident strictly read-only with the controls absent (ADR-029).

## POST /webhook  [FR-1, FR-2]

Headers:
- `X-Praxis-Signature: sha256=<hex(hmac_sha256(WEBHOOK_SIGNING_SECRET, raw_body))>`
- `X-Idempotency-Key: <string>` (optional; if absent, derive `sha256(raw_body)`)

The key remains mapped for `DEDUP_WINDOW_SECONDS` (default `600`) from the original incident creation time. Replays do not extend the window; a request at or after expiry creates a new incident.

Current v1 retains a supplied non-empty value as an opaque process-local key;
it does not yet normalize whitespace or impose a separate header-value bound.
Proposed ADR-030 records the owner-gated correction for whitespace-only
collisions and unbounded retained keys. Proposed ADR-026/027 separately cover
authenticity and durability. None of those proposals changes this contract until
accepted.

`MAX_WEBHOOK_BODY_BYTES` defaults to `262144` (256 KiB). A valid declared length above the limit is rejected without reading the body; undeclared, malformed-length, or deceptively framed requests are still bounded while the ASGI receive stream is consumed. Oversize requests never reach signature verification, JSON parsing, or incident creation (ADR-012).

Body (Sentry-style example — normalizer must also tolerate arbitrary JSON):
```json
{
  "source": "sentry",
  "title": "TimeoutError in checkout-service",
  "service": "checkout-service",
  "level": "error",
  "message": "Upstream payment gateway timed out after 30s",
  "extra": {"region": "eu-central", "occurrences": 47}
}
```

Responses:
- `202` `{"incident_id": "inc_...", "state": "NEW", "duplicate": false, "trace_id": "..."}`
- `200` `{"incident_id": "inc_...", "duplicate": true, ...}` — dedup hit, no new agent run
- `413` `{"detail": "Payload too large", "trace_id": "..."}` when the declared or streamed body exceeds `MAX_WEBHOOK_BODY_BYTES`
- `401` `{"detail": "Invalid webhook signature", "trace_id": "..."}` for a bad/missing signature
- `422` `{"detail": "Unparseable JSON payload", "trace_id": "..."}` for an unparseable payload, including non-standard `NaN`/`Infinity` constants, finite-looking numeric exponents that overflow, and integers rejected by the runtime's bounded parser; log only its byte length and SHA-256 digest, never its raw content (NFR-3, NFR-5)
- `503` `{"detail":"Lifecycle capacity unavailable","trace_id":"..."}` when the one-running/three-pending process admission boundary is full. The rejection occurs before reserving an idempotency key or creating an Incident; a retained duplicate bypasses admission and still returns its existing incident.

Normalized fields: `source`, `service`, `severity` (`critical|high|medium|low` — mapped from source levels), `signal` (short machine label, e.g. `upstream_timeout`), and `title`. Before storage or public/Qwen use, normalized text passes through NFR-5's shared bounded redaction pipeline for URL/DSN userinfo, JWT-like tokens, recognized or unknown Authorization payloads, and sensitive assignment forms—including malformed unterminated quoted values; nested log/tool evidence crosses the same recursive boundary. Valid arbitrary JSON uses the owner-accepted defaults `source="unknown"`, `service="unknown-service"`, `severity="medium"`, `title="Alert for <service>"`, and `signal="generic_alert"`. The parsed `raw_payload` is retained internally for agent processing and audit but is excluded from webhook, list, and incident-detail responses.

## GET /incidents  [FR-13]

Requires the operator bearer token.

`200` `{"incidents": [{"id", "title", "service", "severity", "state", "created_at"}], "trace_id": "..."}` — newest first.

## GET /incidents/{id}  [FR-13, FR-14]

Requires the operator bearer token. Only an authenticated request reaches lookup; unknown IDs then return `404` `{"detail": "Incident not found", "trace_id": "..."}`.

```json
{
  "id": "inc_01H...",
  "source": "sentry",
  "service": "checkout-service",
  "severity": "high",
  "signal": "upstream_timeout",
  "title": "TimeoutError in checkout-service",
  "state": "AWAITING_APPROVAL",
  "created_at": "2026-07-19T21:04:11Z",
  "trace_id": "...",
  "memory_match": {"incident_id": "inc_00...", "similarity": 0.91, "summary": "...", "resolution": "..."},
  "plan": {
    "status": "proposed",
    "steps": [
      {"seq": 1, "action": "Restart checkout-service worker pool", "tool": "restart_service",
       "args": {"service": "checkout-service"}, "risk_level": "safe",
       "rollback": "Service auto-recovers; no rollback needed"},
      {"seq": 2, "action": "Scale gateway timeout to 60s", "tool": "update_config",
       "args": {"key": "gateway_timeout", "value": 60}, "risk_level": "caution",
       "rollback": "Revert gateway_timeout to 30"}
    ]
  },
  "trail": [
    {"seq": 1, "type": "thought", "content": "...", "model_used": "qwen3.7-max", "tokens": 812, "timestamp": "..."},
    {"seq": 2, "type": "tool_call", "content": {"tool": "fetch_logs", "args": {"service": "checkout-service"}}, "timestamp": "..."},
    {"seq": 3, "type": "tool_result", "content": "...", "timestamp": "..."},
    {"seq": 4, "type": "fallback", "content": {"from": "qwencloud/qwen3.7-max", "to": "openrouter/qwen/qwen3.7-max", "reason": "http_402"}, "timestamp": "..."},
    {"seq": 5, "type": "qwen_attempt", "content": {"provider": "openrouter", "model": "qwen/qwen3.7-max", "outcome": "success", "reason": "success", "trace_id": "..."}, "model_used": "qwen/qwen3.7-max", "timestamp": "..."}
  ]
}
```

Qwen routing uses two complementary, secret-safe trail events. A `fallback`
entry means that Praxis actually moved from one attempted provider/model pair
to the next and contains exactly `{from, to, reason}`. One terminal
`qwen_attempt` entry closes the logical call at the successful or final failed
pair and contains exactly `{provider, model, outcome, reason, trace_id}`, where
`outcome` is `success` or `failure`. Together they identify every attempted
pair without storing provider bodies, model output, credentials, or exception
text (FR-4, FR-12, NFR-4, NFR-5).

An HTTP `400` enters the same-Qwen fallback chain only when its bounded,
parsed provider body contains one of the explicitly recognized
model-unavailable error codes or message shapes. That transition appends a
`fallback` event with `reason="model_unavailable"`. A generic, malformed, or
oversized HTTP `400` remains terminal and closes the call with a
`qwen_attempt` event whose `reason` is `http_400`; provider bodies are never
copied into the trail.

Classification must return a non-empty value. If the fast-role model returns
empty or whitespace-only content, the incident remains `NEW`, no plan or model
attribution is stored, and the public trail receives only the fixed
`thought` content `{"stage":"initial_triage","status":"triage_failed"}`.

Successful plan persistence appends one server-created `thought` event with
exact content `{"stage":"plan_ready","status":"ready","trace_id":"<32 lowercase hex>"}`
in the same repository operation that enters `AWAITING_APPROVAL`. Its timestamp,
minus the incident's server-owned `created_at`, is the canonical NFR-2
triage-to-plan measurement. If that trail append fails, both the plan and state
transition roll back.

## POST /incidents/{id}/approve  [FR-6, FR-7, FR-8]

Requires the operator bearer token. Authorization failure uses the fixed challenge above before incident lookup, JSON/schema diagnostics, or mutation.

Decision-specific bodies (fields not listed for a decision are rejected, even
when supplied as `null` or an empty collection):

- `approve`: `{"decision": "approve"}`; `note` and `edits` are forbidden
- `reject`: `{"decision": "reject", "note": "non-empty correction"}`;
  `note` is required and `edits` is forbidden
- `edit`: `{"decision": "edit", "edits": [{"seq": 2, "instruction": "use 45s not 60s"}], "note": "optional"}`;
  at least one edit is required and `note` remains optional

The raw approval body limit is `MAX_APPROVAL_BODY_BYTES`, default `16384`
(16 KiB). At or below that boundary, normal JSON and schema validation applies.
Normalized `note` values are limited to 2,000 characters, each request to 20
edits, and each normalized edit `instruction` to 1,000 characters. Each edit is
the strict object `{seq, instruction}` with a positive integer `seq`.
Before an accepted reject/edit correction is persisted, added to the public
trail, or scheduled for Qwen regeneration, its note and instructions cross the
same bounded NFR-5 redaction boundary. Credential-shaped substrings are returned
as `[REDACTED]`; the decision, sequence targets, and public size limits are
unchanged.

- `approve` → state EXECUTING, executor runs, then one of three fail-closed dispositions (ADR-028): RESOLVED on a verified success; back to AWAITING_APPROVAL with a failure note when a step fails **before** any real external dispatch was recorded (a fresh Approval is then required); or terminal RECONCILIATION_REQUIRED when a real external dispatch was recorded but its outcome could not be verified as succeeded — that incident is never auto-retried and offers no approve/reject control until a human reconciles the target
- `reject` → TRIAGED; a non-empty correction `note` is required and fed back to the agent for asynchronous plan regeneration (FR-7; ADR-014)
- `edit` → TRIAGED; at least one strict `{seq, instruction}` edit is required and fed back to the agent for asynchronous plan regeneration (FR-7; ADR-014)
- `413` `{"detail": "Payload too large", "trace_id": "..."}` when the
  declared or streamed raw body exceeds `MAX_APPROVAL_BODY_BYTES`; this guard
  runs before JSON parsing, incident lookup, or state mutation
- `422` with the trace-bearing framework validation body when JSON is malformed,
  a field has the wrong type or exceeds a bound, an extra field is present, or a
  field is inapplicable to the selected decision; no Approval is recorded
- `409` if not in `AWAITING_APPROVAL` or if repository corruption leaves that
  state without a stored validated plan. Every accepted decision writes its
  Approval record atomically before execution or regeneration is scheduled.
- `503` `{"detail":"Lifecycle capacity unavailable","trace_id":"..."}` when
  process admission is full. This check occurs before recording an Approval or
  changing state, so the same authenticated human may retry after capacity is
  available; the failed request grants no execution authority.
- `503` `{"detail":"Approved execution could not be scheduled","trace_id":"..."}`
  when the approved execution scheduler raises or declines the task; the
  incident returns to `AWAITING_APPROVAL` with a fixed execution-failure note.
- `503` `{"detail":"Plan regeneration could not be scheduled","trace_id":"..."}`
  when a reject/edit regeneration scheduler raises or declines the task; the
  accepted correction and `TRIAGED` state remain durable and a fixed
  `plan_regeneration` / `scheduling_failed` thought is appended.

The superseded plan is cleared before reject/edit regeneration. The single-operator hackathon API records server-owned operator `demo-operator`; client input cannot override it. These semantics are fixed by accepted ADR-014.

Initial plan generation is an internal background task started by accepted signed-webhook intake. Correction regeneration is entered only through `reject` or `edit` above. ADR-015 excludes the previously listed public/manual `POST /incidents/{id}/plan` trigger from v1 because its authorization, state, and cost-control semantics were undefined.

## GET /incidents/{id}/memory-match  [FR-11]

Requires the operator bearer token. `200` `{"match": {"incident_id": "inc_...", "similarity": 0.91, "summary": "...", "resolution": "..."} | null, "trace_id": "<uuid>"}` — top `KnnVectorQuery` hit above `MEMORY_SIMILARITY_THRESHOLD` (default 0.80), or `null` after a clean miss/unavailable memory path. `404` uses the standard `{"detail": "Incident not found", "trace_id": "<uuid>"}` envelope.

## GET /session  [FR-13, FR-14, ADR-029]

Accepts the operator or viewer bearer token (same reader boundary as the incident reads). Returns the least-privilege role the presented token authenticates so the UI can gate its controls before rendering anything sensitive.

`200` `{"role": "operator" | "viewer", "trace_id": "<32 lowercase hex>"}`. An operator token resolves `operator`; the viewer token resolves `viewer`. A missing, malformed, or non-matching token receives the standard fixed `401` challenge. The role is server-derived from token equality only — the client cannot assert or elevate it.

## GET /healthz  [FR-15]

Public; no operator credential is required.

`200` `{"ok": true, "primary_model": "<resolved id>", "deployed_on": "alibaba-fc", "version": "...", "trace_id": "<uuid>", "real_restart_adapter_configured": <bool>, "real_dispatch_timeout_reconciliation_ready": true, "lifecycle": {"max_running_jobs": 1, "max_pending_jobs": 3, "pending_timeout_seconds": 300.0, "job_timeout_seconds": 240.0}}` — doubles as deployment-proof ping; `trace_id` also appears in the `X-Trace-Id` header. `ok` reports liveness only. `real_restart_adapter_configured` states whether the isolated restart adapter is installed; `real_dispatch_timeout_reconciliation_ready` now reports `true` because ADR-028 reconciliation is accepted and implemented, so an approved real remediation may cross the external boundary and any post-dispatch uncertainty fails closed into `RECONCILIATION_REQUIRED` rather than a false success or a blind retry (ADR-024/028). `lifecycle` reports the fixed ADR-024 admission/deadline constants (1 running, 3 pending, 300 s pending expiry, 240 s whole-job deadline) as deployed evidence, not an environment flag.

## Lifecycle admission and deadline semantics [NFR-2, ADR-024]

Initial triage, correction regeneration, approved execution, and the post-resolution memory attempt share one process-wide FIFO controller: exactly one running job and at most three pending jobs. A lease is acquired before any new intake or approval mutation that needs work; per-incident coalescing remains in force. Pending work expires after 300 wall-clock seconds, and a dequeued logical job has a 240-second application deadline covering model, tool, repository, and memory operations.

Each expiry appends one fixed secret-safe timeout event and fails closed. Initial-triage expiry leaves the incident `NEW`; correction-regeneration expiry leaves it `TRIAGED` with its immutable correction retained and no actionable plan; approved execution that expires **before** external dispatch follows the existing execution-failure transition back to `AWAITING_APPROVAL`, and a fresh Approval is required for any later run. A timeout **after** a real external dispatch was recorded is an uncertain outcome: accepted ADR-028 transitions the incident to the terminal `RECONCILIATION_REQUIRED` state (never auto-retried, no fresh approval offered) so a human reconciles the target's true state before any further action.

---

## Plan JSON contract (pydantic-enforced, model MUST comply)

```json
{
  "steps": [
    {"seq": 1, "action": "<imperative sentence>", "tool": "<registered tool name>",
     "args": {}, "risk_level": "safe|caution|dangerous", "rollback": "<sentence>"}
  ]
}
```
Reject and re-prompt (max 2 retries) on: unknown tool, missing rollback, invalid
`risk_level`, non-JSON output, or a registry-policy violation. After structural
Pydantic parsing, the active tool registry synchronously validates every step's
`args` with that selected tool's exact strict Pydantic model (including rejecting
extra fields), requires read steps to use `risk_level: "safe"`, requires write
steps to use exactly the registered risk default below, and requires at least one
registered write remediation step. This validation is side-effect-free: planning
schemas remain read-only and no proposed write is executed before HITL approval.

## Registered tools (initial)

| Tool | Kind | Risk default |
| --- | --- | --- |
| `fetch_logs(service)` | read, real | — |
| `service_status(service)` | read, real | — |
| `restart_service(service)` | write, **real** (demo target service) | safe |
| `update_config(key, value)` | write, dry-run | caution |
| `scale_service(service, replicas)` | write, dry-run | caution |
| `rollback_deploy(service, version)` | write, dry-run | dangerous |

Dry-run adapters return `{"dry_run": true, "would_have": "..."}` and the UI renders a DRY RUN badge (FR-9).
