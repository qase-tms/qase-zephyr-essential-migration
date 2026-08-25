# Security Policy

## Reporting a vulnerability

**Do not open a public issue or discussion, and do not email a vulnerability report to the general migrations address.**

Report privately through GitHub's private vulnerability reporting on this repository: **Security > Report a vulnerability**. This creates a private advisory visible only to the maintainers.

Please include the affected version or commit, what an attacker could achieve, and the steps to reproduce.

We will acknowledge the report and keep you updated until it is resolved.

## What this tool handles

This script reads from Zephyr Essential and Jira, and writes into Qase. Understanding what it touches will help you judge whether something is a vulnerability:

- **Credentials.** A Zephyr API access token and a Qase API token are read from `config.json`, and a Jira email and API token where attachments require it. They are held in memory for the duration of the run and are never written to logs at any level.
- **Source systems are read-only.** The migration never writes to Zephyr or Jira. Every call against them is a read.
- **Customer data.** Test cases, cycles, executions, attachments and user identities pass through the process. Logs written to `logs/` and statistics written to `stats/` can contain test case content, account identifiers and internal URLs.
- **Browser session capture.** Attachment migration can optionally reuse a captured browser session, stored locally as a session file. It holds authenticated cookies for the customer's Atlassian tenant. Treat that file as a credential: it is gitignored, and it should be deleted once the migration is complete.
- **Local artifacts.** `config.json`, `logs/`, `stats/` and any session file are gitignored and must never be committed. Delete them once a migration is complete.

## Handling your own credentials

- Use tokens scoped to the minimum permissions the migration needs. The Zephyr and Jira credentials only ever need read access.
- Revoke the tokens used for a migration once it is finished.
- Never commit `config.json`. It is gitignored, but a file added under a different name will not be.
