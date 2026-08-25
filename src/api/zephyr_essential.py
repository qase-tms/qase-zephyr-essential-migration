import http.client
import time

import requests

from ..exceptions.api import APIError

_DEFAULT_BASE_URL = "https://prod-api.zephyr4jiracloud.com/v2"


class ZephyrEssentialApiClient:
    """HTTP client for the Zephyr Essential Cloud REST API (v2.9).

    Base URL: ``https://prod-api.zephyr4jiracloud.com/v2``
    Auth: ``Authorization: Bearer <access-token>``, token generated in
    Jira → Settings → Apps → Zephyr API Access Tokens.
    Pagination: ``startAt`` + ``maxResults`` + ``isLast``.

    The post-2025 Zephyr Essential API is schema-compatible with Zephyr Scale
    Cloud v2 (verified against the official OpenAPI spec, SwaggerHub
    ``smartbear-public/zephyr-squad-cloud-api/2.9``, and a live tenant):
    same endpoints (/testcases, /testcycles, /testexecutions, /folders,
    /statuses, /priorities, /testplans), same field names, same pagination.
    Known deltas: no /customfields endpoint (callers fall back to test-case
    payload scanning) and no attachment endpoints (attachments are not
    extractable, out of scope per the addendum).
    """

    def __init__(
        self,
        token: str,
        logger,
        base_url: str = _DEFAULT_BASE_URL,
        max_retries: int = 7,
        backoff_factor: float = 2.0,
        connect_timeout: float = 30.0,
        read_timeout: float = 60.0,
        page_size: int = 100,
    ):
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self.logger = logger
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.request_timeout = (float(connect_timeout), float(read_timeout))
        # Pagination size is not configurable: it is an API detail, not a
        # customer decision. See section 4.2 of the migration script standard.
        self.page_size = int(page_size)

    # ------------------------------------------------------------------
    # Low-level request helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict = None) -> dict:
        """GET ``{base_url}/{path}`` and return the parsed JSON body."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.get(
                    url,
                    headers=self.headers,
                    params=params or {},
                    timeout=self.request_timeout,
                )
                if response.status_code == 200:
                    try:
                        return response.json()
                    except (ValueError, requests.exceptions.JSONDecodeError) as e:
                        raise APIError(f"Non-JSON response from {path!r}") from e
                # Fast-fail on deterministic client errors (any 4xx except 408/429)
                if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                    raise APIError(
                        f"HTTP {response.status_code} (non-retryable) for {path!r}"
                    )
                time.sleep(self.backoff_factor * (2 ** attempt))
            except APIError:
                raise
            except (
                requests.exceptions.Timeout,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                requests.exceptions.ConnectionError,
            ):
                time.sleep(self.backoff_factor * (2 ** attempt))

            if attempt == self.max_retries:
                raise APIError(f"Max retries reached for {path!r}")

    def _get_paginated(self, path: str, params: dict = None):
        """Yield all pages from a Zephyr Essential paginated endpoint.

        Zephyr Essential uses ``startAt`` / ``maxResults`` / ``isLast`` for pagination.
        Returns each page's ``values`` list as one batch.
        """
        start_at = 0
        base_params = dict(params or {})
        base_params["maxResults"] = self.page_size
        while True:
            base_params["startAt"] = start_at
            data = self._get(path, base_params)
            values = data.get("values") or []
            if values:
                yield values
            if data.get("isLast", True) or len(values) < self.page_size:
                break
            start_at += len(values)

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def get_projects(self):
        """Return all Zephyr Essential–enabled projects."""
        all_projects = []
        for page in self._get_paginated("projects", {"maxResults": 50}):
            all_projects.extend(page)
        return all_projects

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------

    def get_folders(self, project_key: str, folder_type: str = "TEST_CASE"):
        """Return all folders of *folder_type* for *project_key*.

        folder_type: ``TEST_CASE``, ``TEST_CYCLE``, or ``TEST_PLAN``
        """
        all_folders = []
        for page in self._get_paginated(
            "folders", {"projectKey": project_key, "folderType": folder_type}
        ):
            all_folders.extend(page)
        return all_folders

    # ------------------------------------------------------------------
    # Test cases
    # ------------------------------------------------------------------

    def get_test_cases(self, project_key: str, folder_id: int = None):
        """Yield pages of test cases for *project_key*, optionally filtered by *folder_id*."""
        params = {"projectKey": project_key}
        if folder_id is not None:
            params["folderId"] = folder_id
        yield from self._get_paginated("testcases", params)

    def get_test_case(self, test_case_key: str) -> dict:
        """Fetch a single test case with its full detail."""
        return self._get(f"testcases/{test_case_key}")

    def get_test_steps(self, test_case_key: str) -> list:
        """Return ALL test steps for a test case (step-by-step type).

        Must paginate: the endpoint's default maxResults is 10 (verified live)
       , a single GET silently drops steps beyond the first page.
        """
        steps = []
        for page in self._get_paginated(f"testcases/{test_case_key}/teststeps"):
            steps.extend(page)
        return steps

    def get_test_script(self, test_case_key: str) -> dict:
        """Return the test script for a BDD / plain-script case.

        Shape: ``{"type": "bdd"|"plain", "text": "...", "id": ...}``.
        Step-by-step cases have no script, the endpoint 4xxs; callers
        should treat any error as "no script".
        """
        return self._get(f"testcases/{test_case_key}/testscript")

    # ------------------------------------------------------------------
    # Test cycles
    # ------------------------------------------------------------------

    @staticmethod
    def _cycle_created_ts(cycle: dict) -> int:
        """Best-effort cycle timestamp: createdOn (epoch) or plannedStartDate (ISO).

        Zephyr Essential cycles carry no createdOn, plannedStartDate is the
        only date signal. Returns 0 when neither is parseable (cycle is kept).
        """
        value = cycle.get("createdOn") or cycle.get("plannedStartDate")
        if value is None:
            return 0
        if isinstance(value, (int, float)) and value > 0:
            return int(value / 1000) if value > 1e10 else int(value)
        if isinstance(value, str):
            from datetime import datetime, timezone
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(value[:26], fmt[: len(fmt)])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return int(dt.timestamp())
                except ValueError:
                    continue
        return 0

    def get_test_cycles(self, project_key: str, created_after: int = 0):
        """Yield pages of test cycles for *project_key*."""
        params = {"projectKey": project_key}
        for page in self._get_paginated("testcycles", params):
            if created_after:
                # Keep cycles with no parseable date (ts == 0) rather than
                # silently dropping everything on Essential tenants.
                page = [
                    c for c in page
                    if (ts := self._cycle_created_ts(c)) == 0 or ts >= created_after
                ]
            if page:
                yield page

    def get_test_cycle(self, cycle_key: str) -> dict:
        """Fetch a single test cycle."""
        return self._get(f"testcycles/{cycle_key}")

    def get_test_plans(self, project_key: str):
        """Yield pages of test plans for *project_key*."""
        yield from self._get_paginated("testplans", {"projectKey": project_key})

    # ------------------------------------------------------------------
    # Test executions
    # ------------------------------------------------------------------

    def get_test_executions(self, project_key: str, test_cycle_key: str = None):
        """Yield pages of test executions, optionally filtered by cycle key."""
        params = {"projectKey": project_key}
        if test_cycle_key:
            params["testCycle"] = test_cycle_key
        yield from self._get_paginated("testexecutions", params)

    def get_test_execution_steps(self, execution_key) -> list:
        """Return step-level results for an execution (paginated).

        Each item: ``{"id": ..., "inline": {"description", "expectedResult",
        "actualResult", "status": {id, self}, ...}}``, the per-step status id
        resolves via the TEST_EXECUTION statuses map. Executions of script-type
        (BDD/plain) cases have no steps, treat any error as "no steps".
        """
        steps = []
        for page in self._get_paginated(f"testexecutions/{execution_key}/teststeps"):
            steps.extend(page)
        return steps

    # ------------------------------------------------------------------
    # Environments
    # ------------------------------------------------------------------

    def get_environments(self, project_key: str) -> list:
        """Return all environments for *project_key* (``{id, name, description,
        archived}``). Executions reference them as ``environment: {id, self}``."""
        envs = []
        for page in self._get_paginated("environments", {"projectKey": project_key}):
            envs.extend(page)
        return envs

    # ------------------------------------------------------------------
    # Statuses and priorities
    # ------------------------------------------------------------------

    def get_priorities(self, project_key: str) -> list:
        data = self._get("priorities", {"projectKey": project_key, "maxResults": 100})
        return data.get("values") or []

    def get_statuses(self, project_key: str, status_type: str = "TEST_CASE") -> list:
        data = self._get(
            "statuses",
            {"projectKey": project_key, "statusType": status_type, "maxResults": 100},
        )
        return data.get("values") or []

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def _get_optional(self, path: str, params: dict = None):
        """Like _get but returns None on 404 instead of raising."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = requests.get(
                url,
                headers=self.headers,
                params=params or {},
                timeout=self.request_timeout,
            )
            if response.status_code == 200:
                return response.json()
            if response.status_code == 404:
                return None
            if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                return None
            return None
        except Exception:
            return None

    def get_test_case_attachments(self, tc_key: str) -> list:
        """Return normalised attachment list for a test case.

        The ZS API returns ``{"attachments": [{"id": 123, "name": "file.png"}]}``.
        We normalise each item to ``{"filename": name, "url": <download_url>}``
        so callers can treat file and execution attachments uniformly.
        Download URL: GET /testcases/{key}/attachments/{id} (Bearer token, no CDN).
        """
        data = self._get_optional(f"testcases/{tc_key}/attachments")
        if not data:
            return []
        raw = data.get("attachments") or data.get("values") or []
        result = []
        for a in raw:
            att_id = a.get("id")
            name = a.get("name") or a.get("filename") or "attachment"
            url = a.get("url") or (
                f"{self.base_url}/testcases/{tc_key}/attachments/{att_id}" if att_id else None
            )
            if url:
                result.append({"filename": name, "url": url})
        return result

    def get_test_cycle_attachments(self, cycle_key: str) -> list:
        """Normalised attachment list for a test cycle (undocumented endpoint;
        metadata only, binary download is denied to the API token)."""
        data = self._get_optional(f"testcycles/{cycle_key}/attachments")
        if not data:
            return []
        raw = data.get("attachments") or data.get("values") or []
        result = []
        for a in raw:
            att_id = a.get("id")
            name = a.get("name") or a.get("filename") or "attachment"
            url = a.get("url") or (
                f"{self.base_url}/testcycles/{cycle_key}/attachments/{att_id}" if att_id else None
            )
            if url:
                result.append({"filename": name, "url": url})
        return result

    def get_test_execution_attachments(self, ex_id) -> list:
        """Return normalised attachment list for a test execution.

        Same normalisation as get_test_case_attachments.
        Download URL: GET /testexecutions/{id}/attachments/{att_id} (Bearer token).
        """
        data = self._get_optional(f"testexecutions/{ex_id}/attachments")
        if not data:
            return []
        raw = data.get("attachments") or data.get("values") or []
        result = []
        for a in raw:
            att_id = a.get("id")
            name = a.get("name") or a.get("filename") or "attachment"
            url = a.get("url") or (
                f"{self.base_url}/testexecutions/{ex_id}/attachments/{att_id}" if att_id else None
            )
            if url:
                result.append({"filename": name, "url": url})
        return result

    # ------------------------------------------------------------------
    # Links (Jira issue links + web links)
    # ------------------------------------------------------------------

    def get_test_case_links(self, tc_key: str) -> dict:
        """Return ``{"issues": [...], "webLinks": [...]}`` for a test case.

        Issue links carry only the numeric Jira ``issueId`` (plus a REST
        ``target`` URL), resolving to a human-readable key needs Jira access.
        """
        return self._get_optional(f"testcases/{tc_key}/links") or {}

    def get_test_cycle_links(self, cycle_key) -> dict:
        return self._get_optional(f"testcycles/{cycle_key}/links") or {}

    def get_test_execution_links(self, ex_id) -> dict:
        return self._get_optional(f"testexecutions/{ex_id}/links") or {}

    def download_attachment_bytes(
        self,
        url: str,
        jira_email: str = None,
        jira_api_token: str = None,
        cdn_cookies: dict = None,
    ) -> bytes:
        """Download an attachment binary, returning ``bytes`` or ``None``.

        The Zephyr API access token is denied attachment binaries
        (``401 Permission denied``); the working route is to replay a real
        browser session's CloudFront cookies (see ``support.zephyr_session``).
        Genuine Jira-issue attachments (not Zephyr-internal) are reachable with
        the Jira token.

        Tries, in order, first ``200`` with a body wins:
        1. Browser CDN cookie jar alone, CloudFront-hosted files & inline images
        2. Browser CDN cookie jar + Bearer header, session-gated API endpoints
        3. Bearer header alone, anything the token is actually allowed to serve
        4. Jira Basic auth, genuine Jira-issue attachments

        ``cdn_cookies`` is a ``{name: value}`` jar; empty/None simply skips the
        cookie-based attempts (the common no-session path).
        """
        jar = cdn_cookies or None

        attempts: list = []  # (headers, cookies, auth)
        if jar:
            attempts.append(({}, jar, None))
            attempts.append((self.headers, jar, None))
        attempts.append((self.headers, None, None))
        if jira_email and jira_api_token:
            attempts.append(({}, None, (jira_email, jira_api_token)))

        for hdrs, cookies, auth in attempts:
            try:
                resp = requests.get(
                    url,
                    headers=hdrs or {},
                    cookies=cookies,
                    auth=auth,
                    timeout=(30.0, 120.0),
                    allow_redirects=True,
                )
                if resp.status_code == 200 and resp.content:
                    return resp.content
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------
    # Internal backend attachment records (pre-signed S3 URLs)
    # ------------------------------------------------------------------
    #
    # The public v2 attachment metadata endpoint returns only {id, name} and
    # never a downloadable URL. The Zephyr app's OWN backend
    # (``atm-rest-base`` = .../connect/backend), authenticated with the app
    # JWT harvested from a browser session (the ``Bearer`` cookie), returns a
    # full attachment record whose ``url`` is a time-limited **pre-signed S3
    # URL** — self-authenticating, downloadable with no further credentials.
    # This is the only working route to Zephyr-internal attachment binaries.

    def _backend_attachment_records(
        self, rest_base: str, app_jwt: str, entity: str, key: str, extra_headers: dict = None
    ) -> list:
        """Return ``[{"dbAttachmentId", "name", "url"(presigned)}, ...]`` for
        a ``testcase`` / ``testresult`` / ``testrun``. Empty list on any failure."""
        if not (rest_base and app_jwt and key):
            return []
        url = f"{rest_base.rstrip('/')}/rest/tests/1.0/{entity}/{key}"
        headers = {"Authorization": f"Bearer {app_jwt}", "Accept": "application/json"}
        headers.update(extra_headers or {})
        try:
            resp = requests.get(
                url, headers=headers,
                params={"fields": "attachments(dbAttachmentId,name,filename,url)"},
                timeout=(30.0, 60.0),
            )
            if resp.status_code != 200:
                return []
            records = (resp.json() or {}).get("attachments") or []
        except Exception:
            return []
        out = []
        for r in records:
            u = r.get("url")
            if not u:
                continue
            out.append({
                "dbAttachmentId": r.get("dbAttachmentId"),
                "name": r.get("name") or r.get("filename") or "attachment",
                "url": u,
            })
        return out

    def get_case_attachment_records(self, tc_key: str, rest_base: str, app_jwt: str) -> list:
        return self._backend_attachment_records(rest_base, app_jwt, "testcase", tc_key)

    def get_result_attachment_records(self, result_key, rest_base: str, app_jwt: str) -> list:
        return self._backend_attachment_records(rest_base, app_jwt, "testresult", str(result_key))

    def get_cycle_attachment_records(
        self, cycle_key, jira_project_id, rest_base: str, app_jwt: str
    ) -> list:
        # The testrun endpoint requires a jira-project-id header (unlike
        # testcase/testresult, which resolve the project from the key).
        extra = {"jira-project-id": str(jira_project_id)} if jira_project_id else None
        return self._backend_attachment_records(
            rest_base, app_jwt, "testrun", str(cycle_key), extra_headers=extra
        )

    def get_project_id(self, entity: str, key, rest_base: str, app_jwt: str):
        """Jira numeric project id via the internal backend (needed for the
        testrun/cycle attachment header). ``entity`` = ``testcase``/``testresult``,
        ``key`` a corresponding key. Returns None on failure."""
        if not (rest_base and app_jwt and key):
            return None
        try:
            resp = requests.get(
                f"{rest_base.rstrip('/')}/rest/tests/1.0/{entity}/{key}",
                headers={"Authorization": f"Bearer {app_jwt}", "Accept": "application/json"},
                params={"fields": "projectId"}, timeout=(30.0, 60.0),
            )
            if resp.status_code == 200:
                return (resp.json() or {}).get("projectId")
        except Exception:
            pass
        return None

    @staticmethod
    def download_presigned(url: str) -> bytes:
        """Download a self-authenticating (pre-signed S3) URL with no auth.
        Returns ``bytes`` or ``None``."""
        try:
            resp = requests.get(url, timeout=(30.0, 120.0))
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Custom fields
    # ------------------------------------------------------------------

    def get_custom_fields(self, project_key: str) -> list:
        try:
            all_fields = []
            for page in self._get_paginated(
                "customfields", {"projectKey": project_key}
            ):
                all_fields.extend(page)
            return all_fields
        except APIError:
            return []
