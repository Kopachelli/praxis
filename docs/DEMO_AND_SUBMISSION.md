# DEMO_AND_SUBMISSION.md — Praxis

> **LOCAL POST-DEADLINE REVISION — DO NOT SHIP:** Praxis is submitted and the Qwen hackathon is in judging. Official announcement 45369 prohibits post-deadline repository commits, video replacement, submission edits, and updates to linked materials. Do **not** upload, paste, publish, commit, push, replace a video, edit Devpost, or update any linked material from this file until the organizer grants written permission or judging ends. If these materials are later used, label every newer recording, screenshot, article, and proof asset as **created post-deadline**; never present it as part of the judged submission snapshot.

## Evidence boundary for every script and caption

**Shipped and previously verified:** the deployed Alibaba Function Compute controller is configured for the Tablestore memory backend; the 1024-dimensional Float32/cosine table and search index exist; Qwen Cloud `text-embedding-v4` produces the required embeddings; the controller has a dedicated table-scoped execution role; and prior read-only checks from the live FC instance verified access to the Tablestore schema/API. The hardened `deploy/alibaba_proof.py` now also requires an exact Qwen sentinel, complete schema, and matching live FC health markers; its final allowlisted live result must be recaptured after the owner/freeze gate permits it.

**Still unproven until the M4 exit criterion passes:** a fresh resolution created only after explicit human approval must write its real row, and a **distinct recurrence** must retrieve that row as a semantic memory match during triage. Do not call schema access, a proof-file read, in-memory tests, or `--repeat` deduplication proof of that end-to-end behavior.

**Current runtime limits:** the linked public revision is older than the recording-ready working tree. Accepted ADR-024/025 are implemented locally: both FC functions are configured for one provisioned non-idle instance, the controller admits one running plus three FIFO-pending jobs under 300s/240s pending/job deadlines, and incident reads/decisions require the single-operator bearer token. None of that is live proof until the permitted deploy passes the capacity verifier and protected-route probes through both origins. Same-key deduplication and active incidents remain process-local; the idempotency header is not yet body-signature-bound or independently normalized/bounded; and memory delivery is one best-effort, plan-derived attempt. A post-dispatch audit failure no longer leaves a real tool outcome silently uncertain: accepted ADR-028 records a durable pre-dispatch intent and fails closed into the terminal RECONCILIATION_REQUIRED state instead of a false success or blind retry. A separate read-only viewer role (accepted ADR-029) now exists for a recorder/judge who needs to watch without mutation authority. ADR-019/020 and ADR-026/027 remain proposed and unimplemented, and idempotency-boundary ADR-030 is separately proposed and unimplemented. Do not narrate unverified deployment state as a production guarantee.

## Deployed recording preflight (read-only)

**Hard gate:** do not run a live rehearsal, trigger an alert, expose an approval candidate, approve, record, deploy, or replace any linked asset during the judging freeze or while the owner is unavailable. After written organizer permission or the end of judging, the owner must authorize the deploy and choose a protected approval path before the steps below become executable instructions. The marker GET alone is read-only; it does not grant that authorization.

## Owner-return execution order

Use this as the shortest safe path from the current local tree to final evidence.
Stop at the first unmet gate; never substitute a stale incident, a duplicate
replay, or an unprotected approval surface.

1. **Clear the publication gate.** Record written organizer permission or verify
   that judging has ended. If neither is true, keep every deploy, recording,
   repository, video, blog, and Devpost action local and unpublished.
2. **Confirm the accepted recording decisions and secret boundary.** Choose the
   deployment-wrapper `.env` precedence in `PRAXIS-116`; ADR-024, ADR-025,
   ADR-028, and ADR-029 are accepted and implemented in the working tree. Ensure
   the ignored `.env` has a strong `PRAXIS_OPERATOR_TOKEN` and a distinct
   `PRAXIS_VIEWER_TOKEN` (the process refuses to start if they are equal), both
   without printing them. The accepted ADR-029 read-only viewer token is what a
   judge or recorder uses to watch the dashboard without any credential or
   mutation authority; the owner still performs every approval with the operator
   token. Do not implement any remaining proposal until accepted.
3. **Build the accepted delta locally.** Implement only accepted decisions, run
   the complete regression/security gate, and inspect the exact release diff.
