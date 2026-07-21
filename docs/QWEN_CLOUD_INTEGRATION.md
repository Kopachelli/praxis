# QWEN_CLOUD_INTEGRATION.md — Model & Provider Integration (Praxis)

Qwen Cloud (primary in every environment) + OpenRouter (fallback only). Provider order was accepted in ADR-008 and provider-specific fast-role mapping in ADR-009 on 2026-07-20; model pairs were verified at M0.

## ⚖️ Compliance box — read first

The hackathon rules state: projects **must use the Qwen Cloud API** and **must be deployed on Alibaba Cloud infrastructure**; the submission must link a code file in the repo demonstrating Alibaba Cloud services/APIs; judging rewards sophisticated use of Qwen Cloud APIs.

Consequences for Praxis:
1. **OpenRouter alone is NOT compliant** — it serves the same Qwen models (and for `qwen/qwen3.7-max` actually routes to Alibaba Cloud Int. as the sole provider), but it is not "the Qwen Cloud API". It is a resilience layer only.
2. The **primary path in the deployed app and in the demo video must be Qwen Cloud** (`dashscope-intl`), and `deploy/alibaba_proof.py` must show DashScope + Alibaba SDK usage.
3. The **Alibaba Cloud deployment requirement is untouched** by any provider choice: it has no non-Alibaba fallback. Function Compute is the final selected host. `MEMORY_BACKEND=inmem` is only an operational storage degradation path and does not satisfy the retained M4 Tablestore proof.
4. **Only Qwen-family models, ever, on both providers.** No GPT/Claude in `app/`.

## Provider matrix

| | **Qwen Cloud (primary)** | **OpenRouter (fallback)** |
| --- | --- | --- |
| Base URL | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `https://openrouter.ai/api/v1` |
| Auth | `DASHSCOPE_API_KEY` (Model Studio, Intl/Singapore workspace) | `OPENROUTER_API_KEY` (existing funded account) |
| Model IDs | `qwen3.7-max`, `qwen3-max`, `qwen-plus` (M0 verified); `qwen3.8-max-preview` unavailable (404) | `qwen/qwen3.7-max`, `qwen/qwen3-max`, `qwen/qwen-plus` (M0 verified); `qwen/qwen3.8-max-preview` unavailable (400) |
| Fast role (ADR-009) | `FAST_MODEL=qwen-flash` | `OPENROUTER_FAST_MODEL=qwen/qwen3.6-flash` |
| Thinking mode | `extra_body={"enable_thinking": true}` (native DashScope param) | unified `extra_body={"reasoning": {"enabled": true}}` |
| Function calling | OpenAI-compatible `tools` | OpenAI-compatible `tools` |
| Embeddings | `text-embedding-v4` @1024 ✅ | ❌ not proxied — embeddings are Qwen-Cloud-only |
| Payment | general Model Studio quota/billing; Alibaba account/payment confirmed at M0 | existing balance — SA-payment-safe |
| Role | local + deployed + demo primary; embeddings | same-Qwen runtime fallback only |

### M0 verification outcome — 2026-07-19

| Provider | `3.8 preview` | `3.7 max` | `3 max` | `plus` |
| --- | --- | --- | --- | --- |
| Qwen Cloud | Failed: HTTP 404 | Working | Working | Working |
| OpenRouter | Failed: HTTP 400 | Working | Working | Working |

Praxis therefore selects `PRIMARY_MODEL=qwen3.7-max` and records only the three working pairs per provider in `.env`, exactly following ADR-005's accepted fallback. ADR-008 now requires `qwencloud,openrouter` locally and when deployed.

ADR-009 preserves the same provider order for routine classification while mapping the fast role to live identifiers: `qwen-flash` on Qwen Cloud and `qwen/qwen3.6-flash` on OpenRouter. Production routing for that role is implemented in M2, not M0.

