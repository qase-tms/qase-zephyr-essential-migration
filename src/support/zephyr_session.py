"""
Capture-once Zephyr Essential browser session → CDN attachment cookies.

Why this exists
---------------
The Zephyr API access token can *list* attachment metadata but is denied the
binary itself (``401 Permission denied``), and the public metadata never
includes a downloadable URL. The bytes are reachable only through Zephyr's own
browser-facing channels, and the single route to them is to replay a real
authenticated browser session:

* **File attachments** (case / execution "attachment" tab): the app's internal
  backend (``atm-rest-base`` = ``.../connect/backend``), authenticated with the
  app JWT harvested from the session (the ``Bearer`` cookie), returns a record
  whose ``url`` is a time-limited **pre-signed S3 URL**, self-authenticating,
  downloadable with no further credentials.
* **Inline rich-text images**: embedded in the HTML as
  ``cloudfront.zs.zephyr4jiracloud.com`` URLs, fetched by replaying the harvested
  cookie jar.

Both the app JWT and the CDN cookies are harvested together by this module; the
download layer (``ZephyrEssentialApiClient``) picks the right mechanism per URL.

Opt-in only
-----------
This path is OFF by default and runs only when ``zephyr.attachments.enabled``
is ``true``. It accesses the CDN outside SmartBear's sanctioned API and
replays a full Atlassian session, so it is a deliberate, disclosed choice -
the migration prints a one-time disclosure of exactly what it does whenever
the mode is enabled. See the README "Attachments" section for the sanctioned
alternatives to consider first.

Two credentials, two lifetimes
------------------------------
* The **Atlassian session** (saved by ``capture_session.py`` to
  ``zephyr.attachments.session_file``) lasts ~2–4 weeks and is the only thing the
  manual browser login creates.
* The **Forge app JWT** (the ``Bearer`` cookie, which the attachment backend
  requires) lasts only ~30–40 minutes and is minted *by loading the Zephyr app
  page* in a browser that already holds a valid Atlassian session.

Capture-once model
------------------
* **Once (~monthly):** ``python capture_session.py``, you log in and open the
  Zephyr app; it saves the durable Atlassian session AND records the exact Zephyr
  app-page URL you landed on (``zephyr.attachments.app_page_url``).
* **Every run:** this module launches headless Chromium with the saved session,
  navigates to that app-page URL so the Forge iframe loads and its backend mints
  a *fresh* ``Bearer`` cookie, waits for that cookie to appear, and harvests the
  jar. The jar is cached until the ``Bearer`` token is near expiry, then
  re-minted automatically, so a migration longer than the token's ~30-min life
  refreshes mid-run instead of silently losing its later attachments. No manual
  step per run; the manual login is only when the ~2–4-week Atlassian session
  lapses.

Graceful degradation
---------------------
Playwright is an **optional** dependency. If Playwright is not installed, no
session file is configured, or the session has expired, this module returns an
empty jar and logs a clear, actionable warning, the migration continues and
falls back to noting attachment filenames in descriptions (the documented
API-limitation behavior). Nothing here can crash a run.
"""

import base64
import json
import os

SESSION_MAX_AGE_DAYS = 21.0
import threading
import time

# Module-level cache: {cache_key: {"jar": {...}, "exp": <bearer token exp epoch>}}.
# The browser is launched only when the cached Bearer token is near expiry, so a
# long migration re-mints mid-run instead of replaying a dead token.
_CACHE: dict = {}
_LOCK = threading.Lock()
_WARNED: set = set()

# Re-mint when the Bearer token has less than this many seconds of life left.
_REFRESH_MARGIN_SEC = 180
# Cooldown before retrying a broken/empty harvest, so a failing session doesn't
# relaunch the browser for every single case.
_BROKEN_COOLDOWN_SEC = 300

# Cookies are only useful for the Zephyr CDN / app hosts.
_ZEPHYR_COOKIE_DOMAINS = ("zephyr4jiracloud.com", "smartbear.com")


def _warn_once(logger, key: str, message: str) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    if logger:
        logger.log(message, "warning")


