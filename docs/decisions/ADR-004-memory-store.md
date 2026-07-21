---
status: accepted
date: 2026-07-19
decision-makers: Khristian Kopachelli
---
# ADR-004 — Memory store: Tablestore vector search (1024-d)

## Context and Problem Statement
FR-10/FR-11 need persistent incident memory with semantic recall, on Alibaba infrastructure, provisionable in under an hour.

## Considered Options
* Tablestore search index with KnnVectorQuery
* Tair (TairVector, Redis-compatible)
* AnalyticDB for PostgreSQL (FastANN)
* In-memory dict + cosine (no persistence)

## Decision Outcome
Chosen: **Tablestore KnnVectorQuery**, because it is serverless, available in international regions, currently unbilled for KNN in public preview, and adds a second Alibaba service to the deployment story. Vector dim fixed at **1024** to match `text-embedding-v4` default.

### Consequences
* Good: real persistence across FC restarts; strengthens Technical Depth scoring.
* Bad: managed "Memory Store" flavour is Beijing-only — we use generic KNN on the Intl account.
* Operational fallback: `MEMORY_BACKEND=inmem` remains behind the same interface, but it is not evidence of persistent Tablestore recall.
* Owner-retained scope amendment (2026-07-21): the expired T0+17h cut contingency was not invoked. M4 remains open until an explicitly approved resolution writes its Tablestore row and a distinct recurrence surfaces that prior incident. This preserves the accepted Tablestore architecture and records the later OQ-3 direction; it does not claim that the live exit proof has passed.
