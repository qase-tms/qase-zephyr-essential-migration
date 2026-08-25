from ..api.jira_lookup import JiraLookupClient
from ..api.zephyr_essential import ZephyrEssentialApiClient
from ..support.config_manager import ConfigManager
from ..support.logger import Logger


class ZephyrEssentialService:
    """Thin service wrapper around ZephyrEssentialApiClient.

    Entity classes call this instead of the API client directly so retry /
    business-logic concerns stay in one place.

    Optional Jira name enrichment: the Essential API returns project name,
    components, and fixVersions as bare Jira links. When ``jira.base_url`` /
    ``jira.email`` / ``jira.api_token`` are configured, display names are
    resolved via Jira REST; otherwise the lookups return empty and callers
    fall back gracefully.
    """

    def __init__(self, config: ConfigManager, logger: Logger):
        self.config = config
        self.logger = logger

        token = config.get("zephyr.access_token") or ""
        host = config.get("zephyr.base_url") or "https://prod-api.zephyr4jiracloud.com/v2"

        self.client = ZephyrEssentialApiClient(
            token=token,
            logger=logger,
            base_url=host,
            page_size=page_size,
        )

        jira_base = str(config.get("jira.base_url") or "").strip()
        jira_email = str(config.get("jira.email") or "").strip()
        jira_token = str(config.get("jira.api_token") or "").strip()
        self.jira = None
        if jira_base and jira_email and jira_token:
            self.jira = JiraLookupClient(jira_base, jira_email, jira_token, logger)
            self.logger.log("[ZephyrEssential] Jira name enrichment ENABLED")
        else:
            self.logger.log(
                "[ZephyrEssential] Jira name enrichment disabled (no jira.* config), "
                "project titles fall back to keys, component tags and run version "
                "context are omitted"
            )
        # Per-project caches for enrichment lookups
        self._component_names: dict = {}
        self._version_names: dict = {}
        # Jira issue id → key cache (issue links carry only numeric ids)
        self._issue_keys: dict = {}

    # ------------------------------------------------------------------
    # Optional Jira name enrichment
    # ------------------------------------------------------------------

    def get_jira_project_name(self, project_key: str) -> str:
        """Real Jira project name, or '' when enrichment is off/unavailable."""
        if not self.jira:
            return ""
        try:
            return (self.jira.get_project(project_key).get("name") or "").strip()
        except Exception as e:
            self.logger.log(
                f"[ZephyrEssential] Jira project name lookup failed for {project_key}: {e}",
                "warning",
            )
            return ""

    def get_component_names(self, project_key: str) -> dict:
        """{component_id → name} for the project; empty when enrichment is off."""
        if not self.jira:
            return {}
        if project_key not in self._component_names:
            try:
                self._component_names[project_key] = self.jira.get_component_names(project_key)
            except Exception as e:
                self.logger.log(
                    f"[ZephyrEssential] Jira component lookup failed for {project_key}: {e}",
                    "warning",
                )
                self._component_names[project_key] = {}
        return self._component_names[project_key]

    def get_version_names(self, project_key: str) -> dict:
        """{version_id → name} for the project; empty when enrichment is off."""
        if not self.jira:
            return {}
        if project_key not in self._version_names:
            try:
                self._version_names[project_key] = self.jira.get_version_names(project_key)
            except Exception as e:
                self.logger.log(
                    f"[ZephyrEssential] Jira version lookup failed for {project_key}: {e}",
                    "warning",
                )
                self._version_names[project_key] = {}
        return self._version_names[project_key]

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def get_projects(self) -> list:
        self.logger.log("[ZephyrEssential] Fetching projects")
        return self.client.get_projects()

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------

    def get_folders(self, project_key: str, folder_type: str = "TEST_CASE") -> list:
        self.logger.log(f"[ZephyrEssential] Fetching {folder_type} folders for {project_key}")
        return self.client.get_folders(project_key, folder_type)

    # ------------------------------------------------------------------
    # Test cases
    # ------------------------------------------------------------------

    def get_test_cases(self, project_key: str, folder_id: int = None):
        """Yield pages of test cases."""
        yield from self.client.get_test_cases(project_key, folder_id)

    def get_test_case(self, test_case_key: str) -> dict:
        return self.client.get_test_case(test_case_key)

    def get_test_steps(self, test_case_key: str) -> list:
        return self.client.get_test_steps(test_case_key)

    def get_test_script(self, test_case_key: str) -> dict:
        return self.client.get_test_script(test_case_key)

    # ------------------------------------------------------------------
    # Links (Jira issue links + web links)
    # ------------------------------------------------------------------

    def get_test_case_links(self, tc_key: str) -> dict:
        return self.client.get_test_case_links(tc_key)

    def get_test_cycle_links(self, cycle_key) -> dict:
        return self.client.get_test_cycle_links(cycle_key)

    def get_test_execution_links(self, ex_id) -> dict:
        return self.client.get_test_execution_links(ex_id)

    # ------------------------------------------------------------------
    # Internal-backend attachment records (pre-signed S3 URLs)
    # ------------------------------------------------------------------

    def get_case_attachment_records(self, tc_key: str, rest_base: str, app_jwt: str) -> list:
        return self.client.get_case_attachment_records(tc_key, rest_base, app_jwt)

    def get_result_attachment_records(self, result_key, rest_base: str, app_jwt: str) -> list:
        return self.client.get_result_attachment_records(result_key, rest_base, app_jwt)

    def get_cycle_attachment_records(self, cycle_key, jira_project_id, rest_base: str, app_jwt: str) -> list:
        return self.client.get_cycle_attachment_records(cycle_key, jira_project_id, rest_base, app_jwt)

    def get_project_id(self, entity: str, key, rest_base: str, app_jwt: str):
        return self.client.get_project_id(entity, key, rest_base, app_jwt)

    def download_presigned(self, url: str) -> bytes:
        return self.client.download_presigned(url)

    def resolve_issue_keys(self, issue_ids: list) -> dict:
        """Numeric Jira issue ids → keys (``{"11228": "ZEM-1"}``), cached.

        Returns only what Jira enrichment can resolve, empty dict when
        enrichment is off. Ids that stay unresolved are absent from the map.
        """
        if not self.jira:
            return {}
        pending = [str(i) for i in issue_ids if str(i) not in self._issue_keys]
        if pending:
            try:
                self._issue_keys.update(self.jira.get_issue_keys(pending))
            except Exception as e:
                self.logger.log(f"[ZephyrEssential] Issue key resolution failed: {e}", "warning")
        return {str(i): self._issue_keys[str(i)]
                for i in issue_ids if str(i) in self._issue_keys}

    # ------------------------------------------------------------------
    # Test cycles
    # ------------------------------------------------------------------

    def get_test_cycles(self, project_key: str, created_after: int = 0):
        """Yield pages of test cycles."""
        yield from self.client.get_test_cycles(project_key, created_after)

    def get_test_cycle(self, cycle_key: str) -> dict:
        return self.client.get_test_cycle(cycle_key)

    def get_test_plans(self, project_key: str):
        """Yield pages of test plans."""
        yield from self.client.get_test_plans(project_key)

    # ------------------------------------------------------------------
    # Test executions
    # ------------------------------------------------------------------

    def get_test_executions(self, project_key: str, test_cycle_key: str = None):
        """Yield pages of test executions."""
        yield from self.client.get_test_executions(project_key, test_cycle_key)

    def get_test_execution_steps(self, execution_key) -> list:
        return self.client.get_test_execution_steps(execution_key)

    # ------------------------------------------------------------------
    # Environments
    # ------------------------------------------------------------------

    def get_environments(self, project_key: str) -> list:
        return self.client.get_environments(project_key)

    # ------------------------------------------------------------------
    # Statuses and priorities
    # ------------------------------------------------------------------

    def get_priorities(self, project_key: str) -> list:
        return self.client.get_priorities(project_key)

    def get_statuses(self, project_key: str, status_type: str = "TEST_CASE") -> list:
        return self.client.get_statuses(project_key, status_type)

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def get_test_case_attachments(self, tc_key: str) -> list:
        return self.client.get_test_case_attachments(tc_key)

    def get_test_cycle_attachments(self, cycle_key: str) -> list:
        return self.client.get_test_cycle_attachments(cycle_key)

    def get_test_execution_attachments(self, ex_id) -> list:
        return self.client.get_test_execution_attachments(ex_id)

    def download_attachment_bytes(
        self,
        url: str,
        jira_email: str = None,
        jira_api_token: str = None,
        cdn_cookies: dict = None,
    ) -> bytes:
        return self.client.download_attachment_bytes(url, jira_email, jira_api_token, cdn_cookies)

    # ------------------------------------------------------------------
    # Custom fields
    # ------------------------------------------------------------------

    def get_custom_fields(self, project_key: str) -> list:
        return self.client.get_custom_fields(project_key)
