# PRD — Praxis

**Alert-to-Remediation Autopilot Agent — Qwen Cloud Global AI Hackathon, Track 4 (Autopilot Agent)**

| Field | Value |
| --- | --- |
| PRD ID | PRD-PRAXIS-001 |
| Codename | Praxis (turning alerts into action) |
| Status | Accepted — build in progress |
| Owner | Khristian Kopachelli |
| Version | 1.0 |
| Date | 2026-07-19 |
| Hard deadline | **2026-07-20 14:00 PDT** (Devpost submission); internal cutoff 12:00 PDT |
| Related | `docs/decisions/ADR-001..029`, `docs/BUILD_PLAN.md`, `docs/API_CONTRACT.md` |

---

## 1. Summary

Praxis is a production-minded Autopilot Agent that turns the operational alert firehose into safe, auditable action. It ingests inbound webhooks/alerts (uptime, error tracking, CI/CD, cloud alarms), uses Qwen models via Qwen Cloud with native tool-calling and thinking-mode reasoning to triage and root-cause the incident, drafts a concrete remediation plan, pauses at a human-in-the-loop (HITL) approval checkpoint, and — on approval — executes the fix through registered tools, then records an incident memory so future alerts resolve faster. Backend runs on Alibaba Cloud Function Compute 3.0; agent memory persists in Alibaba Cloud Tablestore vector search. The wedge is the universally-felt "paged at 3am for a problem I've solved before" pain; the demo is a live alert resolving on camera with the full decision trail visible.

## 2. Goals & Non-Goals

### 2.1 Goals
- **G1** — Ingest a real webhook alert and produce a triaged, root-caused remediation plan in under 30 seconds.
- **G2** — Demonstrate sophisticated Qwen API use: function/tool calling, MCP-style tool integration, and thinking-mode reasoning with a visible decision trail.
- **G3** — Enforce a human-in-the-loop approval gate before any state-changing action.
- **G4** — Run the backend on Alibaba Cloud with verifiable proof (FC public URL + code file using Alibaba Cloud SDK/DashScope + short separate recording).
- **G5** — Persist incident memory so a repeat alert is resolved with a cited prior resolution.
- **G6** — Satisfy every submission requirement (OSS repo + license, diagram, ≤3-min video, deployment recording, writeup) with ≥2h buffer.

