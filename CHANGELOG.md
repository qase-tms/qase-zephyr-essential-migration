# Changelog

Notable changes to this migration, newest first. Versions follow
[semantic versioning](https://semver.org) read from the customer's side: a
**major** bump means your `config.json` needs changing, a **minor** means new
capability that needs no action from you, and a **patch** means fixes and
documentation.

## 1.0.0 — 2026-09-22

First release as an official Qase product. The migration itself predates this
version; 1.0.0 marks the point at which it became supported software with a
documented configuration, a validation step and a support address.

### Added
- `python preflight.py` validates your configuration and both APIs before a
  migration touches anything, so a wrong token or project key is a message in
  the first ten seconds rather than a failure half an hour in.
- `--dry-run` reads everything from the source and reports exactly what would
  be created, writing nothing to Qase.
- An end-of-run **migration report** listing everything skipped, degraded or
  failed, with the reason. Counts alone can overstate success: a run that
  quietly dropped a thousand attachments still reports a large number of
  migrated cases.
- Five logging levels (`error`, `warn`, `info`, `verbose`, `debug`). Warnings
  and errors always reach the console, so a run that lost data cannot look
  clean in the terminal.
- Tokens may be supplied by environment variable instead of `config.json`, so a
  config file pasted into a support ticket carries no secrets.
- The version is written as the first line of every log file, so the log you
  send us already says which release produced it.

### Fixed
- Results are sent to Qase in batches of **200**. Qase lowered the server-side
  bulk limit, and a run with more than 200 results previously failed outright.
- A missing or malformed `config.json` now fails immediately with a readable
  message, instead of running with empty values and failing as a `401` partway
  through.

### Changed
- **Python 3.11 or newer is required.** 3.10 reaches end of life in October
  2026. Both entry points refuse to start on an older interpreter rather than
  failing midway.