4. **Verify and deploy the recording revision.** Use the routine controller and
   isolated-target verify/deploy sequence in `README.md`; bootstrap the target
   only if it is proven absent. Do not change the existing Cloudflare DNS route.
   After deployment, run the read-only `python scripts/fc_role.py inspect` and
   require its exact role/policy attachment readiness, then run
   `python scripts/probe_fc.py` and require `configuration_matches=true`,
   `active_capacity_matches=true`, and a valid bounded RFC3339 modification
   timestamp. Verify the same fixed unauthenticated `401` challenge and a
   successful token-authenticated incident read through the native FC URL and
   custom domain without printing the token or bodies. Either failure stops the
   release; do not infer readiness from a successful deploy command alone.
5. **Pass the public marker preflight below.** A failure means the public root is
   stale; stop instead of recording around it.
6. **Close M3 with one fresh owner-controlled loop.** Run the matching
   secret-free `--fresh --preflight` first, then fire `--fresh`; require the
   complete `202`/`NEW`/`duplicate:false` response contract, a UUID trace ID
   matching `X-Trace-Id`, a non-empty incident ID, and the same ID in the UI.
   Capture `AWAITING_APPROVAL`; Khristian reviews that exact plan, clicks
   **Approve plan**, and separately accepts the native confirmation. Capture the
   same incident in strict `RESOLVED` state with differing target boot IDs.
7. **Close M4 from that approved resolution.** Verify its Tablestore write, run
   the matching secret-free `--recurrence --preflight`, then fire one distinct
   `--recurrence` alert. Capture the earlier incident ID and bounded similarity
   in the memory card. `--repeat` does not count.
8. **Close M5 evidence.** Run the hardened standalone Alibaba proof, retain only
   its allowlisted success result, and record the separate 30–60 second console,
   custom-domain, health, and proof-source clip.
9. **Finish M6.** Rehearse five deployed `--fresh` loops under owner control,
   record the human approval clip, let DemoSmith navigate only the exact resolved
   incident read-only, assemble the ≤3:00 video, and capture the three final
   screenshots. Export the five distinct incident-detail responses into the
   NFR-2 evidence envelope described below and require its p95 check to pass.
   Replace publication placeholders only with verified public URLs.
10. **Publish only after the gate in step 1 is clear.** Resolve the owner legal
    attestations, `PRAXIS-115` (`AGENTS.md` publication), and `PRAXIS-113`
    (truthful missed-internal-buffer closure); then use `docs/PUBLICATION_MANIFEST.md`,
    verify the public default branch anonymously, and read back every saved link.

Before any rehearsal or camera take, verify that the public root is serving the recording-ready UI revision. This command performs one read-only GET, prints no page content, and requires the targeted-announcement and memory-similarity feature markers used by the shot list:

```bash
python scripts/check_recording_ui.py --url https://praxis.kopachelli.dev
```

If it does not exit zero and print exactly `{"ok":true,"reason":"recording_ui_ready","source":"url"}`, stop: the linked deployment is stale and must not be recorded as though it contains the current evidence UI. The checker validates an HTML response plus both required feature markers without printing the target, response, headers, or exception details. Running `python scripts/check_recording_ui.py` with no arguments checks the local `ui/index.html` offline. For each of the five rehearsals and the final take, use `python scripts/fire_alert.py --base-url https://praxis.kopachelli.dev --fresh`. Require the single allowlisted result to contain a non-empty `incident_id`, `"status_code":202`, and `"duplicate":false`, then immediately confirm that exact incident ID appears in the browser before reviewing or recording its plan. Any `200`, duplicate result, missing ID, or mismatched UI incident means the take is not fresh; stop without approving it. The fixed default key and `--repeat` remain deterministic deduplication tools, while `--recurrence` remains the explicit M4 memory-proof mode.

Immediately before each fresh live send, run its no-network preflight:

```bash
python scripts/fire_alert.py --base-url https://praxis.kopachelli.dev --fresh --preflight
```

It must return exactly five fields: `mode`, `webhook_url`, `request_count`,
`body_bytes`, and `body_sha256`, with `mode` equal to `fresh`, the normalized
HTTPS webhook URL, and `request_count` equal to `1`. It validates signing-secret
presence without printing or signing with it, never constructs an HTTP client,
and never emits a secret, signature, or idempotency key. The
live `--fresh`/`--recurrence` command itself exits nonzero unless the server
returns the complete accepted-webhook contract: `202`, `state: NEW`,
`duplicate: false`, a non-empty incident ID, and a valid UUID `trace_id` exactly
matching the `X-Trace-Id` response header.

### NFR-2 triage-to-plan evidence