### 2.2 Non-Goals (hackathon build)
- **NG1** — Not a full enterprise incident-management replacement (no PagerDuty-scale integrations).
- **NG2** — No autonomous destructive action without approval (HITL is mandatory in v1).
- **NG3** — Not multi-tenant SaaS with billing (single-operator demo scope).
- **NG4** — No custom video/multimodal pipelines (that's Track 2).
- **NG5** — Not Elixir/Phoenix for the hackathon runtime (Qwen agent tooling + FC favor Python; BEAM rewrite is post-hackathon).

## 3. Success Metrics

North Star (hackathon): placing in Track 4 — proxied by rubric coverage.

| Tier | Metric | Target |
| --- | --- | --- |
| Requirement | Mandatory submission items complete | 7/7 |
| Stage-1 gate | Fits theme + genuinely uses Qwen APIs | Pass |
| Innovation (30%) | Distinct Qwen features shown (tool-calling, thinking, memory-grounded planning, fallback routing) | ≥3 |
| Technical depth (30%) | Deployed on Alibaba Cloud + non-trivial logic (HITL state machine, idempotency) | Live URL |
| Problem value (25%) | End-to-end real alert → resolved on camera | 1 full loop |
| Presentation (15%) | Video ≤3:00, opens on problem, decision trail visualized | Yes |
| Demo reliability | Successful rehearsal runs | ≥5/5 |
| Cost | Token spend for build + demo | < $5 |

## 4. Target Users / Personas

| Persona | Description | Primary need |
| --- | --- | --- |
| On-call engineer | Solo/small-team dev woken by alerts | Fast triage + a safe, pre-approved fix |
| Platform/DevOps lead | Owns reliability across services | Auditable, human-gated automation; no rogue actions |
| Hackathon judge | Alibaba solution architect / DevRel | Deep Qwen agent use + clean engineering + real impact |
| Indie SaaS operator | Runs agent-native products | Reusable open-source autopilot they'd actually deploy |

## 5. Context

Track 4 rewards "production-readiness over toy demos" and explicitly requires external tool invocation plus human-in-the-loop. Judging is two-stage: Stage 1 pass/fail (fits theme + uses Qwen APIs), then weighted: Innovation & AI Creativity 30%, Technical Depth & Engineering 30%, Problem Value & Impact 25%, Presentation & Documentation 15%. Winner patterns: show-don't-tell, open the video on the pain, visualize the decision trail, production-credible, submit early.

## 6. Scope

### 6.1 In scope (v1, 24h)
Webhook intake endpoint; alert normalization; internally orchestrated Qwen tool-calling agent with thinking mode; tool registry (function calling); remediation-plan generation after signed intake and HITL corrections (no public/manual replan endpoint in v1 per ADR-015); a single-operator bearer-authenticated incident/HITL surface (ADR-025); bounded lifecycle admission on provisioned non-idle FC capacity (ADR-024); tool execution (real for ≥1 safe tool, labeled dry-run for risky ones); incident memory write/read via Tablestore; decision-trail logging; architecture diagram; demo video; deployment-proof recording; OSS repo.

### 6.2 Stretch (only if core green by T0+16h)
Second incident type; additional memory scenarios beyond the required M4 owner-approved persistent write plus distinct-recurrence proof; live model-fallback demonstration.

### 6.3 Post-hackathon
Multi-tenant; Phoenix/BEAM rewrite of the durable core; x402-metered tool marketplace; richer integrations.

## 7. Functional Requirements (EARS)

- **FR-1** — WHEN a webhook alert is received at the intake endpoint, THE SYSTEM SHALL validate its signature/payload and persist a normalized Incident record before responding `202 Accepted`.
- **FR-2** — WHERE a webhook is a duplicate (same idempotency key within the dedup window), THE SYSTEM SHALL return the existing incident and SHALL NOT start a second agent run.
- **FR-3** — WHEN a new incident is created, THE SYSTEM SHALL invoke the Qwen agent with thinking mode enabled to produce a triage classification, a root-cause hypothesis, and a ranked remediation plan.
- **FR-4** — WHILE the agent is reasoning, THE SYSTEM SHALL record the decision trail (thoughts, tool calls, tool results) to the incident timeline.
- **FR-5** — WHEN the agent needs external context or actions, THE SYSTEM SHALL call registered tools via native function calling (MCP-compatible tool schemas).
- **FR-6** — BEFORE executing any state-changing remediation, THE SYSTEM SHALL present the plan at a human-in-the-loop checkpoint and SHALL require explicit approval from the authenticated single-operator role.
- **FR-7** — IF the operator rejects or edits the plan, THEN THE SYSTEM SHALL feed the correction back to the agent and regenerate the plan.
- **FR-8** — WHEN the operator approves a plan, THE SYSTEM SHALL execute the approved tool actions in order and record each result to the incident timeline.
- **FR-9** — WHERE a remediation tool is flagged high-risk, THE SYSTEM SHALL execute against a simulated/dry-run adapter clearly labeled as such in the UI and video.
- **FR-10** — WHEN an incident is resolved, THE SYSTEM SHALL write an IncidentMemory record (embedding + metadata + timestamp) to Tablestore vector search.
- **FR-11** — WHEN a new incident is triaged, THE SYSTEM SHALL query Tablestore for semantically similar past incidents and SHALL surface the top match with its prior resolution to the agent and operator.
- **FR-12** — WHERE the primary Qwen model call encounters an ADR-008-eligible authentication/payment, unavailable-model, quota/rate-limit, 5xx, or timeout failure, THE SYSTEM SHALL fall back to the next (provider, model) pair in the configured chain — Qwen-family models only — and record the fallback in the trail. Other request/transport failures terminate safely without inventing a transition.
- **FR-13** — THE SYSTEM SHALL expose a bearer-protected read endpoint returning the full incident timeline (thoughts, tool calls, approvals, results).
- **FR-14** — WHERE the operator views an incident, THE SYSTEM SHALL render the decision trail in human-readable form within the demo UI.
- **FR-15** — THE SYSTEM SHALL run its backend on Alibaba Cloud and SHALL expose a public HTTP endpoint as deployment proof.

## 8. Non-Functional Requirements

- **NFR-1 (Deployability)** — Backend runs on Alibaba Cloud Function Compute 3.0 (ECS fallback); one-command deploy via Serverless Devs `s.yaml`.
- **NFR-2 (Latency)** — Triage-to-plan p95 < 30s; webhook ack < 500ms; UI updates progressively; post-response triage, correction, execution, and memory work runs through one process-wide job plus three FIFO-pending jobs, with 300s pending expiry and a 240s whole-job deadline (ADR-024).
- **NFR-3 (Reliability)** — Idempotent webhook processing; model fallback; no crash on malformed payloads.
- **NFR-4 (Observability)** — Every agent decision, tool call, and approval logged with incident_id + trace_id.
- **NFR-5 (Security)** — Secrets via env/FC-injected credentials; production startup refuses missing or trivially weak provider, webhook, operator, or isolated-target credentials without echoing their values; incident reads and approval mutations require the application-level single-operator bearer token while root, health, and signed webhook intake retain their separate public boundaries; a shared bounded redaction pipeline removes URL/DSN userinfo, JWT-like tokens, recognized or unknown Authorization payloads, and sensitive assignment/key values—including malformed unterminated quoted forms—from public normalized alert fields, nested tool evidence, and reject/edit correction text before any of them can reach incident reads, the decision trail, persistence, or Qwen prompts; the isolated target requires the same visible-ASCII, secret-safe token policy in production, rejects non-visible-ASCII request tokens before comparison, and a missing local token cannot authorize restart; request-boundary failure logs use fixed events and omit exception text/tracebacks; HMAC webhook signature verification; no destructive action without authenticated approval.
- **NFR-6 (Cost)** — Route routine steps to FAST_MODEL; reserve PRIMARY_MODEL for hard reasoning; total demo spend < $5.
- **NFR-7 (Reproducibility)** — README lets a judge run/inspect it; seed script fires a sample alert.
- **NFR-8 (Licensing)** — Repo public with detectable Apache-2.0 license file.

## 9. System Architecture

See `docs/ARCHITECTURE.md` for the diagram. Components: one provisioned non-idle FC 3.0 controller (FastAPI) plus one provisioned non-idle isolated target, each fixed to single-instance concurrency/capacity; a process-wide 1-running/3-pending FIFO lifecycle controller; agent core (Qwen client with fallback chain, thinking-mode triage, tool registry, executor); Tablestore vector memory; incident state machine (NEW → TRIAGED → AWAITING_APPROVAL → EXECUTING → RESOLVED, with reject/edit corrections cycling to TRIAGED per FR-7 and ADR-014); structured decision-trail log; minimal bearer-unlocked single-page timeline UI; `deploy/alibaba_proof.py` as the submission-required proof file. These are working-tree/configuration claims until the live capacity and authentication probes pass after deployment.

## 10. Qwen / Alibaba Cloud Integration Specifics

See `docs/QWEN_CLOUD_INTEGRATION.md` for exact code. Key decisions: dual-provider routing (ADR-008) over Qwen-family models ONLY — every environment starts with the general Model Studio Qwen Cloud OpenAI-compatible endpoint `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` (compliance-mandatory), then automatically fails over to the same Qwen models on OpenRouter `https://openrouter.ai/api/v1` only after an ADR-008-eligible Qwen Cloud failure. A Qwen coding-plan key is not authorized for application-backend traffic and is never used by Praxis. `PRIMARY_MODEL=qwen3.7-max` was selected at M0 after `qwen3.8-max-preview` failed on both providers, with verified fallbacks `qwen3-max` → `qwen-plus`; ADR-009 maps the fast role to Qwen Cloud `qwen-flash` and OpenRouter `qwen/qwen3.6-flash`; embeddings `text-embedding-v4` at 1024 dims matching the Tablestore vector field; thinking mode enabled for root-cause steps only; system prompt defines SRE persona, safety rules ("never execute state-changing tools without an approved plan"), and a strict JSON plan contract (steps with action, tool, args, risk_level, rollback); top Tablestore memory match injected as grounding context.

## 11. Data Model

- **Incident** — id, source, raw_payload, service, severity, signal, title, idempotency_key, state, created_at.
- **DecisionTrailEntry** — incident_id, seq, type (thought | tool_call | tool_result | approval | fallback | qwen_attempt | execution), content, model_used, tokens, timestamp.
- **RemediationPlan** — incident_id, steps[] (seq, action, tool, args, risk_level, rollback), status, operator_edits.
- **Approval** — incident_id, operator, decision (approve | reject | edit), note, timestamp.
- **IncidentMemory** — id, incident_id, embedding (1024-d Float32), summary, resolution, tags, resolved_at.

Full JSON schemas: `docs/API_CONTRACT.md`.

## 12. API Design

Public boundaries are `GET /`, `GET /healthz`, and signed `POST /webhook`. `GET /incidents`, `GET /incidents/{id}`, `POST /incidents/{id}/approve`, and `GET /incidents/{id}/memory-match` require `Authorization: Bearer <operator token>` (ADR-025). Planning is internally orchestrated after signed intake and authenticated HITL corrections; ADR-015 excludes a public/manual replan trigger from v1. Full contract: `docs/API_CONTRACT.md`.

## 13. UX & Demo Flow

1. The public root shows a locked operator view; the human enters the operator token locally, which is retained in JavaScript memory only and discarded on reload/navigation.
2. A real alert fires (seed script) → incident appears as NEW.
3. Agent triages with thinking mode; decision trail streams (thoughts, tool calls fetching logs, cited prior incident from memory).
4. Remediation plan appears with per-step risk badges → AWAITING_APPROVAL.
5. Operator clicks Approve → agent executes (real restart for the safe tool, labeled dry-run for risky) → RESOLVED.
6. Only after the owner-gated M4 proof succeeds: the stored row is followed by a distinct recurrence (fresh idempotency key) → the agent cites the prior resolution. A same-key replay proves FR-2 deduplication, not memory.

## 14. Demo Video Script (≤3:00)

See `docs/DEMO_AND_SUBMISSION.md` for the timed shot list.

## 15. Build Milestones

See `docs/BUILD_PLAN.md` for the hour-by-hour checklists (M0–M7) with exit criteria and skip rules.

## 16. Judging-Criteria Mapping

| Criterion (weight) | How Praxis scores |
| --- | --- |
| Innovation & AI Creativity (30%) | Native tool-calling + thinking-mode reasoning + memory-grounded planning + model fallback = sophisticated Qwen Cloud API use. |
| Technical Depth & Engineering (30%) | Alibaba Cloud FC deployment; provisioned non-idle lifecycle configuration with bounded process admission/deadlines; bearer-authenticated HITL state machine; idempotent webhook middleware; Tablestore vector recall; structured observability. |
| Problem Value & Impact (25%) | Universally-felt on-call pain; a code-enforced approval-record gate behind an authenticated single-operator role; obvious open-source adoption path. |
| Presentation & Documentation (15%) | 3-min problem-first video; live decision trail; architecture diagram; thorough README + build blog. |

## 17. Risks & Mitigations + Open Questions

| Risk | Impact | Mitigation |
| --- | --- | --- |
| FC provisioning/KYC friction eats hours | No deployment proof | Time-box to T0+3h; ECS fallback; proof = any Alibaba Cloud service in a code file |
| Quota exhausted (voucher window closed) | Can't call Qwen | Free-trial quota; route cheap models; cap tokens |
| Tablestore blocked / Memory Store region-limited | Persistent memory proof blocked | Use the accepted generic KNN vector search in the International region; `MEMORY_BACKEND=inmem` preserves degraded local operation but does not satisfy M4, which remains open until the owner-approved persistent write plus distinct-recurrence proof passes |
| Live tool execution risky on camera | Demo breaks / unsafe | Real exec for one safe tool; labeled dry-run for risky; HITL gate |
| Model/tool-call quirks via compatible-mode | Agent misbehaves | Test raw calls first at M0; strict JSON contract; fallback chain |
| `qwen3.8-max-preview` unavailable via API (confirmed at M0: Qwen Cloud 404, OpenRouter 400) | Primary model fails | ADR-005 chain activated: qwen3.7-max → qwen3-max → qwen-plus, verified on both providers |
| SA payment method fails Alibaba binding (3DS/pre-auth/risk controls) | No Alibaba account → no deployment, no Qwen Cloud API | Resolved at M0: account/payment and a real FC deployment were confirmed. ADR-008 keeps Qwen Cloud first in every environment with same-Qwen OpenRouter fallback. Alibaba deployment has no non-Alibaba fallback; ADR-001's FC→ECS contingency remained available but was not triggered because FC succeeded. |

**Open Questions:** none. OQ-3 was resolved by owner direction on 2026-07-21: retain memory in v1 and complete the accepted ADR-004 Tablestore implementation with the in-memory fallback behind the same interface.

**Resolved at M0:** OQ-1 — Function Compute 3.0 is the final runtime host; a real `praxis-api` deployment and public health response succeeded, so ADR-001's ECS fallback was not triggered. OQ-2 — the demo alert source is the deterministic Sentry-style seed script in `scripts/fire_alert.py`, including its repeat mode; no live Sentry integration is in hackathon scope. OQ-5 — `qwen3.8-max-preview` is unavailable on both providers; the working primary/fallback chains are recorded in `.env` and `docs/QWEN_CLOUD_INTEGRATION.md`. OQ-6 — Alibaba account/payment access is active, a paid plan was purchased, and a real Function Compute deployment succeeded.

**Resolved by owner:** OQ-3 — retain persistent Tablestore vector memory in v1, with `MEMORY_BACKEND=inmem` as the accepted operational fallback and no completion claim until an owner-approved resolved-row write plus a distinct-recurrence recall proof passes. OQ-4 — execute the single real restart against a dedicated isolated Function Compute demo target. Accepted ADR-010 binds that safety boundary. The proposed optional build-blog scope cut was rejected in ADR-011, so the blog remains an M6 deliverable.

## 18. Architecture Decision Records

Thirty ADRs in MADR 4.0 format live in `docs/decisions/`: ADR-001 runtime host, ADR-002 language/runtime, ADR-003 track selection, ADR-004 memory store, ADR-005 model routing, ADR-006 safety model, superseded ADR-007 provider routing, accepted ADR-008 Qwen-Cloud-first routing, accepted ADR-009 provider-specific fast-model routing, accepted ADR-010 isolated FC remediation target, rejected ADR-011 optional-blog scope cut, accepted ADR-012 webhook request-body limit, accepted ADR-013 bounded provider-attempt deadlines, accepted ADR-014 reject/edit correction semantics, accepted ADR-015 removal of the public/manual replan trigger, proposed ADR-016 managed FC custom-domain certificate lifecycle, proposed ADR-017 runtime/development dependency separation, proposed ADR-018 bounded superseded-plan correction context, proposed ADR-019 execution-grounded IncidentMemory, proposed ADR-020 durable asynchronous memory-write delivery, proposed ADR-021 model/plan/embedding resource bounds, proposed ADR-022 bounded automatic agent recovery, proposed ADR-023 bounded process-local retention/pagination, accepted ADR-024 FC-supported post-response lifecycle, accepted ADR-025 operator-surface authentication, proposed ADR-026 signed idempotency identity, proposed ADR-027 durable incident/idempotency authority, accepted ADR-028 uncertain tool-outcome reconciliation, accepted ADR-029 least-privilege demo-recorder access, and proposed ADR-030 normalized and bounded webhook idempotency keys. Accepted ADRs are binding; proposed ADRs change nothing until the owner accepts them.
