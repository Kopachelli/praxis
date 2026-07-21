# BUILD_PLAN.md — Praxis (24h to submission)

**Rules:** Work top-to-bottom. Do not start M(n+1) until M(n) exit criteria are green or the human explicitly approves skipping. Owner-authorized parallel work may prepare later-milestone artifacts, but the formal current milestone remains the earliest unmet exit and no later milestone closes merely because readiness work started. Tick checkboxes as you complete tasks and mirror every state change to Linear (one issue per milestone, checklist copied into the issue description). T0 = session start. Hard stop = **2026-07-20 14:00 PDT**; internal cutoff **12:00 PDT**.

---

## M0 — Environment lock (T0 → T0+1h) — Linear: `M0`

- [x] **Account/payment check (OQ-6, timebox 45 min, runs in parallel with everything below):** Alibaba Cloud account active? Payment method bound? If the SA card fails the $1 pre-auth/3DS → bind **PayPal** per the payment playbook in `docs/QWEN_CLOUD_INTEGRATION.md`; if still blocked → **stop-the-line**: open Alibaba support ticket, escalate to Khristian, continue all build work locally against OpenRouter (deployment has no non-Alibaba fallback, but code does).
- [x] **Provider verification matrix (OQ-5):** one chat completion per provider — Qwen Cloud (`qwen3.8-max-preview`, else walk `qwen3.7-max` → `qwen3-max` → `qwen-plus`) AND OpenRouter (`qwen/qwen3.7-max` chain). Record the working (provider, model) pairs in `.env` + worklog. Per ADR-008, set local `PROVIDER_ORDER=qwencloud,openrouter`; OpenRouter is fallback only.
- [x] Scaffold FastAPI app (`app/main.py`, `app/config.py`) + `GET /healthz`
- [x] `requirements.txt` (fastapi, uvicorn, openai, httpx, pydantic, tablestore, pytest)
- [x] Provision FC 3.0 web function (HTTP trigger) via `deploy/s.yaml` with `PROVIDER_ORDER=qwencloud,openrouter`; `s deploy`; public `*.fcapp.run` URL returns 200 on `/healthz`
- [x] **GO/NO-GO:** FC blocked >45 min (account is fine but FC provisioning fails) → switch to ECS (ADR-001 fallback), note in worklog + Linear (OQ-1)

**Exit:** deployed `/healthz` 200 + one successful Qwen completion from the deployed function via the **Qwen Cloud** path (compliance), + OpenRouter fallback verified locally.

## M1 — Webhook intake + incident core (T0+1h → 5h) — Linear: `M1`

- [x] `POST /webhook`: HMAC-SHA256 signature verification [FR-1]
- [x] Payload normalization → Incident record (service, severity, signal, title) [FR-1]
- [x] Idempotency-key dedup window; duplicate returns existing incident, no second agent run [FR-2]
- [x] Incident state machine NEW → TRIAGED → AWAITING_APPROVAL → EXECUTING → RESOLVED; reject/edit corrections return to TRIAGED per ADR-014 and `API_CONTRACT.md`
- [x] Decision-trail store + `GET /incidents/{id}` returning full timeline [FR-4, FR-13]
- [x] `GET /incidents` returns normalized incident summaries newest-first [FR-13]
- [x] `scripts/fire_alert.py` seeds a realistic Sentry-style alert (+ `--repeat` flag)
- [x] Tests: FR-1 happy path, FR-2 duplicate

**Exit:** firing the script twice yields exactly one incident; timeline endpoint returns the normalized record.

## M2 — Qwen agent: triage + plan (5h → 10h) — Linear: `M2`

