import http.client
import time

import requests

from ..exceptions.api import APIError


class JiraLookupClient:
    """Minimal, OPTIONAL Jira REST client used only to resolve display names.

    The Zephyr Essential API returns some fields as bare Jira resource links
    ({id, self}) with no name inline: project name, components, fixVersions.
    When Jira credentials are configured this client resolves them; when not,
    the migration still runs, those names just fall back (project title = key,
    component tag dropped, run version context omitted).

    Scope guard: this client only ever reads /project metadata (name,
    components, versions), /myself, and issue id → key resolution via a
    read-only JQL search (``get_issue_keys``). Zephyr issue links expose only
    the numeric Jira issueId, so key resolution is required to migrate links
    as readable references. Do NOT extend this client beyond these lookups -
    see the "no Jira API expansion" constraint in the repo conventions.
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        logger,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.auth = (email, api_token)
        self.logger = logger
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    def _get(self, path: str, params: dict = None):
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.get(
                    url,
                    auth=self.auth,
                    params=params or {},
                    headers={"Accept": "application/json"},
                    timeout=(30.0, 60.0),
                )
                if 200 <= response.status_code < 300:
                    try:
                        return response.json()
                    except (ValueError, requests.exceptions.JSONDecodeError) as e:
                        raise APIError(f"Non-JSON response from Jira {path!r}") from e
                # Fast-fail on deterministic client errors (any 4xx except 408/429)
                if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                    raise APIError(
                        f"HTTP {response.status_code} (non-retryable) for Jira {path!r}"
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
                raise APIError(f"Max retries reached for Jira {path!r}")

    def get_myself(self) -> dict:
        return self._get("rest/api/2/myself")

    def get_project(self, project_key: str) -> dict:
        return self._get(f"rest/api/2/project/{project_key}")

    def get_component_names(self, project_key: str) -> dict:
        """{component_id (str) → name}"""
        components = self._get(f"rest/api/2/project/{project_key}/components") or []
        return {
            str(c["id"]): (c.get("name") or "").strip()
            for c in components
            if c.get("id") is not None
        }

    def get_version_names(self, project_key: str) -> dict:
        """{version_id (str) → name}"""
        versions = self._get(f"rest/api/2/project/{project_key}/versions") or []
        return {
            str(v["id"]): (v.get("name") or "").strip()
            for v in versions
            if v.get("id") is not None
        }

    def get_issue_keys(self, issue_ids: list) -> dict:
        """Resolve numeric Jira issue ids → issue keys: ``{"11228": "ZEM-1"}``.

        Read-only, batched JQL search (``id IN (...)``, 100 ids per call,
        key field only). Ids the token cannot see are simply absent from the
        result, callers must handle missing entries.
        """
        ids = [str(i) for i in issue_ids if str(i).strip().isdigit()]
        resolved: dict = {}
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            try:
                data = self._get(
                    "rest/api/3/search/jql",
                    # fields=key is required: without it /search/jql returns
                    # bare {id} objects with no key (verified live)
                    {"jql": f"id IN ({','.join(chunk)})", "fields": "key", "maxResults": 100},
                ) or {}
                for issue in data.get("issues", []):
                    iid, key = issue.get("id"), issue.get("key")
                    if iid and key:
                        resolved[str(iid)] = key
            except APIError:
                # `id IN (...)` 400s if ANY id is deleted or not visible —
                # fall back to per-issue lookups so one dead link doesn't
                # sink the whole chunk.
                for iid in chunk:
                    try:
                        issue = self._get(f"rest/api/3/issue/{iid}", {"fields": "key"}) or {}
                        if issue.get("key"):
                            resolved[iid] = issue["key"]
                    except APIError:
                        continue
        return resolved
