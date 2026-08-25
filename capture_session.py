#!/usr/bin/env python3
"""
Capture a Zephyr Essential browser session, run this ONCE (about monthly).

Attachment binaries in Zephyr Essential are reachable only through Zephyr's own
browser-facing channels. This helper opens a real browser so you can log in to
Jira/Zephyr; it then saves your *durable* Atlassian session to disk AND records
the exact Zephyr app-page URL you ended on. Migration runs replay that session
headlessly and revisit that URL to mint a fresh short-lived Forge token each run,
so you never hand-capture anything per run, the manual login is only when the
~2–4-week Atlassian session lapses.

Usage:
    pip install playwright
    playwright install chromium
    python capture_session.py

It reads `zephyr.attachments.session_file` and `jira.base_url`
(or `jira.base_url`) from config.json, opens a browser at your Jira site, and -
crucially, you must **navigate into the Zephyr app** (open a project's Zephyr
section / a test case) before pressing ENTER, so the current page IS the Zephyr
app page. It saves the session and writes that page's URL to
`zephyr.attachments.app_page_url` in config.json for the migration to revisit.

Capturing a session does NOT by itself turn attachment migration on: you must
also set `zephyr.attachments.enabled = true`, which is an explicit, disclosed
opt-in (see the README "Attachments" section).

The saved file is a full session credential: it is gitignored, and you should
treat it like config.json (never commit or share it).
"""

import base64
import json
import os
import re
import sys

# Jira Forge project-page URL, e.g.
# https://site/jira/software/projects/KEY/apps/{appId}/{envId}
_APP_PAGE_RE = re.compile(r"/jira/\w+/projects/[^/]+/apps/[0-9a-fA-F-]+/[0-9a-fA-F-]+")