M0 verifies **fallback readiness** separately with `python scripts/verify_failover.py`. This test-only harness requires `PROVIDER_ORDER=qwencloud,openrouter`, records a controlled local Qwen Cloud failure without constructing a primary client or making a primary network request, then calls the first configured OpenRouter Qwen model and requires the exact `PRAXIS_M0_FAILOVER_OK` sentinel. Its output is allowlisted JSON only. The result proves ordered configuration plus live same-Qwen fallback availability; it does **not** claim that the production router exists yet. Production transition logic and its end-to-end proof remain M2/M5 work. Run this M0 live call only with a non-compromised OpenRouter credential.

M5 verifies the **production router** with `python scripts/verify_runtime_failover.py`. The verifier loads the normal accepted settings, copies them in memory, replaces only the copied Qwen Cloud credential with a fixed public invalid sentinel, and calls the real `QwenClient` reasoning route with a `DecisionTrailStore`. It requires the exact ADR-005 Qwen Cloud chain to emit accepted authentication transitions before the live same-Qwen OpenRouter model succeeds. Success output is limited to provider, model, and ordered `{from,to,reason}` entries; failure uses one fixed envelope. It never edits `.env`, deployment configuration, provider order, or either model chain, and it never prints credentials, provider bodies, model output, usage, account identifiers, or exception text.

### Coding-plan credentials are not runtime credentials

Alibaba's Qwen coding plans use dedicated `sk-sp-...` keys and a coding endpoint for supported interactive coding agents. Official terms prohibit those keys in automated scripts and application backends. Praxis therefore uses only a general Model Studio key (`sk-ws-...` or a still-valid legacy `sk-...`) with the key's Singapore API Host. Coding-plan discounts and model entitlements are not counted as Praxis runtime capacity and do not prove that a model ID is available to the application.

## PROVIDER_ORDER semantics

- **Local dev:** `PROVIDER_ORDER=qwencloud,openrouter` — continuously exercise the compliance-critical path; OpenRouter is fallback only.
- **Deployed / demo / rehearsals:** `PROVIDER_ORDER=qwencloud,openrouter` — identical routing for production parity.
- Failover triggers: HTTP 401/402/403 (auth/payment), 404 unknown model, 408/request timeout, 429 (quota), and 5xx. Every transition writes the API-contract `fallback` trail entry `{from, to, reason}` with fully qualified provider/model pairs, and one terminal `qwen_attempt` entry closes the logical call at the successful or final failed pair (FR-4, FR-12) — resilience becomes a demoable feature.
- Embeddings do NOT fail over: `text-embedding-v4` remains Qwen-Cloud-only. If it is temporarily unavailable, triage continues through the bounded best-effort memory path and M4 remains open; do not cut the owner-retained memory scope or claim completion. `MEMORY_BACKEND=inmem` is the accepted storage fallback behind the same interface, not evidence of persistent Tablestore recall, and it cannot replace the Qwen Cloud embedding call.

## Dual-provider client (`app/agent/client.py`)

```python
from app.agent.client import ModelRole, QwenClient
from app.config import get_settings
from app.trail import DecisionTrailStore

trail = DecisionTrailStore()
async with QwenClient(get_settings(), trail=trail) as client:
    result = await client.chat(
        [{"role": "user", "content": "Triage this incident"}],
        role=ModelRole.PRIMARY,
        thinking=True,
        tools=TOOLS,
        incident_id=incident_id,
        trace_id=trace_id,
    )
```

