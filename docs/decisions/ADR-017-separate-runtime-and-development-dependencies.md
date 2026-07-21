---
status: proposed
date: 2026-07-21
decision-makers: Khristian Kopachelli
---
# ADR-017 — Separate runtime and development dependencies

## Context and Problem Statement

Praxis currently installs every dependency from one `requirements.txt`, and `scripts/build_fc_dependencies.py` copies that complete set into the Alibaba Function Compute artifact. The file includes `pytest`, which is imported only by tests, and the `openai` package, which is imported only by the local M0 provider-verification scripts. No module under `app/` imports either package.

The same audit found a forward-compatibility warning in the development environment: FastAPI 0.139.2 re-exports Starlette 1.3.1 TestClient, which now prefers HTTPX2 and emits `Using httpx with starlette.testclient is deprecated; install httpx2 instead.` Praxis production code still directly uses HTTPX 0.28.1 for Qwen-compatible HTTP, embeddings, and the isolated restart adapter. A wholesale client migration is not required.

The original ADR-017 proposal added a test-only HTTPX2 file while leaving `pytest` and `openai` in the runtime file. That would suppress the warning but would not create the clean deployment boundary it claimed. This amended proposal treats the packaging problem coherently before any dependency changes are accepted.

## Decision Drivers

* Keep only packages imported by the application in the FC dependency artifact.
* Keep the OpenAI-compatible SDK out of the product runtime while retaining it for local Qwen provider-verification tooling.
* Follow Starlette 1.3's supported TestClient dependency path.
* Preserve the proven production HTTPX implementation and Qwen-only model policy.
* Keep local setup reproducible with one development requirements command.
* Bound every moved or added dependency to its current supported major series.

## Considered Options

### A. Keep one requirements file and add HTTPX2

Add `httpx2>=2,<3` to the existing file. This is textually small but bundles two HTTP clients, two test/tooling-only packages, and their dependency chains into Function Compute.

### B. Split runtime and development requirements

Keep application imports only in `requirements.txt`. Add `requirements-dev.txt` that includes `-r requirements.txt` plus `openai>=1.68,<3`, `pytest>=8,<10`, and `httpx2>=2,<3`. Local development, tests, and provider verification install the development file; FC continues installing the runtime file only.

### C. Pin or downgrade Starlette

Constrain a transitive dependency below the warning. This hides the supported migration path and leaves the existing FC package bloat untouched.

### D. Migrate all application HTTP code to HTTPX2

Change provider, embedding, restart, smoke, and test clients. This expands runtime risk without being necessary for the TestClient warning or dependency boundary.

## Decision Outcome

Proposed: **Option B, split runtime and development requirements**.

After explicit owner acceptance:

1. Remove `openai` and `pytest` from `requirements.txt`; leave FastAPI, Uvicorn, HTTPX, Pydantic, and Tablestore as runtime dependencies.
2. Add `requirements-dev.txt` with `-r requirements.txt`, `openai>=1.68,<3`, `pytest>=8,<10`, and `httpx2>=2,<3`.
3. Update contributor commands to install `requirements-dev.txt`; keep `scripts/build_fc_dependencies.py` pinned to `requirements.txt`.
4. Update the FC import gate to require application imports and explicitly prove that `openai`, `pytest`, `httpx2`, and `httpcore2` are absent from the generated FC dependency directory.
5. Run provider-verification unit tests, the complete regression with warnings enabled, compile checks, the Qwen-only runtime scan, and FC dependency verification.

No requirements file, package, environment, source import, or FC artifact may change while this ADR is `proposed`.

### Consequences

* Good: removes test and local-tooling packages from the Alibaba runtime artifact.
* Good: makes the absence of the OpenAI-compatible SDK from product runtime mechanically verifiable while preserving Qwen provider probes outside `app/`.
* Good: resolves Starlette's deprecation through the supported test client without touching production HTTP code.
* Good: reduces FC artifact size and dependency attack surface.
* Bad: contributors must distinguish runtime installation from the documented development installation.
* Bad: HTTPX and HTTPX2 coexist in development environments, so their separate roles must remain explicit.
* Neutral: any future production migration to HTTPX2 remains a separate decision with provider/remediation compatibility proof.

## References

* [Starlette TestClient source: HTTPX2 preferred, HTTPX fallback deprecated](https://github.com/Kludex/starlette/blob/main/starlette/testclient.py)
* [Starlette TestClient documentation](https://www.starlette.io/testclient/)
* [HTTPX2 package metadata and Python 3.10 support](https://pypi.org/project/httpx2/)
* [FastAPI testing documentation](https://fastapi.tiangolo.com/tutorial/testing/)
