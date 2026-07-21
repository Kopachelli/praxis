---
status: rejected
date: 2026-07-20
decision-makers: Khristian Kopachelli
consulted: Codex
informed: Praxis contributors
---
# ADR-011 — Skip the optional build blog

## Context and Problem Statement

The M6 plan includes a build blog for the optional Blog Post Award. It is not one of the mandatory Track 4 submission artifacts. Khristian directed Praxis to skip the blog on 2026-07-20 so the remaining time stays focused on the deployed agent, proof, video, and Devpost submission.

Dropping planned scope requires an ADR before the PRD, build checklist, and Linear mirror can be changed. Should Praxis retain the optional blog task or remove it from the hackathon delivery scope?

## Decision Drivers

* The internal submission cutoff is 2026-07-20 12:00 PDT.
* Core runtime, safety, deployment, and demo proof outrank optional award work.
* The Devpost writeup and README still satisfy mandatory documentation needs.
* The local plan and Linear must represent skipped work explicitly rather than silently omitting it.

## Considered Options

* Publish the 800–1200 word build blog as planned.
* Draft a shortened blog only if render time becomes idle.
* Remove the optional blog from M6 and the hackathon scope.

## Decision Outcome

Rejected: **do not remove the optional build blog from the hackathon scope.**

Khristian rejected the proposed scope cut on 2026-07-20. The 800–1200 word build blog remains an M6 deliverable and may reuse the README narrative, Devpost write-up, Mermaid architecture diagram, screenshots, and video script.

### Consequences

* Good: Praxis remains eligible for the Blog Post Award.
* Good: existing README, Devpost, diagram, screenshot, and video-script material can be reused.
* Bad: writing and publishing the article consumes time during a deadline-constrained M6.
* Guardrail: core runtime, safety, Alibaba deployment, demo proof, public video, and Devpost submission remain higher priority if delivery risk emerges.
