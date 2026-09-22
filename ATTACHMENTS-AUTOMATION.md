# Attachment migration without interactive login — research (2026-07)

Question: can Zephyr Essential / Squad Cloud attachment **binaries** be downloaded
fully automated — ideally with only an API token; Jira email+API-token acceptable —
for a large enterprise where SSO/MFA may block interactive logins?

Short answer: **no token-only path exists today.** Every angle was researched
(official Atlassian/SmartBear docs, plus live probes against a test tenant);
the practical enterprise answer is a **Guard authentication-policy exemption
for one migration account** (option 3), with a SmartBear support ticket
(option 2) as the parallel track.

Verified facts this rests on (see README "Attachments" for the mechanics):

- The public Essential v2 API serves attachment *metadata* only and denies the
  binary to the access token (`401`). The official v2.9 OpenAPI contract
  contains **zero attachment endpoints** — the gap is contractual, not a bug.
- Binaries are only served by (a) the app's internal backend via pre-signed S3
  URLs, authenticated with a **Forge invocation token** — minted exclusively by
  Atlassian's Forge platform when the app page renders in a real user session —
  and (b) the CDN (inline images) behind browser session cookies.

## Options, ranked for an SSO/MFA enterprise

### 1. Legacy ZAPI (accessKey/secretKey JWT) — DEAD for Essential tenants (verified)
The old Squad Cloud API (`prod-api.zephyr4jiracloud.com/connect/public/rest/api/1.0`)
is still alive (probed 2026-07: live 401 challenge, not 404) and documents
`GET /attachment/{id}` returning `File`, with fully headless JWT auth
(per-user accessKey/secretKey from Jira → Apps → Zephyr → API Keys).
**But:** it addresses the OLD data plane. Attachments on the new "Essential
experience" have plain numeric ids (verified live: `168918963`), not ZAPI's
`ztId` format (`0001479…-hex-0001`), and **the API-Keys / Access-Key UI does
not exist anywhere in the Essential app** (verified 2026-07 by walking every
frame of our Essential 10.x tenant's app UI — no key-generation screen, so the
keys ZAPI needs cannot even be minted). Dead for Essential-experience tenants;
only relevant to grandfathered legacy-Squad tenants.
*Also note:* tenants still on the LEGACY Squad experience keep test-case
attachments as genuine **Jira issue attachments** — those download with a plain
Jira email+API token (which bypasses SSO for REST by default).

### 2. SmartBear support ticket — undocumented but real channel
No customer-facing attachment export exists (exports are data-only CSV/Excel).
However SmartBear's own Squad→Essential migration engine "migrates attachments
as they are" server-side and is enabled per-tenant by SmartBear Support on
request — proving they can move binaries. For an enterprise engagement, open a
ticket asking for (a) roadmap on v2 attachment endpoints, (b) a one-time bulk
attachment export or temporary pre-signed URLs. Run this in parallel; do not
block the migration on it.

### 3. Guard policy exemption + captured session — the supported enterprise path
Atlassian Guard explicitly supports multiple authentication policies, and
**non-billable policies are documented as the mechanism for bot accounts** —
a non-billable policy *cannot* enforce SSO. So the org admin:

1. Creates ONE dedicated regular managed account (NOT an Atlassian "service
   account" — those cannot log in to the UI at all) with access to Jira + Zephyr.
2. Places it in a separate authentication policy without enforced SSO
   (optionally without MFA, or with TOTP the migration operator holds).
3. That account performs the one-time `capture_session.py` login (~monthly,
   Atlassian sessions last 2–4 weeks); the script re-mints the short-lived
   Forge token headlessly during runs (see README).

This is a documented, supported Guard configuration — the correct framing for
the customer's security team is "scoped migration account in a dedicated auth
policy", not "SSO bypass". Caution: Guard policies can also *block API tokens*
per policy — verify the customer's policy before promising even the plain
Jira-token flows.

### 4. Scope attachments out
The Zephyr Squad scope addendum already baselines attachments as a hard
exclusion. Filenames are preserved as `📎` notes on every entity either way.

## Confirmed dead ends (do not revisit)

- **Connect JWT**: the app's shared secret is held only by SmartBear and
  Atlassian; no tenant-side path (official Connect docs).
- **Forge invocation tokens**: minted only by the Forge platform toward the
  app's registered remote; no API mints one for a tenant script.
- **OAuth 3LO/2LO**: scopes cover Atlassian product REST only — no scope
  reaches a third-party app's backend.
- **API-token → browser-session exchange**: cookie/basic session auth on
  Jira Cloud was removed in 2019; no programmatic session creation exists,
  and under SAML the only "automation" is automating the IdP login itself.