The production module is async and uses a reusable direct `httpx.AsyncClient`; it validates the approved Alibaba HTTPS endpoint again at the client boundary and requires the exact post-M0 provider/model chains. Accepted ADR-013 applies a 15-second application-level wall-clock deadline around each complete provider/model HTTP operation, keeps every HTTPX phase timeout at or below 15 seconds, adds no inter-attempt delay, and applies a 90-second hard cap to the complete logical call. Those code defaults are fixed rather than environment-configurable: the six-attempt reasoning route can reach OpenRouter and still leave Function Compute headroom, while the two-attempt fast route remains bounded to 30 seconds. Tests may inject only shorter deadlines for deterministic proof. Per-attempt expiry uses the secret-safe `timeout` reason; complete-budget expiry stops with redacted `logical_timeout` exhaustion and does not claim an unattempted transition. The client otherwise fails closed on missing credentials or unapproved failures, emits exactly one secret-safe `fallback` trail entry before each accepted transition, and emits one terminal `qwen_attempt` event for the successful or final failed pair. `ChatCompletion` preserves the successful provider, model, usage, reasoning, and tool-call data for the triage layer. OpenAI and Anthropic clients are prohibited under `app/` even when pointed at a Qwen-compatible endpoint. Build-time provider-verification scripts may use protocol-compatible tooling, but that layer never ships model selection into the Praxis runtime.

Routing observability keeps two event types deliberately separate. `fallback` preserves the transition payload exactly as `{from, to, reason}` and is emitted only when the next route is actually entered. One terminal `qwen_attempt` payload `{provider, model, outcome, reason, trace_id}` records the successful or final failed attempted pair; `outcome` is `success` or `failure`. Each accepted transition also emits one allowlisted structured log correlated by the server-owned `incident_id` and `trace_id`, with the same safe route labels and reason. Provider bodies, model output, credentials, and exception strings are never persisted or logged.

Thinking output: reasoning arrives as `message.reasoning_content` (DashScope compatible-mode) or `message.reasoning` / `<think>...</think>` (OpenRouter-normalized, model-dependent) — handle all, store genuine non-empty PRIMARY reasoning to the trail as a `thought`, and never return raw reasoning to the alert source. When PRIMARY reasoning is absent across all plan/tool rounds, record a safe `status: unavailable` thought and leave the incident `TRIAGED` without an approval-ready plan; FAST fallback can repair invalid plan JSON but cannot substitute for PRIMARY reasoning.

## Function calling (tool schemas)

```python
TOOLS = [{
  "type": "function",
  "function": {
    "name": "fetch_logs",
    "description": "Fetch recent alert-derived log evidence for a service",
    "parameters": {
      "type": "object",
      "properties": {"service": {"type": "string"}},
      "required": ["service"],
      "additionalProperties": false
    }
  }
}, ...]
```

`docs/API_CONTRACT.md` controls the public signature: `fetch_logs(service)` has no caller-selectable line-count argument. The adapter applies its own bounded result count and rejects extra arguments.

Loop: send messages+tools → if `finish_reason == "tool_calls"` → execute each call via the registry (dry-run adapters for write tools during planning — planning must NEVER mutate) → append `{"role": "tool", "tool_call_id": ..., "content": result}` → repeat until a final message. Cap at 6 tool rounds. Parallel tool calls may arrive as a list — execute all. Identical loop on both providers (both are OpenAI-compatible).

## Plan generation prompt (system message core)

```
You are Praxis, an SRE autopilot. Safety rules (absolute):
1) You NEVER execute state-changing actions; you only PROPOSE plans. A human approves.
2) Every step must name a registered tool, exact schema-valid args, the tool's registered risk_level, and a rollback. Read steps are safe; include at least one write remediation step.
3) If a similar prior incident is provided, prefer its proven resolution and cite it.
Output ONLY JSON matching the provided schema. No prose, no markdown fences.
```

Few-shot with one canned incident + valid plan. Inject `memory_match` (if any) as context. First validate the JSON structure with Pydantic, then synchronously bind every step to the active registry's exact strict argument model and risk policy. A valid plan contains at least one registered write remediation step; read steps are always `safe`, and write steps use exactly their registered default risk. Registry validation has no side effects and does not expose write schemas to the planning tool-call loop. On either validation failure, re-prompt with the redacted validation diagnostics, max 2 retries, then use the FAST_MODEL fallback plan template (which must pass the same registry validation).

