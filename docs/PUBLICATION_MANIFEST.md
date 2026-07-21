# Publication manifest

> **Frozen submission:** do not stage, commit, push, tag, deploy, or update any
> linked Qwen hackathon material while judging is active. Use this manifest only
> after written organizer permission or after judging ends.

Praxis currently has many untracked source and working files. Never use
`git add .`, `git add -A`, or another broad staging command. Stage the intended
public surface explicitly, then inspect the index before committing.

## Explicit publication allowlist

Run these blocks from the repository root only when the freeze has lifted:

```powershell
git add -- .gitignore .gitleaksignore .fcignore .env.example README.md LICENSE requirements.txt pytest.ini

git add -- app ui/index.html

git add -- `
  deploy/alibaba_proof.py `
  deploy/s.yaml `
  deploy/instance.s.yaml `
  deploy/target-instance.s.yaml `
  deploy/target.s.yaml

git add -- `
  scripts/build_fc_dependencies.py `
  scripts/capture_submission_screenshots.py `
  scripts/check_plan_latency.py `
  scripts/check_recording_ui.py `
  scripts/ensure_operator_token.py `
  scripts/fc.py `
  scripts/fc_role.py `
  scripts/fire_alert.py `
  scripts/probe_fc.py `
  scripts/probe_memory.py `
  scripts/provision_memory.py `
  scripts/verify_failover.py `
  scripts/verify_runtime_failover.py `
  scripts/verify_providers.py

git add -- tests

git add -- `
  docs/PRD.md `
  docs/ARCHITECTURE.md `
  docs/API_CONTRACT.md `
  docs/BUILD_PLAN.md `
  docs/DEMO_AND_SUBMISSION.md `
  docs/QWEN_CLOUD_INTEGRATION.md `
  docs/PUBLICATION_MANIFEST.md `
  docs/decisions `
  docs/assets
```

The `docs/assets` allowlist above includes the reviewed OpenAI/blog cover at
`docs/assets/praxis-openai-cover.png`. Keep it presentation-only; it is not
runtime or Alibaba deployment evidence.

Do not stage `docs/screenshots/` as a directory. Its current dashboard capture is
the stale authorized revision and its current health capture is replacement-
required. After owner-authorized final evidence capture, add only the exact
reviewed replacement filenames to this allowlist, then inspect each image at
original resolution before staging.

Before committing, inspect every staged path and the staged diff:

```powershell
git status --short
git diff --cached --name-only
git diff --cached --check
git diff --cached
```

Run the repository's complete tests and secret scanner against the exact staged
snapshot. If any unexpected path appears, stop and unstage that exact path; do
not repair the index with a destructive reset. The published `.gitleaksignore`
records the reviewed, non-secret false positives (a synthetic JWT test fixture
and one worklog prose line) so a clean `gitleaks git --staged` exits zero; every
entry was manually verified to contain no real credential.

## Release refs after the freeze

GitHub's default branch is `main`; local work is on `dev`. After the explicit
allowlist is staged, reviewed, tested, and committed on `dev`, publish one exact
reviewed release SHA in this order:

1. Push `dev` and read back `origin/dev`.
2. From a clean worktree, fast-forward `main` to that exact `dev` SHA—never
   merge with an extra commit and never force-push.
3. Push `main`, then verify local `dev`, local `main`, `origin/dev`, and
   `origin/main` all resolve to the same reviewed SHA.
4. Verify GitHub still names `main` as the default branch before changing the
   repository to public or creating `v0.1-hackathon`.

The freeze still blocks every step above until organizer permission or judging
end is recorded.

## Intentionally excluded

- `.env` and all real environment variants; only `.env.example` is public.
- Private keys, browser profiles, tool sessions, agent state, and analysis
  outputs.
- Generated `python/` dependencies, caches, coverage, logs, and local debris.
- `docs/worklog/` and `docs/LINEAR_QUEUE.md`, which are internal operational
  mirrors rather than product documentation.
- `docs/submission/` while it contains post-deadline drafts or unresolved public
  URL/media placeholders.
- `docs/screenshots/` until owner-authorized presentation-ready replacements are
  captured and their exact filenames are added above; never stage the directory
  broadly.
- `PROMPTS.md`, `CLAUDE_CODE_HANDOVER.md`, `recap.md`, `codex-config.snippet.toml`,
  local tool configuration, and non-product design references.
- `AGENTS.md` — Khristian decided on 2026-07-21 (resolving `PRAXIS-115`) to keep it
  **out** of the public repository; it stays a local untracked file and is never
  committed to the published surface.

The Function Compute package follows `.fcignore`, not this Git allowlist. It
must retain `python/**` because that directory contains the Linux dependency
bundle, while excluding local browser/agent state and repository-only material.