- [x] `app/agent/client.py`: Qwen client with fallback chain + timeout + trail entry on fallback [FR-12]
- [x] Thinking-mode triage: classification + root-cause hypothesis, thoughts recorded to trail [FR-3, FR-4]
- [x] Tool registry with ≥1 read tool (fetch logs / service status) wired via function calling [FR-5]
- [x] Strict internally orchestrated JSON plan contract: `steps[{seq, action, tool, args, risk_level, rollback}]` — validated with pydantic; no public/manual replan trigger in v1 (ADR-015)
- [x] Background task: webhook ack < 500ms, agent runs async [NFR-2]

**Exit:** fired alert reaches AWAITING_APPROVAL with a coherent plan and a visible reasoning trail.

## M3 — HITL + execution + UI (10h → 14h) — Linear: `M3`

- [x] `POST /incidents/{id}/approve` handling approve | reject | edit; reject/edit regenerates plan [FR-6, FR-7]
- [x] Executor runs approved steps in order, records each result to trail [FR-8]
- [x] ONE real safe tool (restart a dedicated isolated FC demo target) + labeled dry-run adapters for risky tools [FR-9] (OQ-4; ADR-010 accepted)
- [x] `ui/index.html`: timeline view + Approve/Reject buttons, polls `GET /incidents/{id}` [FR-14]
- [x] Tests: FR-6 gate blocks execution pre-approval; FR-8 executes post-approval
- [ ] **OWNER-RETAINED EXIT PROOF:** after an owner-authorized hardened deployment, a fresh alert reaches `AWAITING_APPROVAL`, Khristian explicitly approves that exact plan, and the isolated target action produces one strict `RESOLVED` trail + UI sequence [FR-6, FR-8]

**Exit:** full camera-quality loop: alert → plan → approve → resolved.

## M4 — Memory (stretch-core) (14h → 17h) — Linear: `M4`

- [x] Tablestore table + search index with 1024-d Float32 vector field (ADR-004)
- [x] On resolve: embed summary via `text-embedding-v4` → write IncidentMemory [FR-10]
- [x] On triage: `KnnVectorQuery` recall; inject top match into prompt; expose `GET /incidents/{id}/memory-match` [FR-11]
- [ ] **OWNER-RETAINED EXIT PROOF:** after explicit approval, the deployed resolver writes IncidentMemory to Tablestore; a distinct `--recurrence` alert (not `--repeat` dedup) cites the prior resolution in trail + UI [FR-10, FR-11]

**Exit:** after the owner-approved write, a distinct `--recurrence` alert (not `--repeat` dedup) cites the prior resolution in trail + UI.

## M5 — Hardening + proof (17h → 20h) — Linear: `M5`

- [x] Production router verified under a controlled accepted Qwen Cloud failure → exact ADR-005 chain reaches live same-Qwen OpenRouter and records API-contract `{from,to,reason}` fallback entries [FR-12]
- [x] `deploy/alibaba_proof.py` finalized (Alibaba Cloud SDK + DashScope usage in one file) — **submission requirement** [FR-15]
- [x] Architecture diagram exported to PNG from `ARCHITECTURE.md` mermaid
- [x] `README.md`: run / deploy / inspect instructions [NFR-7]; `LICENSE` (Apache-2.0) at repo root [NFR-8]
- [ ] Record the short **separate** deployment-proof screen capture (console + live Function Compute URL)

## M6 — Demo + writeup (20h → 23h) — Linear: `M6`

- [ ] Rehearse the demo flow 5× against the DEPLOYED backend (not localhost)
- [ ] Record ≤3:00 video per `DEMO_AND_SUBMISSION.md`; upload public (YouTube unlisted ≠ public — set Public)
- [ ] Devpost writeup mapped explicitly to the four judging criteria; all links checked
- [ ] Publish the 800–1200 word build blog post with 3 screenshots, repository link, and public video link (Blog Post Award; ADR-011 rejected)

## M7 — Submit (23h → 24h) — Linear: `M7`

- [ ] Repo public; license detectable; final push; tag `v0.1-hackathon`
- [ ] Submit on Devpost **≥2h before 14:00 PDT**
- [ ] Screenshot confirmation; move Linear project to Done; closing worklog entry