## Embeddings + Tablestore memory (Qwen Cloud only)

```python
emb = qwencloud_client.embeddings.create(model="text-embedding-v4", input=summary,
                                         dimensions=1024).data[0].embedding
```

Tablestore (Python SDK `tablestore`): table `praxis_memory`, PK `id`; search index `praxis_memory_index` with `embedding` = Vector(Float32, 1024, cosine) + indexed `incident_id`, `service`, `signal`, `resolved_at`, `summary`, and `resolution` fields. Requested result columns are read from the data table; the live service normalizes the search-index `store` flag to false. The data-table `embedding` attribute is the SDK-required JSON array **string** (for example, `[0.1,-0.2,...]`), not a native list column. After the executor records `RESOLVED`, v1 attempts `put_row` (FR-10), then polls bounded exact-`incident_id` visibility because search indexing is asynchronous. That post-resolution attempt is non-fatal: its trail status is `stored` or `unavailable`, and a failed delivery is not retried unless proposed [ADR-020](decisions/ADR-020-durable-asynchronous-memory-write-delivery.md) is explicitly accepted and implemented. Recall via `KnnVectorQuery(field_name="embedding", top_k=3, float32_query_vector=emb)`, filter the same bounded `service` first, threshold 0.80 (FR-11), and reject partial-partition responses. Creds: FC-injected `ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET/SECURITY_TOKEN` in prod after an execution role is attached, `.env` locally. Runtime verifies but does not mutate the schema; `python scripts/provision_memory.py` owns idempotent table/index provisioning, while read-only `python scripts/probe_memory.py` exits nonzero for a missing table/index or any PK, retention, exact-field, vector, filter, or synchronization mismatch. If the Qwen Cloud embedding path is unavailable, triage continues without recalled context, the configured storage fallback remains operational, and M4 stays open until the owner-retained persistent-memory proof succeeds.

`TABLESTORE_ENDPOINT` is the per-instance **data endpoint**, not the regional control-plane endpoint. For local development in Singapore, use the Public form `https://<instance>.ap-southeast-1.ots.aliyuncs.com`; the Classic-network form ends in `ots-internal.aliyuncs.com`, and the VPC form ends in `vpc.tablestore.aliyuncs.com`. Before selecting Public, verify the instance's `NetworkTypeACL` contains `INTERNET`. M0 read-only discovery found configured instance `praxis` in `normal` state with `VPC`, `CLASSIC`, and `INTERNET` allowed, so local Praxis uses `https://praxis.ap-southeast-1.ots.aliyuncs.com`.

Production attaches the dedicated `praxis-fc-tablestore-role` only to the controller through `props.role: ${env('FC_EXECUTION_ROLE_ARN')}`. Its exact attachment set contains only the table-scoped custom `PraxisFcTablestoreRuntime` policy, which allows `ots:DescribeTable`, `ots:ListSearchIndex`, `ots:DescribeSearchIndex`, `ots:PutRow`, and `ots:Search`; it cannot create or delete tables or indexes. Run `python scripts/fc_role.py inspect` for a read-only check or `python scripts/fc_role.py ensure` for read-before-write provisioning. Inspection fails readiness on any extra system/custom attachment, and `ensure` refuses to mutate an over-privileged role. Both commands discard SDK stdout/stderr and raw exception messages and emit only allowlisted JSON. Function Compute then supplies short-lived `ALIBABA_CLOUD_ACCESS_KEY_ID`, `ALIBABA_CLOUD_ACCESS_KEY_SECRET`, and `ALIBABA_CLOUD_SECURITY_TOKEN` values automatically. Never render the local long-lived Alibaba credentials into the FC environment.

