"""Preflight check, validate config and connectivity BEFORE running the migration.

Run:  python preflight.py [config.json]

Checks, in order:
  1. Config file parses; required keys present, no placeholder values
  2. Zephyr Essential API auth works (GET /healthcheck + /projects) and every
     projects.import key exists and is Zephyr-enabled on the tenant
  3. Per-project data sanity: test case / cycle / execution counts (a zeroed
     project usually means seed data is missing, not an API problem)
  4. Qase API auth works (GET /v1/project)

Exit code 0 = all green; 1 = at least one failure.
"""

import json
import os
import sys

import requests

from src.support.config_manager import ConfigManager
from src.support.logger import Logger
from src.support.zephyr_session import SESSION_MAX_AGE_DAYS
from src.api.zephyr_essential import ZephyrEssentialApiClient
from src.exceptions.api import APIError

_PLACEHOLDER_MARKERS = ("<", ">", "your-", "YOUR_", "changeme", "xxxx")

_results = []


def _report(name: str, ok: bool, detail: str = ""):
    icon = "✅" if ok else "❌"
    print(f"  {icon} {name}" + (f": {detail}" if detail else ""))
    _results.append(ok)


def _warn(name: str, detail: str = ""):
    print(f"  ⚠️  {name}" + (f": {detail}" if detail else ""))


