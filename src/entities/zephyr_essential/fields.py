import asyncio

from ...service.qase import QaseService
from ...service.zephyr_essential import ZephyrEssentialService
from ...support.config_manager import ConfigManager
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools

# Zephyr Essential custom field type name → Qase custom field type id
_CF_TYPE_MAP = {
    "plain_text": 2,
    "single_line_text": 1,
    "paragraph": 2,
    "single_select": 3,
    "list": 3,
    "multi_select": 6,
    "multilist": 6,
    "checkbox": 4,
    "integer": 0,
    "float": 0,
    "decimal": 0,
    "number": 0,
    "date": 9,
    "datetime": 9,
    "url": 7,
    "user_list": 8,
}


def _infer_cf_type(name: str, sample_value) -> int:
    """Infer Qase custom field type from a field name and a sample value."""
    if isinstance(sample_value, bool):
        return 4  # checkbox
    if isinstance(sample_value, list):
        return 6  # multi-select
    if isinstance(sample_value, (int, float)):
        return 0  # number
    name_lower = (name or "").lower()
    if isinstance(sample_value, str):
        if "user" in name_lower:
            return 8
        if "date" in name_lower:
            return 9
        if "url" in name_lower:
            return 7
        return 1  # single-line text
    # null — use name hints only (user before date to avoid "user picker" matching "picker")
    if "user" in name_lower:
        return 8
    if "date" in name_lower:
        return 9
    if "url" in name_lower:
        return 7
    if "checkbox" in name_lower:
        return 4
    if "number" in name_lower or "integer" in name_lower or "float" in name_lower:
        return 0
    if "multi" in name_lower or "list" in name_lower:
        return 6
    if "paragraph" in name_lower or "multi line" in name_lower or "multiline" in name_lower:
        return 2
    return 1  # default: string