After deployment, list the current controller instance with `python scripts/fc.py instances`, then run `python scripts/fc.py memory-smoke --instance-id <id>`. This executes a read-only schema verification inside the live Function Compute instance and returns only backend/model/dimension evidence; it does not embed an incident, write memory, or bypass the HITL gate.

`python scripts/fire_alert.py --repeat` is only the FR-2 idempotency replay proof. Use `--recurrence` after a resolved incident to send the same alert body with a fresh idempotency key for the distinct FR-11 semantic-memory proof.

## Function Compute 3.0 deploy (deploy/s.yaml sketch)

```yaml
edition: 3.0.0
name: praxis
access: default
resources:
  praxis:
    component: fc3
    props:
      region: ap-southeast-1
      functionName: praxis-api
      runtime: custom.debian10
      code: ../
      memorySize: 1024
      timeout: 120
      instanceConcurrency: 1
      concurrencyConfig:
        reservedConcurrency: 1
      provisionConfig:
        alwaysAllocateCPU: true
        alwaysAllocateGPU: false
        defaultTarget: 1
        scheduledActions: []
        targetTrackingPolicies: []
      internetAccess: true
      customRuntimeConfig:
        command: [/var/fc/lang/python3.10/bin/python3]
        args: [-m, uvicorn, app.main:app, --log-config, app/uvicorn_log_config.json, --host, 0.0.0.0, --port, '9000', --timeout-keep-alive, '86400']
        port: 9000
        healthCheckConfig:
          httpGetUrl: /healthz
          initialDelaySeconds: 1
          periodSeconds: 3
          timeoutSeconds: 1
          failureThreshold: 3
          successThreshold: 1
      environmentVariables:
        PATH: /var/fc/lang/python3.10/bin:/code/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/code:/code/bin:/opt:/opt/bin
        PYTHONPATH: /code/python:/code
        APP_ENV: prod
        DEPLOYED_ON: alibaba-fc
        PORT: '9000'
        DASHSCOPE_API_KEY: ${env(DASHSCOPE_API_KEY)}
        OPENROUTER_API_KEY: ${env(OPENROUTER_API_KEY)}
        PROVIDER_ORDER: qwencloud,openrouter
        WEBHOOK_SIGNING_SECRET: ${env(WEBHOOK_SIGNING_SECRET)}
        PRAXIS_OPERATOR_TOKEN: ${env(PRAXIS_OPERATOR_TOKEN)}
        DEDUP_WINDOW_SECONDS: ${env(DEDUP_WINDOW_SECONDS)}
        MAX_WEBHOOK_BODY_BYTES: ${env(MAX_WEBHOOK_BODY_BYTES)}
        QWEN_BASE_URL: ${env(QWEN_BASE_URL)}
        QWENCLOUD_MODELS: ${env(QWENCLOUD_MODELS)}
        OPENROUTER_MODELS: ${env(OPENROUTER_MODELS)}
        PRIMARY_MODEL: ${env(PRIMARY_MODEL)}
        FAST_MODEL: ${env(FAST_MODEL)}
        OPENROUTER_FAST_MODEL: ${env(OPENROUTER_FAST_MODEL)}
      triggers:
        - triggerName: http
          triggerType: http
          triggerConfig: {authType: anonymous, methods: [GET, POST]}
```

ADR-024 requires all three capacity layers for both `praxis-api` and `praxis-demo-target`. `instanceConcurrency: 1` limits simultaneous requests handled by one instance; `concurrencyConfig.reservedConcurrency: 1` caps each function at the accepted single-instance boundary; and `provisionConfig.defaultTarget: 1` with `alwaysAllocateCPU: true` keeps that one instance active after a response. Reserved concurrency alone is not lifecycle proof. Inside the controller, one process-wide FIFO permits one running and three pending jobs; pending work expires after 300 seconds and each dequeued logical job has a 240-second deadline. A recycle of that single process still clears active incidents, so fire the approval candidate immediately before the operator reviews it; never reuse a stale incident ID.