def _looks_placeholder(value: str) -> bool:
    return any(marker in value for marker in _PLACEHOLDER_MARKERS)


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "./config.json"

    print("\n- Config -")
    if not os.path.exists(config_path):
        _report(f"Config file {config_path}", False, "not found, copy config.example.json")
        return _finish()
    try:
        with open(config_path) as f:
            json.load(f)
    except json.JSONDecodeError as e:
        _report(f"Config file {config_path}", False, f"invalid JSON: {e}")
        return _finish()
    _report(f"Config file {config_path}", True, "parses OK")

    config = ConfigManager(config_file=config_path)
    config.load_config()

    required = {
        "qase.api_token": "Qase API token",
        "zephyr.access_token": "Zephyr access token (Jira → Settings → Apps → Zephyr API Access Tokens)",
    }
    config_ok = True
    for key, label in required.items():
        value = str(config.get(key) or "").strip()
        if not value:
            _report(f"{key} ({label})", False, "missing/empty")
            config_ok = False
        elif _looks_placeholder(value):
            _report(f"{key} ({label})", False, f"looks like a placeholder: {value[:40]!r}")
            config_ok = False
        else:
            _report(f"{key} ({label})", True)

    import_all = bool(config.get("projects.import_all"))
    projects = [str(p).strip() for p in (config.get("projects.import") or []) if str(p).strip()]
    exclude = [str(p).strip() for p in (config.get("projects.exclude") or []) if str(p).strip()]
    if import_all:
        _report(
            "projects.import_all", True,
            "every Zephyr-enabled project on the tenant"
            + (f", excluding {exclude}" if exclude else ""),
        )
    elif not projects:
        _report(
            "projects.import", False,
            "empty, list the Jira project keys to migrate (or set projects.import_all: true)",
        )
        config_ok = False
    else:
        overlap = sorted(set(projects) & set(exclude))
        if overlap:
            _report(
                "projects.import", False,
                f"{overlap} appear in BOTH projects.import and projects.exclude, "
                f"remove them from one side",
            )
            config_ok = False
        else:
            _report("projects.import", True, f"{len(projects)} project key(s): {projects}")

    # users.map: {atlassian_account_id: qase_user_id} — values must be ints
    users_map = config.get("users.map") or {}
    if users_map:
        bad = {k: v for k, v in users_map.items() if not str(v).isdigit()}
        if bad:
            _report("users.map", False, f"non-numeric Qase user id(s): {bad}")
            config_ok = False
        else:
            _report("users.map", True, f"{len(users_map)} account id(s) mapped")

    # Status / priority overrides must map to valid Qase slugs
    _valid_result = {"passed", "failed", "blocked", "skipped", "invalid", "in_progress", "untested"}
    _valid_priority = {"high", "medium", "low"}
    for cfg_key, valid, label in (
        ("runs.status_map", _valid_result, "Qase result status"),
        ("cases.priority_map", _valid_priority, "Qase priority"),
    ):
        mapping = config.get(cfg_key) or {}
        bad = {k: v for k, v in mapping.items() if str(v).strip().lower() not in valid}
        if bad:
            _report(cfg_key, False, f"invalid {label} slug(s): {bad}, valid: {sorted(valid)}")
            config_ok = False
        elif mapping:
            _report(cfg_key, True, f"{len(mapping)} override(s)")

    _pid = config.get("cases.preserve_ids")
    preserve_ids = True if _pid is None else bool(_pid)
    if config.get("migration.delta") and not preserve_ids:
        _report(
            "migration.delta", False,
            "requires cases.preserve_ids: true (preserved ids are the delta match key; "
            "the original migration must have used them too)",
        )
        config_ok = False

    if not config_ok:
        return _finish()

    # ---------------- Browser session (attachments, optional) ----------------
    if config.get("zephyr.attachments.enabled"):
        print("\n- Attachment browser session -")
        session_file = str(config.get("zephyr.attachments.session_file") or "zephyr_session.json")
        if not os.path.exists(session_file):
            _report(
                "Session file", False,
                f"{session_file} not found, run `python capture_session.py` "
                f"(attachments will fall back to filename notes)",
            )
        else:
            try:
                with open(session_file) as f:
                    session = json.load(f)
                cookies = session.get("cookies") or []
                names = {c.get("name") for c in cookies}
                have_session = "cloud.session.token" in names or "tenant.session.token" in names
                _report(
                    "Session file", have_session,
                    f"{session_file}: {len(cookies)} cookie(s)"
                    + ("" if have_session else ": no Atlassian session cookie; re-capture"),
                )
                import time as _time
                age_days = (_time.time() - os.path.getmtime(session_file)) / 86400
                max_age = SESSION_MAX_AGE_DAYS
                if age_days > max_age:
                    _warn(
                        f"Session file is {age_days:.0f} days old",
                        f"older than {max_age:.0f} days, Atlassian sessions "
                        f"expire in ~2-4 weeks; re-run `python capture_session.py` if downloads fail",
                    )
            except (ValueError, OSError) as e:
                _report("Session file", False, f"{session_file} unreadable: {e}")
        if not str(config.get("zephyr.attachments.app_page_url") or "").strip():
            _warn(
                "zephyr.attachments.app_page_url not set",
                "token re-mint mid-run will fall back to deriving the URL; "
                "capture_session.py records it automatically",
            )

    logger = Logger(level="error", write_to_file=False)

    # ---------------- Zephyr Essential ----------------
    print("\n- Zephyr Essential -")
    client = ZephyrEssentialApiClient(
        token=str(config.get("zephyr.access_token")),
        logger=logger,
        base_url=str(config.get("zephyr.base_url") or "") or None,
        max_retries=1,
    )
    try:
        # GET /healthcheck returns 200 with an EMPTY body — don't parse JSON
        resp = requests.get(
            f"{client.base_url}/healthcheck",
            headers=client.headers,
            timeout=(15, 30),
        )
        if resp.status_code == 200:
            _report("API healthcheck", True, client.base_url)
        else:
            raise APIError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    except (APIError, requests.exceptions.RequestException) as e:
        _report("API healthcheck", False, f"{client.base_url} → {str(e)[:200]}")
        _warn(
            "Hint",
            "401 here usually means the token was revoked or belongs to a different "
            "Jira site; regenerate it in Jira → Settings → Apps → Zephyr API Access Tokens",
        )
        return _finish()

    try:
        tenant_projects = {p.get("key"): p for p in client.get_projects()}
        _report(
            "GET /projects",
            True,
            f"{len(tenant_projects)} Zephyr-enabled project(s): {sorted(tenant_projects)}",
        )
    except APIError as e:
        _report("GET /projects", False, str(e)[:200])
        return _finish()

    unknown_excludes = sorted(set(exclude) - set(tenant_projects))
    if unknown_excludes:
        _warn(
            f"projects.exclude key(s) not on the tenant: {unknown_excludes}",
            "harmless, but check for typos",
        )
    if import_all:
        projects = [k for k in sorted(tenant_projects) if k not in exclude]
        if not projects:
            _report(
                "Resolved project list", False,
                "projects.import_all resolved to zero projects "
                + (f"(everything excluded: {exclude})" if exclude else
                   "(no Zephyr-enabled projects on this tenant)"),
            )
            return _finish()
        _report("Resolved project list", True, f"{len(projects)} project(s): {projects}")
    else:
        projects = [k for k in projects if k not in exclude]

    for key in projects:
        proj = tenant_projects.get(key)
        if proj is None:
            _report(f"Project {key}", False, "not found / not Zephyr-enabled on this tenant")
            continue
        if not proj.get("enabled", True):
            _report(f"Project {key}", False, "Zephyr is disabled for this project")
            continue
        # Data sanity: counts per entity (total field comes back on page 1)
        counts = {}
        for label, path in (
            ("cases", "testcases"),
            ("cycles", "testcycles"),
            ("executions", "testexecutions"),
            ("folders", "folders"),
        ):
            try:
                data = client._get(path, {"projectKey": key, "maxResults": 1})
                counts[label] = data.get("total", "?")
            except APIError:
                counts[label] = "ERR"
        detail = ", ".join(f"{k}={v}" for k, v in counts.items())
        _report(f"Project {key}", True, detail)
        if counts.get("cases") == 0:
            _warn(f"Project {key} has 0 test cases", "seed data missing? (not an API problem)")

    # ---------------- Jira (optional name enrichment) ----------------
    print("\n- Jira (optional) -")
    jira_keys = {
        "jira.base_url": str(config.get("jira.base_url") or "").strip(),
        "jira.email": str(config.get("jira.email") or "").strip(),
        "jira.api_token": str(config.get("jira.api_token") or "").strip(),
    }
    n_set = sum(1 for v in jira_keys.values() if v and not _looks_placeholder(v))
    if n_set == 0:
        _warn(
            "Jira enrichment disabled",
            "without jira.* config: project titles fall back to keys, component "
            "tags are dropped, runs lose version context. Add jira.base_url/email/"
            "api_token to enable.",
        )
    elif n_set < 3:
        _report(
            "Jira enrichment config",
            False,
            f"partially configured, set all three of {list(jira_keys)} or none",
        )
    else:
        from src.api.jira_lookup import JiraLookupClient

        jira = JiraLookupClient(
            jira_keys["jira.base_url"],
            jira_keys["jira.email"],
            jira_keys["jira.api_token"],
            logger,
            max_retries=1,
        )
        try:
            me = jira.get_myself()
            _report("Jira auth (GET /myself)", True, f"{me.get('displayName')}")
            for key in projects:
                try:
                    name = (jira.get_project(key).get("name") or "").strip()
                    _report(f"Jira project {key}", True, f"name={name!r}")
                except APIError as e:
                    _report(f"Jira project {key}", False, str(e)[:200])
        except APIError as e:
            _report("Jira auth (GET /myself)", False, str(e)[:200])

    # ---------------- Qase ----------------
    print("\n- Qase -")
    from src.service.qase import qase_api_url, is_dedicated_cluster

    qase_host = str(config.get("qase.host") or "qase.io")
    api_url = qase_api_url(config)
    if is_dedicated_cluster(qase_host):
        _report("Qase host", True, f"{qase_host} treated as a dedicated cluster: {api_url}")
    try:
        resp = requests.get(
            f"{api_url}/v1/project",
            headers={"Token": str(config.get("qase.api_token"))},
            params={"limit": 1},
            timeout=(15, 30),
        )
        if resp.status_code == 200 and (resp.json() or {}).get("status"):
            total = ((resp.json().get("result") or {}).get("total")) or 0
            _report("Qase auth (GET /v1/project)", True, f"{total} project(s) in workspace")
        else:
            _report(
                "Qase auth (GET /v1/project)", False, f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
    except requests.exceptions.RequestException as e:
        _report("Qase auth (GET /v1/project)", False, str(e)[:200])

    return _finish()


def _finish():
    failed = _results.count(False)
    print()
    if failed:
        print(f"❌ Preflight FAILED, {failed} check(s) failed. Fix the items above before migrating.")
        sys.exit(1)
    print("✅ Preflight passed, ready to run: python start.py")
    sys.exit(0)


if __name__ == "__main__":
    main()