def _jwt_claims(token: str) -> dict:
    """Decode a JWT payload (no verification). {} on any error."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _app_url_from_pages(urls) -> str:
    """Return the first open tab that is a real Zephyr Forge app page, or ''."""
    for u in urls or []:
        if u and "atlassian.net" in u and "id.atlassian.com" not in u and _APP_PAGE_RE.search(u):
            return u.split("?")[0]
    return ""


def _derive_app_url(session_file: str, cfg: dict) -> str:
    """Reconstruct the Zephyr app-page URL deterministically from the captured
    session (app id + env id live in the minted ``Bearer`` cookie) plus config
    (Jira site + project key). Returns '' if any piece is missing.

    Robust fallback for when no app-page tab was captured. The only assumption
    is the ``/jira/software/`` segment (the common project type); a captured
    browser URL, when available, is preferred because it carries the real one.
    """
    try:
        session = json.load(open(session_file))
    except Exception:
        return ""
    bearer = next(
        (c["value"] for c in session.get("cookies", [])
         if c.get("name") == "Bearer" and "zephyr4jiracloud" in c.get("domain", "")),
        None,
    )
    if not bearer:
        return ""
    cl = _jwt_claims(bearer)
    app_id = str(cl.get("aud", "")).split("/")[-1] or None
    env_id = None
    # context.localId = ari:cloud:ecosystem::extension/{appId}/{envId}/static/...
    parts = str((cl.get("context") or {}).get("localId") or "").split("/")
    if len(parts) >= 3 and parts[0].endswith("extension"):
        app_id = app_id or parts[1]
        env_id = parts[2]
    if not env_id:
        env_id = (_jwt_claims(cfg.get("zephyr", {}).get("access_token") or "")
                  .get("context") or {}).get("environmentId")
    site = (cfg.get("jira", {}).get("base_url")
            or cfg.get("zephyr", {}).get("attachments", {}).get("app_url") or "").rstrip("/")
    imported = cfg.get("projects", {}).get("import") or []
    key = imported[0] if imported else None
    if app_id and env_id and site and key:
        return f"{site}/jira/software/projects/{key}/apps/{app_id}/{env_id}"
    return ""


def _load_config():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "config.json")
    if not os.path.exists(path):
        print("ERROR: config.json not found. Copy config.example.json first.")
        sys.exit(1)
    with open(path) as f:
        cfg = json.load(f)
    return cfg, here


def main():
    cfg, here = _load_config()
    zephyr = cfg.get("zephyr", {})
    attachments = zephyr.get("attachments", {})
    jira = cfg.get("jira", {})

    session_file = (attachments.get("session_file") or "zephyr_session.json").strip()
    if not os.path.isabs(session_file):
        session_file = os.path.join(here, session_file)

    app_url = (jira.get("base_url") or "").strip().rstrip("/")
    if not app_url:
        print("ERROR: set `jira.base_url` in config.json "
              "(your Jira site, e.g. https://your-org.atlassian.net).")
        sys.exit(1)

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("ERROR: Playwright is not installed. Run:")
        print("    pip install playwright && playwright install chromium")
        sys.exit(1)

    print(f"\nOpening a browser at: {app_url}")
    print("→ Log in to Jira, then OPEN THE ZEPHYR APP, a project's Zephyr section")
    print("  or a test case, so the browser is ON the Zephyr app page. This page's")
    print("  URL is what every run revisits to mint a token. THEN press ENTER.\n")

    app_page_url = ""
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=False)
        except Exception as e:
            print(f"ERROR: could not launch Chromium ({e}).")
            print("Run `playwright install chromium` and try again.")
            sys.exit(1)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(app_url, wait_until="domcontentloaded", timeout=90000)
        except Exception:
            # Non-fatal: user can still navigate manually in the open window.
            pass
        try:
            input("Press ENTER here once you are logged in and viewing the Zephyr app… ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted, nothing saved.")
            browser.close()
            sys.exit(1)
        # Save the session first, then find the Zephyr app-page URL two ways:
        # (1) a real app-page tab open in the browser (authoritative — carries
        # the exact project-type segment); (2) derived from the captured session
        # (robust when no such tab is found, e.g. the app opened on its own CDN
        # origin or the tracked tab drifted to a login redirect).
        context.storage_state(path=session_file)
        browser_url = _app_url_from_pages([pg.url for pg in context.pages])
        browser.close()

    derived_url = _derive_app_url(session_file, cfg)
    app_page_url = browser_url or derived_url
    source = "detected from the open Zephyr tab" if browser_url else (
        "derived from the captured session" if derived_url else "unavailable")

    # Persist the app-page URL so the migration can revisit it to mint tokens.
    cfg.setdefault("zephyr", {}).setdefault("attachments", {})["app_page_url"] = app_page_url
    with open(os.path.join(here, "config.json"), "w") as f:
        json.dump(cfg, f, indent=4)

    print(f"\n✓ Session saved to: {session_file}")
    if app_page_url:
        print(f"✓ App page URL recorded ({source}):")
        print(f"    {app_page_url}")
        if not browser_url and derived_url:
            print("  (derivation assumes a '/jira/software/' project; if file attachments")
            print("   don't download, open a Zephyr test case during capture so the exact")
            print("   URL is detected, or set zephyr.attachments.app_page_url by hand.)")
    else:
        print("  ⚠ Could not determine the Zephyr app-page URL. Re-run this and be sure")
        print("    to OPEN A ZEPHYR TEST CASE before pressing ENTER, or set")
        print("    zephyr.attachments.app_page_url manually (the URL in your address bar")
        print("    while viewing the Zephyr app).")
    print("  IMPORTANT: the session file is a full Atlassian session credential, keep it")
    print("  secret (it is gitignored). Anyone with it can act as you across Atlassian.")
    print("  Attachment downloads still require: zephyr.attachments.enabled = true")
    print("  Re-run this helper if downloads later fail with auth errors "
          "(the Atlassian session lasts ~2–4 weeks).")


if __name__ == "__main__":
    main()