ADR-025 adds `PRAXIS_OPERATOR_TOKEN` at the application secret boundary. Production validates it as a non-placeholder 32–4096 visible-ASCII value with at least eight distinct characters and no whitespace, then requires `Authorization: Bearer <operator token>` for incident list/detail/memory reads and approval mutations. Root, health, and HMAC-signed webhook intake retain their separate public boundaries. The browser token is entered locally, exists only in page memory, and is discarded on reload; never put it in HTML, a URL, logs, screenshots, browser storage, a recording prompt, or a third-party recorder credential.

ADR-010 needs a two-phase first deployment because the controller must know the isolated target's generated URL before the complete manifest can be verified. Put `PRAXIS_DEMO_TARGET_TOKEN` in the ignored `.env`, run `python scripts/fc.py bootstrap-target-verify`, then `python scripts/fc.py bootstrap-target-deploy`. Copy only its allowlisted `url` result into `.env` as `PRAXIS_DEMO_TARGET_URL`; never copy raw Serverless Devs output. Now set `PRAXIS_OPERATOR_TOKEN` in the same ignored secret boundary, run final `python scripts/fc.py verify`, and then `python scripts/fc.py deploy`. The target-only `deploy/target.s.yaml` requires only the target token, while the final `deploy/s.yaml`, `scripts/probe_fc.py`, and production startup require the controller URL/token and operator token. The final app also verifies that the real `restart_service` handler is installed, so the placeholder handler cannot satisfy production readiness.

`custom.debian10` is the FC web-function runtime wrapper; Praxis still runs Python 3.10 from `/var/fc/lang/python3.10/bin/python3`. FastAPI is served directly by Uvicorn on the FC custom-runtime port, so no event-handler shim or additional framework adapter is required. The installed `fc3` component does not auto-run pip for custom runtimes, so build and verify `python/` with `python scripts/build_fc_dependencies.py` (the exact official fc3 image), complete the target bootstrap above, then run final `python scripts/fc.py verify` and `python scripts/fc.py deploy`. The wrapper loads ignored `.env` values into only the child process, captures raw Serverless Devs output, prints only an allowlisted JSON summary, assigns a unique CLI trace ID, and removes that trace's local plaintext logs in `finally`. Every action has a fixed deadline; a hung child receives terminate, then kill after a bounded grace period. A read-only/verify timeout makes no release-state claim. A deploy/bootstrap timeout exits `124` with `release_state=unknown`, `retry_safe=false`, and a fixed reconcile-before-retry action; inspect the FC resources and generated URLs through secret-safe read-only probes or the console before another mutation. The wrapper intentionally exposes no arbitrary/debug/preview pass-through. This containment is mandatory because Serverless Devs 3.1.10's normal deploy result includes rendered environment values.

For the M0 deployed-completion gate, warm the public `/healthz` route, then run `python scripts/fc.py instances` immediately and take the first value in `instance_ids`. From a PTY-enabled terminal, run `python scripts/fc.py smoke --instance-id <id>`. The wrapper uses a minimal `deploy/instance.s.yaml` from a temporary working directory, strips credential-like variables from the child environment, and invokes the required `s instance exec --instance-id <id> --shell /bin/bash --cmd "cd /code && /var/fc/lang/python3.10/bin/python3 -m app.m0_smoke"` form. The smoke command refuses to run without FC's injected `FC_FUNCTION_NAME`, `FC_INSTANCE_ID`, and `FC_REGION`; require a parsed `{"ok":true,...}` line from the remote command because the component's local process exit code proves only the WebSocket transport, not remote success. For ADR-010's isolated-target invariant, use the separate secret-free `python scripts/fc.py target-instances` action; it selects `deploy/target-instance.s.yaml` and emits only the allowlisted `instance_ids` array.

