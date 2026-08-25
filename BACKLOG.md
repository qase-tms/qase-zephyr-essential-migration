# Backlog — items that need a product/scope decision

Parked here because each one changes behavior, scope, or the customer
conversation. Everything unambiguous from the 2026-07 cross-script review has
already been implemented (see README + git history).

## 1. Native Qase shared steps for call-to-test

Zephyr steps can reference another test case (`{"testCase": {"testCaseKey": …}}`).
Today they render as a textual `→ Call to test case KEY` step (and no longer
crash the import). The alternative is converting each referenced case into a
Qase **shared step** and referencing it natively.
**Decision needed:** is the extra fidelity worth it? The referenced case is also
migrated as a normal case, so its steps would exist twice (as a case AND a
shared step). TestRail's script doesn't do this either. Effort: M.

## 2. SCIM user migration

TestRail's script creates missing users via SCIM (`qase.scim_token`). We now
support `users.map` (account id → Qase user id) with `users.default` fallback,
which covers attribution without creating anyone. **Known Qase limitation
(verified live 2026-07): the v1 results API ignores `author_id` — every
API-created result is attributed to the token owner. Case authors ARE honored.
This limits what any user migration can achieve for run results.**
**Decision needed:** auto-create users via SCIM? Requires a SCIM token, a
customer conversation (seats/licensing!), and email visibility — Zephyr only
exposes Atlassian account IDs, so resolving id → email would require a Jira API
call (`/user?accountId=`), which expands the sanctioned Jira surface
(CLAUDE.md security constraint). Effort: M–L, mostly policy.

## 3. Case ID preservation — ✅ IMPLEMENTED 2026-07-17

Shipped as the `cases.preserve_ids` config toggle (default `true` since the
2026-07-18 config review — delta migrations depend on it): the Zephyr
key suffix is sent as the Qase case id (`ZEM-T123` → case `123`). The re-run
guard already enforces the empty-target requirement by default; combining it
with `migration.allow_existing_target` emits a collision warning. Keys without
a `-T<number>` suffix fall back to Qase-assigned ids with a report entry.

## 4. Full idempotent re-run / resume — ⏳ PARTIALLY SHIPPED 2026-07-17

**Add-only delta migration shipped** as `migration.delta` (see README "Delta
migrations"): cases match by preserved id, suites by title+parent, milestones
by title, runs by run title; pre-existing entities reused/skipped with report
summaries. This covers the cutover use case (trial migration → final delta)
AND doubles as crash resume (re-run with delta on: finished work is skipped).
**Still open:** update/re-sync of changed cases (impossible without a
modified-date from the source API) and re-sync of executions added to
already-migrated cycles (would need per-result matching inside existing runs).

## 5. Qase defects from failed executions

`ResultCreate` has a `defect: true` flag — Qase can open a defect per failed
result. Zephyr executions link Jira bugs (we note them in the result comment).
**Decision needed:** should failed executions with linked Jira bugs create Qase
defects? Might double-track bugs customers keep in Jira. Effort: S.

## 6. Docker / pipx packaging

Removes the Python-env setup friction for Basic/Standard (customer-run)
packages — the biggest support-cost driver after config confusion.
**Decision needed:** distribution channel + maintenance owner (applies to all
six scripts, not just this one). Effort: M per script, or one shared base image.

## 7. Attachment browser-session default

Binary attachment migration works (pre-signed S3 + CDN cookie replay) but stays
opt-in with a disclosure because it replays a full Atlassian browser session
against internal endpoints (gray-area vs the sanctioned API).
**Decision needed:** keep opt-in (recommended), or promote per-engagement after
customer sign-off. No code work — policy only.

## 8. Configurations / shared parameters

Qase has configurations; Zephyr Essential has no equivalent entity (verified —
no endpoint). Nothing to migrate; listed here so nobody "finds the gap" again.