For each of the five owner-authorized deployed rehearsals, retain the full
`GET /incidents/{id}` response for the exact fresh incident. Wrap those five
distinct server responses as `{"incidents":[...]}` in a local JSON file, then
run this no-network verifier:

```bash
python scripts/check_plan_latency.py --input <local-evidence.json>
```

The verifier defines plan readiness as the timestamp of the server-created
`thought` event whose content is exactly
`{"stage":"plan_ready","status":"ready","trace_id":"<32 lowercase hex>"}`.
It subtracts the same incident's server-owned `created_at`, calculates the
nearest-rank p95 across 5–50 unique plan-ready incidents, and passes only when
the result is strictly below 30 seconds. A passing five-sample rehearsal prints
only a fixed envelope such as
`{"ok":true,"p95_seconds":12.345,"reason":"within_target","sample_count":5,"target_seconds":30.0}`.
Malformed, mixed-incident, duplicate, oversized, or pre-plan evidence fails
closed without echoing the file path or contents. The local verifier validates
the exported structure and timing calculation; it does not cryptographically
attest that a local file came from the deployed service. The owner must retain
the five exact deployed responses and their rehearsal evidence chain. Local
tests prove the measurement logic only; NFR-2 remains unproven until those five
distinct deployed samples pass after the owner and judging gates clear.

## Demo video script (hard cap 3:00, target 2:50)

| Time | Shot | Script / action |
| --- | --- | --- |
| 0:00–0:18 | Face or title card | "Every on-call engineer knows the 3am page for a problem they've already solved. Praxis is a Qwen Cloud autopilot that triages an alert, proposes a fix, and stops for human approval before anything changes." |
| 0:18–0:32 | Terminal + browser split | After the matching no-network `--fresh --preflight` above succeeds, run `python scripts/fire_alert.py --base-url https://praxis.kopachelli.dev --fresh`. Keep the same URL visible in the browser. The explicit origin prevents an accidental localhost recording; `--fresh` gives this take a unique identity. Show only the allowlisted `202` / `duplicate: false` result and returned incident ID, then confirm that exact ID appears in the timeline. |
| 0:32–0:52 | Human-recorded UI clip | Off camera, Khristian unlocks this tab with the operator token; the value remains in page memory and is never shown. Show `AWAITING_APPROVAL`; Khristian inspects the exact plan, clicks **Approve plan**, and then accepts the browser's native confirmation as a separate second click. Those visible owner actions cross the authenticated HITL boundary. Cut the wait, then show the isolated `praxis-demo-target` restart evidence and strict `RESOLVED` state. DemoSmith must never receive the operator token or perform either state-changing click; when it drives the dashboard it uses only the accepted ADR-029 read-only viewer token, which the server rejects at the approve boundary. |
| 0:52–1:57 or 2:17 | DemoSmith read-only UI | Show only trail events and plan steps that the deployed run actually produced: Qwen provider/model labels, root-cause reasoning, any read-tool calls, the exact risk badge, approval, execution evidence, and `RESOLVED`. Do not narrate a caution/dangerous step unless it exists in that run. |
| 1:57–2:17 | Conditional memory beat | Use this 20-second slot only after M4's deployed write-and-recall exit proof is green: show a distinct recurrence and the visible memory card naming the earlier incident ID with its bounded Tablestore similarity percentage. If M4 is still open, keep the DemoSmith walkthrough through 2:17 instead. Never present `--repeat` deduplication as semantic memory. |
| 2:17–2:42 | Architecture diagram | Show a regenerated `docs/assets/praxis-architecture.png` as a logical summary. It is accurate to say that ADR-024's active-capacity/admission design, ADR-025's operator boundary, ADR-028's uncertain-outcome reconciliation, and ADR-029's read-only viewer role are implemented in the working tree but count as live-proven only after their deployed probes pass; the diagram itself is not proof. Do not imply that proposed ADR-019/020 or ADR-026/027 are implemented. Idempotency parsing/bounds remain separately proposed and unimplemented in ADR-030. If M4 is still open, say plainly that the owner-approved resolved-row write and distinct-recurrence recall have not yet been demonstrated. |
| 2:42–2:50 | Outro | End with Apache-2.0, Alibaba Function Compute, `https://praxis.kopachelli.dev`, and `https://github.com/Kopachelli/praxis`. |