After every recording revision deploy, run `python scripts/probe_fc.py` and require both `configuration_matches=true` and `active_capacity_matches=true`. The capacity summary checks both functions for active CPU allocation, disabled GPU allocation, provisioned target/current equal to one, and reserved concurrency equal to one; it prints only allowlisted booleans. Then verify the fixed protected-route `401` challenge without a token and successful authenticated reads through both the native FC trigger and `https://praxis.kopachelli.dev`, without printing the token or response bodies. These are live gates: local manifests, tests, or environment flags do not prove the deployed lifecycle or authentication boundary.

### Canonical HTTPS domain

The production UI and API use `https://praxis.kopachelli.dev`. Cloudflare holds a DNS-only CNAME to the account-scoped Alibaba FC regional endpoint, and Alibaba FC owns the matching custom-domain binding and `/*` route to the controller. The default `fcapp.run` trigger remains useful for deployment diagnostics, but its gateway can inject `Content-Disposition: attachment` for the root HTML response; the native custom domain is therefore the browser-facing endpoint. Wrangler does not provide a general arbitrary-DNS-record command, so the record was created with the Cloudflare DNS API using an existing locally encrypted credential. No Cloudflare Worker or non-Alibaba runtime sits in the request path.

The initial TLS certificate was issued with DNS-01, installed on the FC custom-domain resource, and its temporary private-key workspace was removed after verification. Renewal is not yet automated; `PRAXIS-60` tracks the work, and proposed ADR-016 prefers an Alibaba-managed commercial certificate lifecycle because it keeps private-key custody and deployment outside the application runtime. That proposal is conditional on owner-approved cost and confirmed third-party-DNS eligibility. Do not add renewal dependencies, DNS validation records, certificate-management permissions, or automation before the ADR is accepted.

## deploy/alibaba_proof.py (submission requirement, FR-15)

One file that (a) requires an exact sentinel from the accepted Qwen Cloud primary and records only safe requested/returned Qwen identities, (b) verifies the complete accepted `praxis_memory` table and `praxis_memory_index` schema through the Tablestore SDK, and (c) contacts the validated generated `fcapp.run` `/healthz` endpoint and requires matching Praxis `ok`, Alibaba-FC, primary-model, version, and trace-header markers. Its allowlisted success object contains those bounded evidence fields; failures report only a fixed stage/type/code envelope and exit nonzero. Link it directly in the Devpost form. This file uses Qwen Cloud ONLY—no OpenRouter path—and its hardened live result must be rerun only after the owner/freeze gate allows provider and deployment proof calls.

## 💳 Payment playbook (South Africa)

Alibaba Cloud account binding accepts Visa/Mastercard/AMEX/JCB (international + 3DS enabled, ≥$1 for the pre-auth; charge shows as ALI*ALICLOUD) **or PayPal** (verified account, non-China; one card/PayPal per Alibaba account). If the SA card fails:
1. Retry with **PayPal** — the standard workaround for card-binding failures.
2. Confirm with the bank: international online transactions + 3DS enabled, risk controls not blocking.
3. Try Chrome incognito / different device (3DS page failures are often browser-side).
4. Continue development through the general Model Studio API; ADR-008 keeps it first and uses OpenRouter only after an eligible Qwen Cloud authentication/payment, unavailable-model, quota/rate-limit, 5xx, or timeout failure. Other request/transport failures terminate safely. Do not substitute a coding-plan key in the application backend.
5. If the account cannot be activated at all → **stop-the-line**: deployment on Alibaba has no substitute. Open an Alibaba support ticket immediately and escalate to Khristian; keep building locally against OpenRouter in the meantime so no hours are lost.

## Token budget (NFR-6)

Classification ≈ 300 tok on qwen-flash; root-cause + plan ≈ 3–6k on PRIMARY; embeddings negligible. Full demo loop should remain < $0.10 and the build target remains < $5 equivalent on the general Model Studio account. Coding-plan discounts are excluded from this estimate. Log `usage` + provider per call to the trail.
