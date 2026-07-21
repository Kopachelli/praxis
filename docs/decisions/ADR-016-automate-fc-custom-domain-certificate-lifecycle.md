---
status: proposed
date: 2026-07-21
decision-makers: Khristian Kopachelli
---
# ADR-016 — Automate the FC custom-domain certificate lifecycle

## Context and Problem Statement

`praxis.kopachelli.dev` is a DNS-only Cloudflare CNAME whose browser-trusted TLS certificate is terminated by the native Alibaba Cloud Function Compute custom-domain resource. The initial Let's Encrypt certificate was issued manually through DNS-01 and is valid through 2026-10-18T22:53:44Z, but its temporary private-key workspace was intentionally removed. Without a managed renewal path, the canonical demo URL can expire while the Praxis function remains healthy.

Certificate automation is an architectural and security decision: it changes deployment operations, introduces a certificate-management owner, and can expose DNS-write credentials, a certificate private key, or the broad FC `UpdateCustomDomain` permission if designed poorly. The Praxis runtime role must not acquire any of those capabilities.

## Decision Drivers

* Keep `praxis.kopachelli.dev` on the accepted Alibaba Function Compute runtime and native custom-domain route.
* Minimize long-lived secret material outside managed cloud control planes.
* Keep certificate-management permissions out of the Praxis application runtime.
* Detect renewal or deployment failure before the public certificate expires.
* Avoid relying on Wrangler for capabilities it does not provide: Wrangler has no general DNS-record or public server-certificate renewal command.
* Require explicit owner approval before incurring certificate-management charges or changing DNS validation.

## Considered Options

### A. Alibaba-managed commercial certificate lifecycle

Purchase an eligible Alibaba Cloud commercial certificate, prefer a DigiCert product that supports persistent CNAME domain pre-authorization with third-party authoritative DNS, enable Certificate Management Service auto-management, and create a hosted deployment task that updates the existing Function Compute custom-domain certificate.

This keeps certificate issuance, private-key custody, renewal, and FC deployment inside Alibaba Cloud. It has the smallest automation secret surface, but eligibility must be confirmed for this account and Cloudflare-hosted zone. Alibaba's general hosting prerequisites reference Alibaba Cloud DNS, while newer DigiCert pre-authorization supports third-party DNS; the documentation does not explicitly guarantee that every hosted-renewal prerequisite is satisfied by that combination. Hosting is also a paid service, documented at USD 40 per renewal in addition to certificate cost.

### B. External ACME DNS-01 automation

Run a scheduled ACME client against Cloudflare DNS, then upload the resulting PEM certificate and private key through FC `UpdateCustomDomain`. This preserves Let's Encrypt and Cloudflare authority but adds a Cloudflare zone-scoped DNS token, ACME account key, leaf private key, separate Alibaba deployment credential, scheduler, secure state storage, monitoring, and rollback machinery. Alibaba documents `fc:UpdateCustomDomain` against `*`, so the deployment credential cannot be restricted to only this custom-domain resource.

### C. Continue manual renewal

Retain the current manual DNS-01 procedure and calendar reminders. This adds no service or dependency, but it leaves a preventable availability risk and does not satisfy the reliability objective in `PRAXIS-60`.

## Decision Outcome

Proposed: **Option A, Alibaba-managed commercial certificate lifecycle**, subject to all of the following owner-retained gates:

1. Khristian explicitly accepts this ADR and authorizes the certificate and hosting cost.
2. A read-only console check confirms that the selected certificate supports third-party-DNS persistent CNAME pre-authorization, auto-management, and hosted deployment to the existing FC custom domain.
3. The validation CNAME is the only Cloudflare DNS change; it is reviewed before creation and remains DNS-only.
4. The deployment target is exactly the existing `praxis.kopachelli.dev` FC custom-domain resource.

If the eligibility check fails, do not silently switch to external ACME automation. Keep the still-valid certificate in place and propose an amendment or replacement ADR for Option B with a named trusted renewal controller and secret store.

No application dependency, DNS record, certificate, billing setting, RAM permission, or cloud resource may change while this ADR is `proposed`.

### Security Boundary

* Alibaba Certificate Management Service owns the renewed private key and deployment task; certificate or key bodies never enter the repository, worklog, Linear, application environment, or normal logs.
* The existing `praxis-fc-tablestore-role` receives no certificate-management, DNS, or `UpdateCustomDomain` permission.
* A one-time Cloudflare validation CNAME is public routing data, not a credential. If API automation is later required, it needs a separate zone-scoped token and a revised accepted ADR.
* Debug HTTP traces and raw certificate-management API responses remain disabled because certificate payloads and credential identifiers can appear in control-plane traffic.

### Monitoring and Failure Handling

* Enable Alibaba public-domain monitoring and hosted-deployment notifications.
* Add an independent HTTPS monitor with warning thresholds at 30 days, urgent at 14 days, and critical at 7 days remaining.
* After every managed deployment, validate the served hostname/SAN, issuer, chain, new expiry, `/`, and `/healthz` over HTTPS.
* A failed eligibility check or renewal leaves the current valid certificate unchanged and alerts the owner. Do not replace a working certificate with an unverified candidate.

### Consequences

* Good: the smallest secret and operational surface of the supported options.
* Good: no certificate-management authority is added to the application runtime.
* Good: renewal and FC deployment remain on Alibaba Cloud, consistent with the accepted hosting architecture.
* Good: independent expiry and endpoint monitoring catches control-plane deployment failures.
* Bad: adds a commercial certificate and paid hosting/renewal service.
* Bad: requires one owner-confirmed eligibility check because Alibaba's third-party-DNS and hosting documentation is not fully explicit about their interaction.
* Bad: if the managed path is ineligible, a second ADR is required before external ACME automation.

## References

* [Function Compute custom domains](https://www.alibabacloud.com/help/en/functioncompute/fc/configure-custom-domain-names)
* [Certificate deployment-method matrix](https://www.alibabacloud.com/help/en/ssl-certificate/ssl-certificate-deployment-scheme-selection)
* [Certificate hosting and auto-management](https://www.alibabacloud.com/help/en/ssl-certificate/enable-certificate-hosting)
* [Hosted deployment to Alibaba Cloud services](https://www.alibabacloud.com/help/en/ssl-certificate/manage-alibaba-cloud-services-to-which-hosted-certificates-are-deployed)
* [Third-party-DNS domain pre-authorization](https://www.alibabacloud.com/help/en/ssl-certificate/domain-name-level-authorization-for-verification-free-issuance)
* [Cloudflare API-token permissions](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)
* [Let's Encrypt DNS-01 guidance](https://letsencrypt.org/docs/challenge-types/)
* [Wrangler command catalog](https://developers.cloudflare.com/workers/wrangler/commands/)