**Recording rules:** these apply only after the hard gate above is cleared. Use the permitted deployed backend, never localhost: `https://praxis.kopachelli.dev`, 1080p+, readable font sizes, and cursor highlights. Rehearse 5× (M6), using `--fresh` and proving a new returned/UI incident identity every time. The owner unlocks the exact recording tab off camera; never display, narrate, paste into a recorder form, or persist the operator token. The owner approval take must visibly capture both deliberate actions: click **Approve plan**, then click **OK/Confirm** in the native browser confirmation dialog. Keep one full-loop screen recording as backup B-roll before attempting the "perfect take". DemoSmith may navigate the already-unlocked page read-only but must never receive the token, approve, confirm, reject, edit, submit, trigger, or invoke a state-changing control.

## Final three-shot evidence list

Capture these from one fresh deployed sequence only after the owner returns, the freeze gate is cleared, the hardened revision passes marker, active-capacity, and both-origin authentication preflights, and the owner has unlocked the recording tab off camera:

1. **Safety gate:** a fresh incident in `AWAITING_APPROVAL`, with Qwen provider/model, root-cause hypothesis, risk, rollback, exact plan, and the visible Approve/Reject controls. The following human-only take must show both the **Approve plan** click and the separate native confirmation click. Structured edit correction is supported by the API but is not a browser control or required camera beat.
2. **Approved resolution:** that same incident in `RESOLVED` only after explicit human approval, with the restart attempt/result and differing isolated-target boot IDs visible in the trail.
3. **Incident memory:** a distinct recurrence whose memory card names the earlier incident ID and displays its bounded Tablestore similarity percentage. A duplicate webhook replay is not recurrence proof.

The currently captured empty dashboard is a stale-public-revision reference and must not be used as fresh readiness evidence. The `/healthz` capture needs a readable recapture with the canonical URL visible. Three current-interface assets now exist at `docs/screenshots/submission/03-local-awaiting-approval.png`, `04-local-approved-resolution.png`, and `05-local-memory-recurrence.png`; each says **LOCAL FIXTURE** and **NOT LIVE PROOF**. They are useful seeded illustrations for draft layout, but they do not satisfy the deployed M3/M4 evidence list. `docs/assets/praxis-cover.png` is presentation art, while the architecture and sequence PNGs are logical references. If shot 2 or 3 cannot be captured, use the cover, architecture summary, and deployment proof as clearly labeled post-deadline substitutes and do not imply that the absent evidence exists.

## Deployment-proof recording (SEPARATE clip, required by rules)

30–60s screen capture after the owner-authorized strict proof rerun: Alibaba Cloud console showing both FC functions, one active provisioned instance for each, idle CPU allocation disabled, reserved concurrency one, and the native custom-domain binding → `https://praxis.kopachelli.dev/healthz` returning `{"deployed_on": "alibaba-fc"}` → the finalized `deploy/alibaba_proof.py` source and its allowlisted success result. No narration needed. Do not expose environment values, credentials, account identifiers, raw provider responses, or temporary role tokens.

## Local Devpost readiness checklist

This checklist prepares a possible future correction; it does not authorize a change to the frozen submission.

- [ ] `https://github.com/Kopachelli/praxis` is public and Apache-2.0 LICENSE is detectable at root
- [ ] Link to `deploy/alibaba_proof.py` (code file demonstrating Alibaba Cloud services/APIs)
- [ ] Deployment-proof recording uploaded (separate from demo video)
- [ ] Demo video ≤3:00, set to **Public** on YouTube/Vimeo
- [ ] Architecture diagram image attached
- [ ] Track selected: **Track 4 — Autopilot Agent**
- [ ] Text description mapped to the four judging criteria (use §16 of PRD verbatim as scaffold)
- [ ] Team: solo — Khristian Kopachelli
- [ ] Required by the retained scope: published blog post URL added (ADR-011 rejected the proposed blog cut)

## Blog post outline (Blog Post Award — prepare locally during M6 render time)

1. The 3am problem · 2. Why an autopilot needs a human gate · 3. What Qwen made easy (thinking mode, tool calling, same-Qwen fallback chain) · 4. Building on Function Compute 3.0 as a solo developer · 5. Tablestore in two layers: shipped infrastructure/read proof, then the recurrence moment only if M4's human-gated write-and-recall exit proof is green · 6. What remains before production adoption. Target 800–1200 words, the three verified evidence shots above, `https://github.com/Kopachelli/praxis`, and the eventual public video link. Keep the draft local during the judging freeze, label any later publication post-deadline, and do not introduce an unaccepted future stack or marketplace scope.
