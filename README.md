# Zephyr Essential to Qase Migration

Migrates test data from **Zephyr Essential** for Jira Cloud (the post-2025 SmartBear product, formerly Zephyr Squad) into [Qase](https://qase.io).

Free to use for every paying and trialing Qase customer. If you would rather we ran it for you, including adapting it to your data, see [Getting help](#12-getting-help).

---

## 1. What this migrates

Reads Zephyr Essential over its REST API, optionally enriched by read-only Jira metadata lookups, and recreates folders, test cases, cycles and execution history in Qase.

### Which Zephyr is this for?

- **Zephyr Essential** (Jira Cloud app v10+). Its REST API (v2.9) lives at `https://prod-api.zephyr4jiracloud.com/v2` and is schema-compatible with Zephyr Scale Cloud v2: test cases, folders, cycles and executions are native Zephyr entities.
- **Not** classic Zephyr Squad Cloud (v1 / ZAPI), where a test case is a Jira issue. Use `qase-zephyr-squad-migration`.
- **Not** Zephyr Scale. Use `qase-zephyr-scale-migration`.
- **Not** Zephyr on Data Center or Server. Those expose a different, self-hosted ZAPI surface that no current script speaks.

Not sure which you have? If your test cases appear in Jira as issues with their own keys, you are on classic Squad. If they live in a Zephyr panel with their own folder tree, you are here.

### Safety: what this touches

**The source is strictly read-only.** The script cannot delete or modify anything in Jira or Zephyr, by construction and verified by audit (July 2026):

- Every HTTP request against a source host is a `GET`. There is no `POST`, `PUT`, `DELETE` or `PATCH` aimed at Jira or Zephyr anywhere in the code.
- The Jira client's entire surface is five read lookups: `/myself`, project, components and versions metadata, and a JQL id-to-key search.
- The optional headless browser used for attachments only navigates and reads cookies. It never clicks, fills or submits. Loading the page is equivalent to opening it in a tab.

**The target is never damaged.** The script never deletes anything in Qase, and the worst case is additive:

- A project that already contains cases, suites, runs or milestones is **refused** and reported, unless `migration.allow_existing_target` is set explicitly.
- Custom fields the script creates, including "Jira Links", are **scoped to the target projects only**. They do not appear in the customer's other projects. Where a field of the same name already exists, its scope is appended to, never shrunk, never flipped workspace-global.
- Everything else written is project-scoped to the targets.

### How the API surface was verified

Against the official OpenAPI spec at `https://api.swaggerhub.com/apis/smartbear-public/zephyr-squad-cloud-api/2.9/swagger.json`, linked from [developer.smartbear.com/zephyr-squad](https://developer.smartbear.com/zephyr-squad/default/introduction), cross-checked against a live Essential 10.x tenant. Auth, pagination and entity shapes confirmed July 2026.

## 2. Coverage table

| Zephyr Essential | Qase | Status | Notes |
|---|---|---|---|
| Project | Project | Full | Matched by title, or via `projects.mapping` |
| Test case folders (tree) | Suites | Full | Nested folders become nested suites |
| Test plans | Milestones | Full | |
| Test case | Test case | Full | Name, objective, precondition, labels, priority, status, custom fields. Component becomes a tag |
| Test steps | Case steps | Full | Step, test data, expected result |
| BDD or plain-text script | Case description | Partial | Rendered as a fenced code block |
| Test cycle | Test run | Full | |
| Execution | Run result | Full | Status, comment, actual end date, execution time |
| Execution step results | Result steps | Full | Positional, always migrated |
| Environments | Environments | Partial | Set on the run when the whole cycle shares one, noted per result when mixed |
| Case to Jira issue links | External issues | Partial | Native `jira-cloud` issues when the Qase Jira integration is connected, otherwise a "Jira Links" custom field |
| Case web links | Case description | Full | As markdown links |
| Cycle links | Run description | Full | As markdown notes |
| Execution links | Result comment | Full | As markdown notes |
| Call-to-test steps | Text step | Partial | Rendered as `-> Call to test case KEY`. Qase bulk create cannot reference shared steps |
| Attachments | Attachments | Opt-in | Off by default, see section 3 |
| Per-case estimated time | | Not supported | No Qase bulk field |
| Users | | Not migrated | Attributed via `users.map`, else `users.default` |
| Dashboards, reports, saved filters | | Not supported | No Qase equivalent |

### Mapping notes

- **Statuses.** Pass to passed, Fail to failed, Blocked to blocked. In Progress and Not Executed are left untested. Custom statuses map via `runs.status_map`, otherwise untested plus a console warning.
- **Priorities.** Qase offers exactly high, medium and low. Highest and High map to high, Medium and Normal to medium, Low and Lowest to low. Custom priorities map via `cases.priority_map`, otherwise medium plus a warning.
- **Elapsed time.** `actualEndDate` minus `actualStartDate` where available, otherwise `executionTime` in milliseconds. Essential does not return `actualStartDate`, so `executionTime` is the usual source.
- **Custom fields.** The Essential API has no `/customfields` endpoint, so definitions are discovered by scanning test case payloads and typed by inference. Multi-select values arriving as `[{"id":…,"name":…}]` are serialised by name.
- **Step-level results.** Anything outside passed, failed or blocked becomes a skipped step. If Qase rejects a run's step payload, the results are re-sent once without steps rather than losing the run.

## 3. Known limitations

**Attachment binaries are not migrated by default.** The Essential API exposes attachment *metadata* but denies the binary to the API access token, verified as `401 Permission denied`, and the metadata never includes a downloadable URL. The binaries are reachable only through Zephyr's browser-facing channels: the app's internal backend issues pre-signed S3 URLs, and inline images sit on a CDN. By default, filenames are recorded on the case description, run description or result comment with a console warning per affected entity, so nothing disappears silently. Migrating the binaries is an explicit opt-in with real trade-offs, documented in [ATTACHMENTS-AUTOMATION.md](ATTACHMENTS-AUTOMATION.md).

**Users are not migrated.** This script creates no users and no groups. Executions and cases are attributed through `users.map`, falling back to `users.default`. Note also that the Qase results API attributes every API-created result to the token owner regardless, so result authorship cannot be preserved by any script.

**Per-case estimated time is not migrated.** The Qase bulk case endpoint has no field for it.

**Call-to-test steps become text.** A step referencing another case is rendered as `-> Call to test case KEY`, because Qase bulk create cannot reference shared steps. Native shared-step conversion is tracked in `BACKLOG.md`.

**Re-running duplicates data** unless you use delta mode. See section 10.

**Jira metadata is optional but affects fidelity.** Without Jira credentials, project titles fall back to the project key, component tags are dropped, and runs lose version context. Everything else migrates identically.

## 4. Prerequisites

### Zephyr Essential

1. In Jira, go to **Settings (gear) > General Settings > Apps > Zephyr API Access Tokens**.
2. Choose **Create access token**. This is `zephyr.access_token`.
3. Note the Jira project keys you intend to migrate. They must be Zephyr-enabled.

### Qase

1. Create an API token at **Workspace > API tokens**. This is `qase.api_token`.
2. Decide which Qase user should own anything the migration cannot attribute. Its email address is `users.default`.
3. For native Jira issue links, the **Qase to Jira Cloud integration must be connected** in the workspace. Qase validates every issue key against it. Without the integration, links are written to a "Jira Links" custom field instead, so they are never lost.

### Jira, optional but recommended

1. Create a token at [id.atlassian.com > Security > API tokens](https://id.atlassian.com/manage-profile/security/api-tokens).
2. Fill in `jira.base_url`, `jira.email` and `jira.api_token`.

The Essential API returns project name, components and fixVersions as bare Jira id links. With Jira credentials the script resolves them into proper Qase project titles, component tags and `[Release] Cycle` run titles. Only `/myself` and project metadata endpoints are ever called.

### Before you run

| What | Why |
|---|---|
| An empty target Qase project | The script refuses a populated target unless told otherwise, section 10 |
| `cases.preserve_ids` left on | Required if you ever intend a delta top-up, section 10 |

## 5. Install

Requires **Python 3.10 or newer**.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

## 6. Configure

Edit `config.json`. Every key below is read by the code, and every key the code reads is listed here.

### Qase

| Key | Required | Default | Meaning |
|---|---|---|---|
| `qase.api_token` | yes | | API token from **Workspace > API tokens** |
| `qase.host` | yes | `qase.io` | Leave as-is unless you are on a dedicated cluster |
| `qase.ssl` | no | `true` | Use HTTPS |
| `qase.dedicated_cluster` | no | `false` | Set only if Qase runs on your own dedicated cluster with its own hostname. Unrelated to the Qase Enterprise plan |

### Zephyr

| Key | Required | Default | Meaning |
|---|---|---|---|
| `zephyr.access_token` | yes | | Zephyr API access token |
| `zephyr.base_url` | yes | `https://prod-api.zephyr4jiracloud.com/v2` | |
| `zephyr.attachments.enabled` | no | `false` | Opt in to migrating attachment binaries, see [ATTACHMENTS-AUTOMATION.md](ATTACHMENTS-AUTOMATION.md) |
| `zephyr.attachments.session_file` | no | `./zephyr_session.json` | Where the captured browser session is stored. **Treat it as a credential and delete it after the migration** |
| `zephyr.attachments.app_page_url` | no | | **Written automatically by `capture_session.py`.** Leave it empty; you never fill this in by hand |

### Jira, optional

| Key | Required | Default | Meaning |
|---|---|---|---|
| `jira.base_url` | no | | For example `https://your-site.atlassian.net` |
| `jira.email` | no | | Atlassian account email |
| `jira.api_token` | no | | Atlassian API token |

### Selection

| Key | Required | Default | Meaning |
|---|---|---|---|
| `projects.import_all` | yes | `false` | Migrate every Zephyr-enabled project. When `false`, `projects.import` must list keys |
| `projects.import` | yes unless `import_all` | | Jira project keys |
| `projects.exclude` | no | `[]` | Subtracted from either form |
| `projects.mapping` | no | `{}` | Zephyr project key to an existing Qase project code |
| `runs.created_after` | no | `0` | Unix timestamp. Cycles older than this are skipped |

### Cases, runs and users

| Key | Required | Default | Meaning |
|---|---|---|---|
| `cases.preserve_ids` | no | `true` | Send the Zephyr key suffix as the Qase case id. Required for delta migrations |
| `cases.priority_map` | no | `{}` | Custom Zephyr priority to `high`, `medium` or `low` |
| `runs.status_map` | no | `{}` | Custom Zephyr status to a Qase result status |
| `users.default` | yes | | Email address, or numeric id, of the Qase user owning anything unmatched |
| `users.map` | no | `{}` | Atlassian account id to a Qase user id |

### Migration behavior

| Key | Required | Default | Meaning |
|---|---|---|---|
| `migration.allow_existing_target` | no | `false` | Permit importing into a Qase project that already contains data. **Produces duplicates**, section 10 |
| `migration.delta` | no | `false` | Add-only top-up against an already-migrated project, section 10 |

### Logging and output

| Key | Required | Default | Meaning |
|---|---|---|---|
| `logging.level` | no | `info` | `error`, `warn`, `info`, `verbose` or `debug`. Each includes the ones before it |
| `logging.write_to_file` | no | `true` | Write a log file under `logging.dir` |
| `logging.dir` | no | `./logs` | |
| `prefix` | no | | Prefix for log and statistics filenames |

## 7. Validate

Always run this first. It is read-only and writes nothing.

```bash
python preflight.py
```

It validates the config, pings both APIs, checks that every project exists and is Zephyr-enabled, and reports per-project data counts so a zeroed project is caught before the run rather than after. Exit code is `0` when everything passes and `1` otherwise.

## 8. Run

```bash
python preflight.py          # validate first
python start.py --dry-run    # full read-only rehearsal, writes nothing to Qase
python start.py              # the real migration
```

`--dry-run` runs the entire pipeline against the real source, so unmapped statuses, missing fields, oversized values and attachment problems all surface, while writing nothing to Qase. Use it before any migration you care about. `QASE_DRY_RUN=1` does the same.

**Expected duration.** Minutes for a few hundred cases. Attachments, when enabled, dominate everything else.

## 9. What good output looks like

Progress is printed per stage with a running count, and a green tick replaces the arrow as each stage completes.

```
	↪ Importing projects [3/3]
	↪ Importing custom fields [14/14]
	↪ Importing suites [62/62]
	↪ Importing test cases [1840/1840]
	↪ Importing cycles [96/96]
	↪ Importing executions [4127/4127]
```

**Warnings and errors print in colour as they happen**, even at the default logging level, so a skipped item is visible while the run is still going:

```
	! [warn] [ZEM][Cases] Priority 'Critical' is not mapped, defaulting to medium
	✗ [error] [ZEM][Runs] Cycle 'Regression 4.2' rejected by Qase, retried without steps
```

At the end, two blocks. First the counts, source against target:

```
------ Stats ------

{'projects': {'ZEM': {'test_cases': {'zephyr-essential': 1840, 'qase': 1840},
                      'runs':       {'zephyr-essential': 96,   'qase': 96},
                      'results':    {'zephyr-essential': 4127, 'qase': 4127}}},
 'attachments': {'zephyr-essential': 212, 'qase': 0},
 'custom_fields': {'zephyr-essential': 14, 'qase': 14}}
```

Then the migration report, listing everything skipped or degraded, with the reason:

```
------ Migration report: 7 skipped/degraded item(s) ------

  [ZEM] · 7 item(s)
    ! [cases] Priority 'Critical' is not mapped, defaulting to medium (x4)
    ! [attachments] 212 attachment(s) not migrated, filenames noted in descriptions
    ! [runs] Cycle 'Regression 4.2' re-sent without step results
    · [cases] 3 call-to-test step(s) rendered as text
```

**A clean run says so explicitly:**

```
------ Migration report: no skipped or degraded items ------
```

That line is the one to look for. An empty report is not the same as no report, so the script always prints one.

Artifacts written afterwards:

- `logs/<prefix>_zephyr_essential_<timestamp>.log`, the full log at your configured level
- `stats/<prefix>_stats.json` and `stats/<prefix>_stats.xlsx`, the counts and the full issue list, including any items the on-screen report truncated

**Before you call the migration done:** compare the counts against Zephyr, and read the migration report rather than only the counts. A high case count with 212 skipped attachments is a successful-looking run that is missing data.

## 10. Re-run and resume behavior

**Read this before running twice.**

Projects dedup by title, and custom fields and environments dedup by name. **Suites, cases, runs and results are create-only**, so a plain re-run duplicates them. Because of that the script **refuses to import into a Qase project that already contains cases** and says so in the report. Set `migration.allow_existing_target` only when you accept duplicates.

### Delta migrations

`migration.delta: true` turns a re-run into an **add-only top-up**, which is the standard cutover pattern: trial migration now, keep working in Zephyr, final delta at switch-over.

- A target that does not exist yet, or is empty, migrates in full. Delta never restricts scope; `projects.import` does.
- A pre-existing target is read first, then only what is missing is created. **Cases** match by preserved id (`ZEM-T123` to case `123`), which is why `cases.preserve_ids` must have been on for the original run too. **Suites** match by title and parent, **milestones** by title, **runs** by final run title.
- Nothing is ever updated or deleted.

Honest limits, which are source-API constraints rather than shortcuts:

- **Edits to already-migrated cases are not re-synced.** The Essential API exposes no modified date, so change detection is impossible. Freeze case editing during the migration window, which is normal cutover practice.
- **Executions added to an already-migrated cycle are not re-synced.** The run is matched by title and skipped whole. Entirely new cycles come over completely.
- Suite and run matching assumes the Qase side was not renamed after the first migration. Renamed entities would be recreated under their source names.

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Config file not found` | No `config.json` | `cp config.example.json config.json` and fill it in |
| `401` from Zephyr | Wrong or expired access token | Regenerate at **Settings > Apps > Zephyr API Access Tokens** |
| `401` from Qase | Wrong or revoked token | Check `qase.api_token`, then run `python preflight.py` |
| Project skipped, "already contains" | Target has data | Use an empty project, or `migration.delta`, or `migration.allow_existing_target` |
| Project reports zero cases | Not Zephyr-enabled, or the key is wrong | Preflight reports per-project counts. Check the key in Jira |
| Statuses all untested | Custom Zephyr statuses | Map them in `runs.status_map`. Warnings name the unmapped values |
| Priorities all medium | Custom Zephyr priorities | Map them in `cases.priority_map` |
| Jira links in a custom field, not native | The Qase Jira Cloud integration is not connected | Connect it in Qase, or accept the custom field |
| Project titles are just keys | No Jira credentials | Fill in the `jira` block |
| Attachments missing, filenames noted | Expected default | See [ATTACHMENTS-AUTOMATION.md](ATTACHMENTS-AUTOMATION.md) |
| Duplicates after a second run | Expected without delta | See section 10 |

## 12. Getting help

Email **migrations@qase.io**.

GitHub Issues and Discussions are disabled on this repository, so email is the way to reach us.

To get a useful answer on the first reply, include:

- What you ran, and the output of `python preflight.py`
- Your `config.json` **with every token removed**
- The relevant part of the log from `logs/`, again with tokens removed
- Roughly how many projects, cases and executions are involved

Please do not send API tokens or customer data. If a log is large, describe the error and we will tell you what to send.

**Want us to run it?** A fully managed migration, including adapting the script to your data structures, is available as a paid service. Email the same address.