class Fields:
    """Register Qase system field option ids and mirror Zephyr Essential custom fields to Qase."""

    def __init__(
        self,
        qase_service: QaseService,
        source_service: ZephyrEssentialService,
        logger: Logger,
        mappings: Mappings,
        config: ConfigManager,
        pools: Pools,
    ):
        self.qase = qase_service
        self.zephyr = source_service
        self.config = config
        self.logger = logger
        self.mappings = mappings
        self.pools = pools

    def import_fields(self) -> Mappings:
        return asyncio.run(self._import_fields_async())

    async def _import_fields_async(self) -> Mappings:
        # 1 — Register Qase system fields (priority/status option ids)
        self.logger.log("[Fields] Registering Qase system fields (priority/status options)")
        system_fields = await self.pools.qs(self.qase.get_system_fields)
        if system_fields:
            fields_as_dicts = []
            for f in system_fields:
                if hasattr(f, "to_dict"):
                    fields_as_dicts.append(f.to_dict())
                elif isinstance(f, dict):
                    fields_as_dicts.append(f)
            self.mappings.register_qase_system_fields(fields_as_dicts)
            self.logger.log(
                f"[Fields] Registered {len(fields_as_dicts)} system field(s); "
                f"priority options: {list(self.mappings.qase_priority_keys_to_id.keys())[:8]}; "
                f"status options: {list(self.mappings.qase_case_status_keys_to_id.keys())[:8]}"
            )
        else:
            self.logger.log("[Fields] No system fields returned from Qase", "warning")

        # 2 — Mirror Zephyr Essential custom fields → Qase custom fields
        await self._sync_custom_fields()

        # 3 — Ensure the "Jira Links" fallback field exists up front
        await self._ensure_jira_links_field()

        return self.mappings

    def _target_codes(self) -> list:
        """Qase project codes this run imports into, the ONLY projects any
        custom field is scoped to. Fields are workspace-global in Qase, so
        broader scoping (is_enabled_for_all_projects) would pollute every
        pre-existing project in the customer's workspace."""
        return sorted({p.get("code") for p in self.mappings.projects if p.get("code")})

    async def _scope_cf_to_targets(self, cf, cf_id: int, name: str) -> None:
        """Make an EXISTING field usable in the target projects without touching
        its scope anywhere else: globally-enabled fields are left untouched;
        project-scoped fields get the target codes APPENDED (merge, never shrink)."""
        if bool(getattr(cf, "is_enabled_for_all_projects", False)):
            return
        existing = [str(c) for c in (getattr(cf, "projects_codes", None) or [])]
        missing = [c for c in self._target_codes() if c not in existing]
        if not missing:
            return
        ok = await self.pools.qs(
            self.qase.update_custom_field, cf_id,
            {
                # title is required by the update model — sent unchanged
                "title": (getattr(cf, "title", None) or name),
                "projects_codes": existing + missing,
            },
        )
        self.logger.log(
            f"[Fields] {'Added' if ok else 'FAILED to add'} target project(s) {missing} "
            f"to existing CF {name!r} (kept its other {len(existing)} project(s))",
            "info" if ok else "warning",
        )

    async def _ensure_jira_links_field(self):
        """Ensure a text CF titled "Jira Links" exists and covers the target projects.

        It is the fallback home for Zephyr issue links whenever native
        external-issue attach is unavailable (workspace has no Jira Cloud
        integration, or Qase can't resolve a key against it).
        """
        title = "Jira Links"
        try:
            qase_cfs = await self.pools.qs(self.qase.get_case_custom_fields)
        except Exception as e:
            self.logger.log(f"[Fields] Could not list Qase custom fields: {e}", "warning")
            qase_cfs = []

        for cf in (qase_cfs or []):
            if (getattr(cf, "title", "") or "").strip().lower() == title.lower():
                cf_id = int(getattr(cf, "id"))
                await self._scope_cf_to_targets(cf, cf_id, title)
                self.mappings.jira_links_field_id = cf_id
                self.logger.log(f"[Fields] Reusing 'Jira Links' CF (id={cf_id})")
                return

        cf_id = await self.pools.qs(self.qase.create_custom_field, {
            "title": title,
            "entity": 0,  # case
            "type": 2,    # text
            "is_required": False,
            "is_visible": True,
            "is_filterable": False,
            "is_enabled_for_all_projects": False,
            "projects_codes": self._target_codes(),
        })
        if cf_id:
            self.mappings.jira_links_field_id = cf_id
            self.logger.log(f"[Fields] Created 'Jira Links' CF (id={cf_id})")
        else:
            self.logger.log(
                "[Fields] Could not create the 'Jira Links' CF, issue-link "
                "fallback will use case descriptions instead",
                "warning",
            )

    async def _sync_custom_fields(self):
        """Create (or reuse) a Qase custom field for each ZS custom field.

        ZS Cloud API v2 exposes field definitions at GET /customfields, but that
        endpoint is unavailable on some tenants (returns 404). When it fails we
        fall back to scanning the first page of test cases for each project and
        inferring field types from the values we observe.

        The mapping key is always the field NAME (string), because the
        ``customFields`` object on a test case response uses names as keys, not
        numeric IDs.
        """
        if not self.mappings.projects:
            return

        # --- Phase 1: try the /customfields endpoint ---
        # name → full ZS field definition dict
        zs_fields_by_name: dict = {}
        for proj in self.mappings.projects:
            zephyr_key = proj.get("zephyr_key", "")
            try:
                for cf in self.zephyr.get_custom_fields(zephyr_key):
                    name = (cf.get("name") or "").strip()
                    if name:
                        zs_fields_by_name[name] = cf
            except Exception as e:
                self.logger.log(
                    f"[Fields] Failed to fetch ZS custom fields for {zephyr_key}: {e}", "warning"
                )

        # --- Phase 2: fallback — discover fields from test case payloads ---
        if not zs_fields_by_name:
            self.logger.log(
                "[Fields] /customfields unavailable; discovering custom fields from test cases"
            )
            cf_samples: dict = {}  # name → first non-null value seen (or None)
            for proj in self.mappings.projects:
                zephyr_key = proj.get("zephyr_key", "")
                try:
                    gen = self.zephyr.get_test_cases(zephyr_key)
                    try:
                        first_page = next(gen)
                    except StopIteration:
                        first_page = []
                    for tc in first_page:
                        for cf_name, cf_val in (tc.get("customFields") or {}).items():
                            if cf_name not in cf_samples or (
                                cf_samples[cf_name] is None and cf_val is not None
                            ):
                                cf_samples[cf_name] = cf_val
                except Exception as e:
                    self.logger.log(
                        f"[Fields] Failed to scan TCs for {zephyr_key} CF discovery: {e}", "warning"
                    )

            for cf_name, sample_val in cf_samples.items():
                zs_fields_by_name[cf_name] = {
                    "name": cf_name,
                    "_inferred": True,
                    "_inferred_type": _infer_cf_type(cf_name, sample_val),
                }

        if not zs_fields_by_name:
            self.logger.log("[Fields] No Zephyr Essential custom fields found")
            return

        self.logger.log(f"[Fields] Found {len(zs_fields_by_name)} Zephyr Essential custom field(s)")
        self.mappings.stats.add_custom_field("zephyr-essential", len(zs_fields_by_name))

        # Load existing Qase custom fields to avoid duplicates (match by title)
        # Store: title.lower() → (id, cf object) — the object carries the
        # current scope so reuse can MERGE target codes instead of going global
        existing_qase_cfs: dict = {}
        try:
            qase_cfs = await self.pools.qs(self.qase.get_case_custom_fields)
            for cf in (qase_cfs or []):
                t = (getattr(cf, "title", None) or "").strip().lower()
                cid = getattr(cf, "id", None)
                if t and cid is not None:
                    existing_qase_cfs[t] = (int(cid), cf)
        except Exception as e:
            self.logger.log(
                f"[Fields] Failed to fetch existing Qase custom fields: {e}", "warning"
            )

        for name, zs_cf in zs_fields_by_name.items():
            name_lower = name.lower()
            if name_lower in existing_qase_cfs:
                qase_cf_id, cf_obj = existing_qase_cfs[name_lower]
                self.logger.log(f"[Fields] Reusing existing Qase CF '{name}' (id={qase_cf_id})")
                # Make it usable in the target projects WITHOUT widening its
                # scope anywhere else (case creation 422s if the field isn't
                # scoped to the project).
                await self._scope_cf_to_targets(cf_obj, qase_cf_id, name)
            else:
                if zs_cf.get("_inferred"):
                    qase_type = zs_cf["_inferred_type"]
                    # For inferred multiselect we don't know options → fall back to text
                    if qase_type == 6:
                        qase_type = 1
                else:
                    zs_type_name = (zs_cf.get("type") or {}).get("name", "plain_text").lower()
                    qase_type = _CF_TYPE_MAP.get(zs_type_name, 2)

                cf_data = {
                    "title": name,
                    "entity": 0,  # 0 = case
                    "type": qase_type,
                    "is_required": False,
                    "is_visible": True,
                    "is_filterable": True,
                    # Scope to the target projects ONLY — CFs are workspace-
                    # global, and enabling for all projects would make the new
                    # field appear in every pre-existing project.
                    "is_enabled_for_all_projects": False,
                    "projects_codes": self._target_codes(),
                }

                # Carry over option names for select/multiselect from /customfields data
                options = zs_cf.get("options") or []
                if options and qase_type in (3, 6):
                    cf_data["value"] = [
                        {"title": str(opt.get("name") or opt), "default": False}
                        for opt in options if opt
                    ]

                qase_cf_id = await self.pools.qs(self.qase.create_custom_field, cf_data)
                if qase_cf_id:
                    self.logger.log(
                        f"[Fields] Created Qase CF '{name}' (id={qase_cf_id}, type={qase_type})"
                    )
                    existing_qase_cfs[name_lower] = (qase_cf_id, None)
                else:
                    self.logger.log(f"[Fields] Failed to create Qase CF '{name}'", "warning")
                    continue

            # Map by field NAME — the TC customFields dict uses names as keys
            self.mappings.custom_fields[name] = qase_cf_id
            self.mappings.stats.add_custom_field("qase", 1)

        self.logger.log(f"[Fields] Custom field mapping: {self.mappings.custom_fields}")
