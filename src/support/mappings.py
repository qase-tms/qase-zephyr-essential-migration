from .stats import Stats


class Mappings:
    def __init__(self, source: str, default_user: int = 1, user_map: dict = None):
        self.suites = {}
        self.types = {}
        self.priorities = {}
        self.result_statuses = {}
        self.case_statuses = {}
        self.custom_fields = {}
        self.milestones = {}
        self.projects = []
        self.attachments_map = {}

        # Zephyr Scale project key -> Qase project code
        self.project_map = {}

        # Zephyr Scale testcase key -> Qase case id (per project code)
        self.zephyr_tc_id_to_qase_case_id: dict = {}

        self.refs_id = None
        self.group_id = None
        # "Jira Links" CF id — fallback home for Zephyr issue links when
        # native external-issue attach is unavailable (no Jira integration)
        self.jira_links_field_id = None

        self.qase_fields_type = {
            "number": 0,
            "string": 1,
            "text": 2,
            "selectbox": 3,
            "checkbox": 4,
            "radio": 5,
            "multiselect": 6,
            "url": 7,
            "user": 8,
            "datetime": 9,
        }

        self.default_user = default_user
        # Optional config-provided user mapping: Atlassian account id (string)
        # → Qase user id. Populated from ``users.map``; anything unmapped
        # falls back to ``users.default``. No Jira API involved.
        self.user_map = {str(k): int(v) for k, v in (user_map or {}).items()}

        # Qase environment registry: {project_code: {env_title_lower: qase_env_id}}
        self.environments = {}

        # Delta mode: {project_code: state dict from get_existing_project_state}
        # Present only for projects that pre-existed this run.
        self.existing_state = {}

        self.stats = Stats(source=source)

        self.qase_priority_keys_to_id: dict = {}
        self.qase_case_status_keys_to_id: dict = {}

    def register_qase_system_fields(self, fields: list) -> None:
        """Map Qase priority / case-status option titles and slugs → numeric ids for bulk import."""
        self.qase_priority_keys_to_id.clear()
        self.qase_case_status_keys_to_id.clear()
        for f in fields or []:
            if not isinstance(f, dict):
                continue
            slug = (f.get("slug") or "").strip().lower()
            title = (f.get("title") or "").strip().lower()
            options = f.get("options")
            if not isinstance(options, list) or not options:
                continue
            target = None
            if (
                slug == "priority"
                or title == "priority"
                or ("priority" in slug and "severity" not in slug)
            ):
                target = self.qase_priority_keys_to_id
            elif slug in ("status", "case-status", "state") or (
                "status" in slug and ("case" in slug or slug.endswith("status"))
            ):
                target = self.qase_case_status_keys_to_id
            elif title in ("status", "state") or (
                "status" in title and "run" not in title and "defect" not in title
            ):
                target = self.qase_case_status_keys_to_id
            if target is None:
                continue
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                oid = opt.get("id")
                if oid is None:
                    continue
                oid = int(oid)
                for key in (opt.get("slug"), opt.get("title")):
                    if isinstance(key, str) and key.strip():
                        target[key.strip().lower()] = oid
                target[str(oid)] = oid

    def get_user_id(self, id) -> int:
        """Resolve a source user (Atlassian account id) to a Qase user id.

        Uses the ``users.map`` config when provided; otherwise ``users.default``.
        """
        if id is not None:
            mapped = self.user_map.get(str(id).strip())
            if mapped is not None:
                return mapped
        return self.default_user

    def register_zephyr_testcase_qase_case_id(
        self, project_code: str, zephyr_tc_key: str, qase_case_id: int
    ) -> None:
        """Record mapping from Zephyr Scale test case key to Qase case id."""
        if not project_code or not zephyr_tc_key or qase_case_id is None:
            return
        code = str(project_code).strip()
        self.zephyr_tc_id_to_qase_case_id.setdefault(code, {})[
            str(zephyr_tc_key).strip()
        ] = int(qase_case_id)