def _token_exp(jar: dict) -> int:
    """Expiry epoch of the harvested Forge app JWT (the ``Bearer`` cookie), or 0."""
    tok = (jar or {}).get("Bearer")
    if not tok or tok.count(".") < 2:
        return 0
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0


def _try_import_playwright():
    """Return playwright.sync_api.sync_playwright or None if unavailable."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    except Exception:
        return None


def get_cdn_cookies(config, logger=None) -> dict:
    """Return a ``{cookie_name: value}`` jar for Zephyr attachment downloads.

    Safe to call from a worker thread (uses Playwright's *sync* API, which must
    not run inside an asyncio event loop, call via ``pools.source``). Returns
    an empty dict on any missing prerequisite or failure; never raises.

    Cheap to call repeatedly: the harvested jar is cached and only re-minted
    (relaunching the headless browser) when the ``Bearer`` token is within
    ``_REFRESH_MARGIN_SEC`` of expiry. Callers should therefore fetch the jar
    fresh per batch/case rather than hoisting it once, so long runs stay valid.
    """
    # EXPLICIT GATE: the browser-session download path is OFF unless the
    # operator turns it on. Merely having a session file present is not enough
    # — this is a deliberate, disclosed choice (see the disclosure below and
    # the README "Attachments" section), because it accesses the CDN outside
    # SmartBear's sanctioned API and replays a full Atlassian session.
    enabled = bool(config.get("zephyr.attachments.enabled"))
    if not enabled:
        _warn_once(
            logger, "session-disabled",
            "[Attachments] Attachment download is DISABLED (default). Attachment "
            "filenames are noted in descriptions; binaries are not migrated. To "
            "enable it, and understand exactly what it does, see the README "
            "'Attachments' section, then set zephyr.attachments.enabled = true.",
        )
        return {}

    # Enabled → disclose, once per run, precisely what this mode does.
    _warn_once(
        logger, "session-disclosure",
        "[Attachments] Browser-session attachment mode is ENABLED. This launches a "
        "headless browser that replays your saved Atlassian session to reach Zephyr's "
        "own app backend and CDN, the only channels that serve attachment binaries "
        "(the public API denies them). By enabling this you accept that: (1) it uses "
        "Zephyr's internal endpoints outside the sanctioned public API, review your "
        "Atlassian/SmartBear terms of service; (2) the saved session authorizes the "
        "whole Atlassian account, not just Zephyr, keep the session file secret (it "
        "is gitignored); (3) automated login may trip bot detection on locked-down "
        "tenants. Disable any time with zephyr.attachments.enabled = false.",
    )

    session_file = str(
        config.get("zephyr.attachments.session_file") or "zephyr_session.json"
    ).strip()
    # Navigate to the recorded Zephyr app page — loading it mints a fresh Forge
    # token. Fall back to app_url / jira.base_url, though those alone don't load
    # the Forge app and won't mint the token (they only help the CDN cookies).
    nav_url = str(
        config.get("zephyr.attachments.app_page_url")
        or config.get("jira.base_url")
        or ""
    ).strip()

    if not os.path.exists(session_file):
        _warn_once(
            logger, "missing-session-file",
            f"[Attachments] zephyr.attachments.session_file {session_file!r} not "
            f"found, run `python capture_session.py` to create it. Skipping "
            f"attachment downloads (filenames noted in descriptions).",
        )
        return {}

    if not nav_url:
        _warn_once(
            logger, "no-app-url",
            "[Attachments] No zephyr.attachments.app_page_url (or jira.base_url) set, cannot open the Zephyr app to mint a token. "
            "Re-run `python capture_session.py` to record it. Skipping downloads.",
        )
        return {}

    sync_playwright = _try_import_playwright()
    if sync_playwright is None:
        _warn_once(
            logger, "no-playwright",
            "[Attachments] Playwright is not installed, attachment binaries "
            "cannot be downloaded. Install it once with:\n"
            "    pip install playwright && playwright install chromium\n"
            "Falling back to noting attachment filenames in descriptions.",
        )
        return {}

    if not config.get("zephyr.attachments.app_page_url"):
        _warn_once(
            logger, "no-app-page-url",
            "[Attachments] zephyr.attachments.app_page_url is not set, falling "
            "back to a URL that does not load the Zephyr app, so the Forge token "
            "needed for file attachments likely won't mint. Re-run "
            "`python capture_session.py` to record the app page URL.",
        )

    # TTL advisory: the Atlassian session behind the file lasts ~2–4 weeks.
    # A captured session is a credential; refusing a stale one is a safety
    # guard, not a preference, so it is a constant rather than config.
    max_age_days = SESSION_MAX_AGE_DAYS
    age_days = (time.time() - os.path.getmtime(session_file)) / 86400.0
    if age_days > max_age_days:
        _warn_once(
            logger, "stale-session",
            f"[Attachments] Session file is {age_days:.0f} days old (>{max_age_days:.0f}). "
            f"If attachment downloads fail with auth errors, re-capture it with "
            f"`python capture_session.py`.",
        )

    cache_key = (session_file, nav_url)
    now = time.time()
    with _LOCK:
        entry = _CACHE.get(cache_key)
        if entry is not None and entry["exp"] - now > _REFRESH_MARGIN_SEC:
            return entry["jar"]
        # (Re)mint: cache is empty, or the Forge token is at/near expiry.
        jar = _harvest_cookies(sync_playwright, session_file, nav_url, logger)
        exp = _token_exp(jar)
        if exp <= 0:
            # No Bearer token (broken/empty harvest, or CDN-only cookies) — cache
            # briefly so a failing session doesn't relaunch the browser per case.
            exp = now + _BROKEN_COOLDOWN_SEC
        _CACHE[cache_key] = {"jar": jar, "exp": exp}
        return jar


def _harvest_cookies(sync_playwright, session_file: str, nav_url: str, logger) -> dict:
    """Replay the saved session headlessly, load the Zephyr app page to mint a
    fresh Forge token, and harvest the Zephyr cookie jar."""
    if logger:
        logger.log("[Attachments] Minting a fresh Zephyr session (headless browser)…")
    jar: dict = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(storage_state=session_file)
                page = context.new_page()
                # Loading the Zephyr app page loads its Forge iframe, whose backend
                # mints a fresh 'Bearer' cookie. Poll until it appears (the Forge
                # handshake can take several seconds) rather than a fixed sleep.
                page.goto(nav_url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass

                def _harvest_from(ctx) -> dict:
                    out = {}
                    for c in ctx.cookies():
                        domain = (c.get("domain") or "").lstrip(".")
                        if any(domain.endswith(d) for d in _ZEPHYR_COOKIE_DOMAINS):
                            out[c["name"]] = c["value"]
                    return out

                # Poll up to ~30s for the Forge 'Bearer' cookie to be minted.
                for _ in range(15):
                    page.wait_for_timeout(2000)
                    jar = _harvest_from(context)
                    if jar.get("Bearer"):
                        break
            finally:
                browser.close()
    except Exception as e:
        _warn_once(
            logger, "harvest-failed",
            f"[Attachments] Could not mint a Zephyr session ({e}). The saved "
            f"session may have expired, re-capture it with `python capture_session.py`. "
            f"Falling back to noting attachment filenames.",
        )
        return {}

    if not jar.get("Bearer"):
        _warn_once(
            logger, "no-bearer",
            "[Attachments] The Zephyr app did not mint a Forge token, file "
            "attachments cannot be downloaded. Usual cause: the Atlassian session "
            "expired, or app_page_url doesn't load the Zephyr app. Re-capture with "
            "`python capture_session.py` (open a Zephyr test case before pressing ENTER).",
        )
    elif logger:
        exp = _token_exp(jar)
        left = int((exp - time.time()) / 60) if exp else 0
        logger.log(
            f"[Attachments] Zephyr session ready ({len(jar)} cookie(s); "
            f"token valid ~{left} min)."
        )
    return jar
