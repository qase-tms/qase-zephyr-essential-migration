import asyncio
import re

from qaseio.models import TestCasebulkCasesInner

from ...service.qase import QaseService
from ...service.zephyr_essential import ZephyrEssentialService
from ...support.config_manager import ConfigManager
from ...support.html import extract_img_urls as _extract_img_urls
from ...support.html import strip_html as _strip_html
from ...support.links import format_issue_links
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools
from ...support.zephyr_session import get_cdn_cookies

# Zephyr Essential priority names → Qase priority slugs.
# Qase's priority field options are high / medium / low ONLY (verified against
# GET /system_field) — mapping to "normal"/"critical" silently drops the
# priority (bug inherited from the ZS script, caught by stress test).
_PRIORITY_MAP = {
    "highest": "high",
    "high": "high",
    "normal": "medium",
    "medium": "medium",
    "low": "low",
    "lowest": "low",
}

# Zephyr Essential status names → Qase status slugs
_STATUS_MAP = {
    "approved": "actual",
    "draft": "draft",
    "deprecated": "deprecated",
}


class Cases:
    """Import Zephyr Essential test cases → Qase test cases.

    For each project, fetches all test cases (paginated), resolves their suite
    (folder) mapping, fetches step details, then bulk-creates in Qase.
    """

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

    def import_cases(self, project: dict):
        self._warned_priorities = set()
        self._current_code = project.get("code")
        # Preserve Zephyr case numbers: ZEM-T123 → Qase case id 123. ON by
        # default (missing key = true; delta migrations depend on it). Safe
        # only into an empty target project (the re-run guard enforces empty
        # by default); suffixes are unique per Zephyr project so no in-project
        # collisions. Same mechanism as ZE's zephyr_qase_case_id_in_request.
        _pid = self.config.get("cases.preserve_ids")
        self._preserve_ids = True if _pid is None else bool(_pid)
        if self._preserve_ids:
            self.logger.log(
                f"[{self._current_code}][Cases] cases.preserve_ids is ON, Zephyr key "
                f"suffixes will be sent as Qase case ids"
            )
        asyncio.run(self._import_cases_async(project))

    def _warn_unmapped_priority(self, priority_name: str):
        if priority_name in getattr(self, "_warned_priorities", set()):
            return
        self._warned_priorities.add(priority_name)
        self.logger.log(
            f"[Cases] Unmapped Zephyr priority {priority_name!r} → 'medium'. "
            f'Override via config: "cases": {{"priority_map": {{"{priority_name}": "high"}}}}',
            "warning",
        )
        self.mappings.stats.add_issue(
            self._current_code or "-", "case",
            f"priority {priority_name!r} has no mapping, defaulted to 'medium' "
            f"(add it to cases.priority_map)",
        )

    async def _import_cases_async(self, project: dict):
        code = project["code"]
        zephyr_key = project["zephyr_key"]

        # Jira credentials for downloading genuine Jira-issue attachments
        jira_email = str(self.config.get("jira.email") or "").strip()
        jira_api_token = str(self.config.get("jira.api_token") or "").strip()
        # Attachment binaries / inline images are reachable only via a captured
        # browser session (zephyr.attachments.*). Prime it once here (emits the
        # disclosure / first token mint); each case re-fetches the jar below, so
        # a run longer than the Forge token's ~30-min life re-mints mid-flight.
        await self.pools.source(get_cdn_cookies, self.config, self.logger)

        # Load Qase system field option ids (populated by Fields step)
        priority_map = self.mappings.qase_priority_keys_to_id
        status_map = self.mappings.qase_case_status_keys_to_id

        # Optional Jira enrichment: TestCase.component is {id, self} with no
        # name inline — resolve id → name so the component tag survives.
        self._component_names = await self.pools.source(
            self.zephyr.get_component_names, zephyr_key
        )

        # Pre-fetch Zephyr Essential priority/status name maps.
        # The API returns priority/status as {id, self} objects — name is not inline.
        zephyr_priority_id_to_name: dict = {}
        for p in self.zephyr.get_priorities(zephyr_key):
            pid = p.get("id")
            pname = (p.get("name") or "").lower().strip()
            if pid is not None and pname:
                zephyr_priority_id_to_name[int(pid)] = pname

        zephyr_status_id_to_name: dict = {}
        for s in self.zephyr.get_statuses(zephyr_key, "TEST_CASE"):
            sid = s.get("id")
            sname = (s.get("name") or "").lower().strip()
            if sid is not None and sname:
                zephyr_status_id_to_name[int(sid)] = sname

        self.logger.log(
            f"[{code}][Cases] Loaded {len(zephyr_priority_id_to_name)} priority(s), "
            f"{len(zephyr_status_id_to_name)} TC status(es) from Zephyr Essential"
        )

        # Fetch all pages of test cases from Zephyr Essential
        self.logger.log(f"[{code}][Cases] Fetching test cases from Zephyr Essential")
        all_cases = []
        for page in self.zephyr.get_test_cases(zephyr_key):
            all_cases.extend(page)

        if not all_cases:
            self.logger.log(f"[{code}][Cases] No test cases found")
            return

        total = len(all_cases)
        self.logger.log(f"[{code}][Cases] Found {total} test case(s)")
        self.mappings.stats.add_entity_count(code, "cases", "zephyr-essential", total)

        # Delta mode: cases whose preserved id already exists in the target are
        # skipped — but still REGISTERED in the mapping, so new cycles that
        # execute old cases resolve to the right Qase case. Requires
        # cases.preserve_ids (enforced at startup).
        existing_case_ids = (
            (self.mappings.existing_state.get(code) or {}).get("case_ids") or set()
        )
        if existing_case_ids:
            remaining = []
            skipped_existing = 0
            unmatchable = 0
            for tc in all_cases:
                tc_key = tc.get("key") or ""
                m = re.search(r"-T(\d+)$", tc_key)
                if m and int(m.group(1)) in existing_case_ids:
                    qase_id = int(m.group(1))
                    self.mappings.register_zephyr_testcase_qase_case_id(code, tc_key, qase_id)
                    if tc.get("id") is not None:
                        self.mappings.register_zephyr_testcase_qase_case_id(
                            code, str(tc["id"]), qase_id
                        )
                    skipped_existing += 1
                elif not m:
                    # No numeric suffix → cannot delta-match; creating it would
                    # duplicate on EVERY delta run, so skip with a report entry.
                    unmatchable += 1
                    self.logger.log(
                        f"[{code}][Cases] {tc_key}: no -T<number> suffix, cannot "
                        f"delta-match, skipped", "warning",
                    )
                    self.mappings.stats.add_issue(
                        code, "case",
                        f"{tc_key}: skipped in delta (no numeric suffix to match on)",
                        "error",
                    )
                else:
                    remaining.append(tc)
            all_cases = remaining
            self.logger.log(
                f"[{code}][Cases] Delta: {skipped_existing} case(s) already in target, "
                f"{len(all_cases)} new to migrate"
            )
            self.mappings.stats.add_issue(
                code, "case",
                f"delta: skipped {skipped_existing} case(s) already in the target"
                + (f", {unmatchable} unmatchable" if unmatchable else ""),
                "info",
            )
            if not all_cases:
                self.logger.log(f"[{code}][Cases] Nothing new to migrate")
                return

        # Fetch steps and attachments for all cases concurrently
        self.logger.log(f"[{code}][Cases] Fetching steps for {total} case(s)")
        steps_cache = {}
        attachments_cache = {}  # {tc_key: [qase_hash, ...]}
        # {tc_key: [filename, ...]} — attachments whose binaries the Essential
        # API refuses to serve (metadata is readable, download 401s). Their
        # names are appended to the case description so nothing is lost silently.
        self._missed_attachments = {}
        # {tc_key: {"type": "bdd"|"plain", "text": ...}} — script-type cases
        self._scripts_cache = {}
        # {tc_key: {"issues": [...], "webLinks": [...]}} and issue id → key map
        self._links_cache = {}
        self._issue_key_map = {}
        # {tc_key: {src_url: {"name", "url"(qase)}}} — inline rich-text images,
        # uploaded to Qase and re-linked in the description markdown (kept OUT of
        # the file-attachment bucket so they render in place instead).
        self._inline_images = {}
        semaphore = asyncio.Semaphore(16)

        async def fetch_steps(tc_key: str):
            async with semaphore:
                try:
                    steps = await self.pools.source(self.zephyr.get_test_steps, tc_key)
                    steps_cache[tc_key] = steps or []
                except Exception:
                    # BDD / plain-script cases have no teststeps (the endpoint
                    # 4xxs) — their content lives on /testscript instead.
                    steps_cache[tc_key] = []
                    try:
                        script = await self.pools.source(
                            self.zephyr.get_test_script, tc_key
                        )
                    except Exception:
                        script = None
                    if script and (script.get("text") or "").strip():
                        self._scripts_cache[tc_key] = script
                        self.logger.log(
                            f"[{code}][Cases] {tc_key} is a "
                            f"{script.get('type') or 'script'}-type case, migrating "
                            f"its test script into the description"
                        )
                    else:
                        self.logger.log(
                            f"[{code}][Cases] Failed to fetch steps for {tc_key} "
                            f"and no test script found, case migrates without steps",
                            "warning",
                        )
                        self.mappings.stats.add_issue(
                            code, "case",
                            f"{tc_key}: no steps and no test script, migrated without steps",
                        )

        async def fetch_and_upload_attachments(tc: dict):
            tc_key = tc.get("key") or ""
            async with semaphore:
                try:
                    # Fetch the session jar fresh (cached; re-mints only when the
                    # Forge token is near expiry) so long runs stay authenticated.
                    cdn_cookies = await self.pools.source(
                        get_cdn_cookies, self.config, self.logger
                    )
                    app_jwt = cdn_cookies.get("Bearer")
                    rest_base = cdn_cookies.get("atm-rest-base")
                    hashes = []

                    # 1. File attachments. Binaries are reachable only via the app
                    # backend's pre-signed S3 URLs (needs the browser-session app
                    # JWT). Without a session, fall back to the public v2 metadata
                    # so at least the filename is noted.
                    if app_jwt and rest_base:
                        records = await self.pools.source(
                            self.zephyr.get_case_attachment_records,
                            tc_key, rest_base, app_jwt,
                        )
                        atts = [{"url": r["url"], "filename": r["name"]} for r in (records or [])]
                    else:
                        atts = await self.pools.source(
                            self.zephyr.get_test_case_attachments, tc_key
                        )
                    for att in (atts or []):
                        att_url = att.get("url") or ""
                        att_name = att.get("filename") or "attachment"
                        if not att_url:
                            continue
                        if app_jwt and rest_base:
                            raw = await self.pools.source(
                                self.zephyr.download_presigned, att_url
                            )
                        else:
                            raw = await self.pools.source(
                                self.zephyr.download_attachment_bytes,
                                att_url, jira_email, jira_api_token, cdn_cookies,
                            )
                        if raw is None:
                            self.logger.log(
                                f"[{code}][Cases] Could not download attachment "
                                f"{att_name!r} for {tc_key}, noting the filename in "
                                f"the description", "warning"
                            )
                            self._missed_attachments.setdefault(tc_key, []).append(att_name)
                            self.mappings.stats.add_issue(
                                code, "attachment",
                                f"{tc_key}: {att_name!r} not downloadable, filename noted "
                                f"in the description",
                            )
                            continue
                        result = await self.pools.qs(
                            self.qase.upload_attachment, code, (att_name, raw)
                        )
                        if result and result.get("hash"):
                            hashes.append(result["hash"])

                    # 2. Inline images embedded in objective / precondition /
                    # description HTML. These are re-linked INLINE in the Qase
                    # description (markdown), not filed as case attachments — so
                    # collect a {src_url: {name, qase_url}} map, dedup by src url.
                    html_fields = [
                        tc.get("objective") or "",
                        tc.get("precondition") or "",
                        tc.get("description") or "",
                    ]
                    seen_src = set()
                    for html in html_fields:
                        for img_url in _extract_img_urls(html):
                            if img_url in seen_src:
                                continue
                            seen_src.add(img_url)
                            # Derive filename from URL
                            raw_name = (img_url.rstrip("/").split("/")[-1]
                                        .split("?")[0].replace("+", " "))
                            cdn_m = re.match(r'^[a-f0-9-]+-\d+-(.+)$', raw_name)
                            img_name = cdn_m.group(1) if cdn_m else raw_name or "inline_image"
                            raw = await self.pools.source(
                                self.zephyr.download_attachment_bytes,
                                img_url, jira_email, jira_api_token, cdn_cookies,
                            )
                            if raw is None:
                                # No bytes → the description keeps a [Image: name]
                                # text note (via _strip_html) so nothing is lost.
                                self.logger.log(
                                    f"[{code}][Cases] Could not download inline image "
                                    f"{img_name!r} for {tc_key}, keeping a text note "
                                    f"in the description", "warning"
                                )
                                continue
                            result = await self.pools.qs(
                                self.qase.upload_attachment, code, (img_name, raw)
                            )
                            if result and result.get("url"):
                                self._inline_images.setdefault(tc_key, {})[img_url] = {
                                    "name": img_name, "url": result["url"],
                                }

                    if hashes:
                        attachments_cache[tc_key] = hashes
                    n_inline = len(self._inline_images.get(tc_key) or {})
                    if hashes or n_inline:
                        self.logger.log(
                            f"[{code}][Cases] {tc_key}: {len(hashes)} file attachment(s), "
                            f"{n_inline} inline image(s) migrated"
                        )
                except Exception as e:
                    self.logger.log(
                        f"[{code}][Cases] Attachment processing failed for {tc_key}: {e}", "warning"
                    )

        async def fetch_links(tc_key: str):
            async with semaphore:
                try:
                    links = await self.pools.source(self.zephyr.get_test_case_links, tc_key)
                    if links and (links.get("issues") or links.get("webLinks")):
                        self._links_cache[tc_key] = links
                except Exception as e:
                    self.logger.log(
                        f"[{code}][Cases] Failed to fetch links for {tc_key}: {e}", "warning"
                    )

        tc_keys_with_key = [tc.get("key") or "" for tc in all_cases if tc.get("key")]
        await asyncio.gather(*[fetch_steps(k) for k in tc_keys_with_key])
        await asyncio.gather(*[fetch_links(k) for k in tc_keys_with_key])
        await asyncio.gather(*[fetch_and_upload_attachments(tc) for tc in all_cases if tc.get("key")])

        # Resolve all linked Jira issue ids → keys in one batched lookup
        # (links only carry numeric ids; needs Jira enrichment).
        all_issue_ids = sorted({
            str(link.get("issueId"))
            for links in self._links_cache.values()
            for link in (links.get("issues") or [])
            if link.get("issueId") is not None
        })
        if all_issue_ids:
            self._issue_key_map = await self.pools.source(
                self.zephyr.resolve_issue_keys, all_issue_ids
            )
            unresolved = len(all_issue_ids) - len(self._issue_key_map)
            self.logger.log(
                f"[{code}][Cases] {sum(len(l.get('issues') or []) for l in self._links_cache.values())} "
                f"Jira issue link(s) across {len(self._links_cache)} case(s); "
                f"{len(self._issue_key_map)}/{len(all_issue_ids)} issue id(s) resolved to keys"
            )
            if unresolved:
                self.logger.log(
                    f"[{code}][Cases] {unresolved} linked Jira issue(s) could not be resolved "
                    f"to keys{'' if self.zephyr.jira else ' (Jira enrichment disabled)'}, "
                    f"their REST URLs are noted in the case descriptions instead",
                    "warning",
                )

        n_with_att = sum(1 for k in tc_keys_with_key if k in attachments_cache)
        if n_with_att:
            self.logger.log(f"[{code}][Cases] {n_with_att} case(s) have attachment(s)")

        # Build Qase case objects
        suite_map = self.mappings.suites.get(code) or {}
        qase_cases = []
        for tc in all_cases:
            case_obj = self._build_qase_case(
                tc, suite_map, steps_cache, attachments_cache,
                priority_map, status_map,
                zephyr_priority_id_to_name, zephyr_status_id_to_name,
            )
            if case_obj is not None:
                # Keep both the string key (e.g. "ZSM-T2") and the numeric id so that
                # run executions (which only expose testCase.id) can still be resolved.
                qase_cases.append((tc.get("key") or "", tc.get("id"), case_obj))

        if not qase_cases:
            self.logger.log(f"[{code}][Cases] Nothing to create after mapping")
            return

        # Bulk create in Qase (100 per chunk with per-case fallback)
        tc_keys = [k for k, _, _ in qase_cases]
        tc_ids = [tid for _, tid, _ in qase_cases]
        case_objs = [c for _, _, c in qase_cases]

        self.logger.log(f"[{code}][Cases] Creating {len(case_objs)} case(s) in Qase")
        _, qase_ids = self.qase.create_cases(code, case_objs)

        created = 0
        for i, qase_id in enumerate(qase_ids):
            if qase_id is not None:
                if tc_keys[i]:
                    self.mappings.register_zephyr_testcase_qase_case_id(code, tc_keys[i], qase_id)
                if tc_ids[i] is not None:
                    self.mappings.register_zephyr_testcase_qase_case_id(code, str(tc_ids[i]), qase_id)
                created += 1
            else:
                self.mappings.stats.add_issue(
                    code, "case",
                    f"{tc_keys[i] or f'row {i}'}: FAILED to create in Qase (see log for "
                    f"the rejected payload)",
                    "error",
                )

        self.mappings.stats.add_entity_count(code, "cases", "qase", created)
        self.logger.log(
            f"[{code}][Cases] Created {created}/{len(case_objs)} case(s) in Qase"
        )

        # Attach resolved Jira issue links as native Qase external issues.
        # Native attach requires the workspace's Jira Cloud integration —
        # Qase validates every key against it ("Issue not found." otherwise).
        # Cases whose attach fails get ALL their issue links written to the
        # "Jira Links" custom field instead, so nothing is lost either way.
        attach_entries = []  # ({case_id, external_issues}, all issue links of the case)
        for i, qase_id in enumerate(qase_ids):
            if qase_id is None or not tc_keys[i]:
                continue
            issue_links = (self._links_cache.get(tc_keys[i]) or {}).get("issues") or []
            keys = sorted({
                self._issue_key_map[str(link["issueId"])]
                for link in issue_links
                if str(link.get("issueId")) in self._issue_key_map
            })
            if keys:
                attach_entries.append(
                    ({"case_id": int(qase_id), "external_issues": keys}, issue_links)
                )

        failed_entries = []
        if attach_entries:
            # Probe with one case first: if Qase can't resolve a key we just
            # resolved against Jira, the integration is missing/mismatched and
            # every attach would fail identically — skip straight to fallback
            # instead of logging one error per case.
            if self.qase.attach_external_issues(code, [attach_entries[0][0]]):
                for start in range(1, len(attach_entries), 100):
                    chunk = attach_entries[start:start + 100]
                    if not self.qase.attach_external_issues(code, [e[0] for e in chunk]):
                        for entry in chunk:
                            if not self.qase.attach_external_issues(code, [entry[0]]):
                                failed_entries.append(entry)
            else:
                failed_entries = list(attach_entries)

        if failed_entries:
            fid = getattr(self.mappings, "jira_links_field_id", None)
            target = "the 'Jira Links' custom field" if fid else "the case descriptions"
            self.logger.log(
                f"[{code}][Links] Native external-issue attach unavailable for "
                f"{len(failed_entries)} case(s), connect the Jira Cloud integration in "
                f"Qase (workspace Apps) to get native links. Writing the links to "
                f"{target} instead.",
                "warning",
            )
            for payload, issue_links in failed_entries:
                parts = format_issue_links(issue_links, self._issue_key_map)
                if fid:
                    fields = {"custom_field": {str(fid): "\n".join(parts)}}
                else:
                    fields = {"description": "🔗 Linked Jira issues: " + " · ".join(parts)}
                self.qase.update_case_fields(code, payload["case_id"], fields)

    def _build_qase_case(
        self,
        tc: dict,
        suite_map: dict,
        steps_cache: dict,
        attachments_cache: dict,
        priority_map: dict,
        status_map: dict,
        zephyr_priority_id_to_name: dict,
        zephyr_status_id_to_name: dict,
    ):
        """Map a Zephyr Essential test case to a Qase TestCasebulkCasesInner object."""
        tc_key = tc.get("key") or ""
        title = (tc.get("name") or "").strip()
        if not title:
            title = tc_key or "Untitled"

        # Suite / folder mapping
        folder = tc.get("folder") or {}
        folder_id = folder.get("id") if isinstance(folder, dict) else None
        suite_id = suite_map.get(folder_id) if folder_id else None

        # Description / preconditions. Pass the inline-image map so <img> tags
        # render inline (markdown) pointing at the uploaded Qase attachment.
        img_map = (getattr(self, "_inline_images", {}) or {}).get(tc_key, {})
        description = _strip_html(tc.get("objective") or tc.get("description") or "", img_map)

        # BDD / plain-script cases: the script is the test content — carry it
        # into the description (Qase bulk create has no script field).
        script = (getattr(self, "_scripts_cache", {}) or {}).get(tc_key)
        if script:
            script_type = (script.get("type") or "").lower()
            lang = "gherkin" if script_type == "bdd" else ""
            label = "Test script (BDD)" if script_type == "bdd" else "Test script"
            block = f"**{label}:**\n\n```{lang}\n{script.get('text', '').strip()}\n```"
            description = f"{description}\n\n{block}" if description else block

        # Attachments the API wouldn't serve → keep at least their names visible
        missed = (getattr(self, "_missed_attachments", {}) or {}).get(tc_key)
        if missed:
            note = "📎 Attachments in source (not migrated, could not be downloaded): " + ", ".join(missed)
            description = f"{description}\n\n{note}" if description else note

        # Web links → markdown note in the description. Jira issue links are
        # handled separately: native external-issue attach after creation,
        # with the "Jira Links" custom field as fallback.
        links = (getattr(self, "_links_cache", {}) or {}).get(tc_key) or {}
        web_links = links.get("webLinks") or []
        if web_links:
            parts = [
                f"[{(w.get('description') or w.get('url') or 'link').strip()}]({w.get('url')})"
                for w in web_links if w.get("url")
            ]
            if parts:
                note = "🔗 Web links: " + " · ".join(parts)
                description = f"{description}\n\n{note}" if description else note
        preconditions = _strip_html(tc.get("precondition") or "", img_map)

        # Priority — API returns {id, self}; resolve name via pre-fetched map
        priority_obj = tc.get("priority") or {}
        if isinstance(priority_obj, dict):
            priority_name = (priority_obj.get("name") or "").lower().strip()
            if not priority_name:
                pid = priority_obj.get("id")
                priority_name = zephyr_priority_id_to_name.get(int(pid), "") if pid is not None else ""
        else:
            priority_name = str(priority_obj).lower().strip()
        qase_priority_slug = _PRIORITY_MAP.get(priority_name)
        if qase_priority_slug is None:
            # Custom Zephyr priority (e.g. "Urgent") — allow a config override,
            # else default to medium and tell the user on the console.
            override = (self.config.get("cases.priority_map") or {}).get(priority_name)
            qase_priority_slug = str(override).lower() if override else "medium"
            if not override and priority_name:
                self._warn_unmapped_priority(priority_name)
        priority_id = priority_map.get(qase_priority_slug) or priority_map.get("medium")

        # Status — same id-only pattern
        status_obj = tc.get("status") or {}
        if isinstance(status_obj, dict):
            status_name = (status_obj.get("name") or "").lower().strip()
            if not status_name:
                sid = status_obj.get("id")
                status_name = zephyr_status_id_to_name.get(int(sid), "") if sid is not None else ""
        else:
            status_name = str(status_obj).lower().strip()
        qase_status_slug = _STATUS_MAP.get(status_name, "actual")
        status_id = status_map.get(qase_status_slug) or status_map.get("actual")

        # Steps
        steps = self._build_steps(steps_cache.get(tc_key) or [], tc_key)

        # Preserved case id from the key suffix (ZEM-T123 → 123)
        preserved_id = None
        if getattr(self, "_preserve_ids", False) and tc_key:
            m = re.search(r"-T(\d+)$", tc_key)
            if m:
                preserved_id = int(m.group(1))
            else:
                self.logger.log(
                    f"[Cases] {tc_key}: cases.preserve_ids is on but the key has no "
                    f"-T<number> suffix, Qase will assign the id",
                    "warning",
                )
                self.mappings.stats.add_issue(
                    self._current_code or "-", "case",
                    f"{tc_key}: id not preserved (key has no numeric suffix)",
                )

        if len(title) > 255:
            self.logger.log(
                f"[Cases] {tc_key}: title exceeds 255 chars ({len(title)}), truncated",
                "warning",
            )
        data = {
            "title": title[:255],
            "description": description or None,
            "preconditions": preconditions or None,
            "steps": steps or None,
        }

        if preserved_id is not None:
            data["id"] = preserved_id
        if suite_id is not None:
            data["suite_id"] = suite_id
        if priority_id is not None:
            data["priority"] = priority_id
        if status_id is not None:
            data["status"] = status_id

        # Attachments uploaded during pre-fetch
        att_hashes = attachments_cache.get(tc_key) or []
        if att_hashes:
            data["attachments"] = att_hashes

        # Tags: labels + component (when set)
        labels = list(tc.get("labels") or [])
        component = (tc.get("component") or {})
        if isinstance(component, dict):
            component_name = (component.get("name") or "").strip()
            if not component_name and component.get("id") is not None:
                # Essential returns component as {id, self} — resolve via the
                # Jira enrichment map (empty when jira.* is not configured)
                component_name = (
                    getattr(self, "_component_names", {}) or {}
                ).get(str(component["id"]), "")
        else:
            component_name = str(component).strip()
        if component_name:
            labels.append(component_name)
        if labels:
            tags = [str(lbl) for lbl in labels if lbl]
            if len(tags) > 10:
                self.logger.log(
                    f"[Cases] {tc_key}: {len(tags)} tags, Qase accepts 10, dropping: "
                    f"{tags[10:]}", "warning",
                )
            data["tags"] = tags[:10]

        data["is_flaky"] = 0

        # Case owner (Atlassian account id) → Qase author, only when the
        # customer mapped it explicitly via users.map (otherwise Qase's
        # default author — the API token owner — is kept).
        owner = tc.get("owner")
        if owner is not None and str(owner).strip() in self.mappings.user_map:
            data["author_id"] = self.mappings.user_map[str(owner).strip()]

        # Custom fields
        custom_fields = tc.get("customFields") or {}
        cf_map = self.mappings.custom_fields or {}
        if custom_fields and cf_map:
            qase_cf = {}
            for cf_name, value in custom_fields.items():
                qase_cf_id = cf_map.get(cf_name)
                if qase_cf_id is None or value is None:
                    continue
                if isinstance(value, bool):
                    if not value:
                        continue  # False (unchecked) is Qase's default; Qase silently drops "false" anyway
                    serialised = "true"
                elif isinstance(value, list):
                    if not value:
                        continue
                    parts = []
                    for v in value:
                        if isinstance(v, dict):
                            parts.append(str(v.get("name") or v.get("value") or next(iter(v.values()), "")))
                        else:
                            parts.append(str(v))
                    serialised = ",".join(parts)
                elif isinstance(value, str):
                    # Strip ISO time suffix from date values ("2026-06-30T00:00:00Z" → "2026-06-30")
                    if "T" in value and value.endswith("Z"):
                        serialised = value.split("T")[0]
                    elif "<" in value:
                        serialised = _strip_html(value)
                    else:
                        serialised = value
                    if not serialised:
                        continue
                else:
                    serialised = str(value)
                qase_cf[str(qase_cf_id)] = serialised
            if qase_cf:
                data["custom_field"] = qase_cf

        # Issue links whose keys could not be resolved (Jira enrichment off,
        # or issue invisible to the token) → "Jira Links" CF at create time
        # so they are never lost. Resolved links are attached natively after
        # creation, falling back to this same CF.
        unresolved_links = [
            link for link in (links.get("issues") or [])
            if str(link.get("issueId")) not in (getattr(self, "_issue_key_map", {}) or {})
        ]
        if unresolved_links:
            fid = getattr(self.mappings, "jira_links_field_id", None)
            parts = format_issue_links(unresolved_links, self._issue_key_map)
            if fid:
                data.setdefault("custom_field", {})[str(fid)] = "\n".join(parts)
            else:
                note = "🔗 Linked Jira issues: " + ", ".join(parts)
                desc = data.get("description") or ""
                data["description"] = f"{desc}\n\n{note}" if desc else note

        return TestCasebulkCasesInner(**data)

    def _build_steps(self, raw_steps: list, tc_key: str = "") -> list:
        """Convert Zephyr Essential step objects to Qase step dicts.

        A step is either ``{"inline": {...}, "testCase": null}`` or a
        call-to-test ``{"inline": null, "testCase": {"testCaseKey": ...}}`` -
        the latter is rendered as a textual "Call to test" step, because Qase
        bulk create cannot reference shared steps.
        """
        steps = []
        for step in raw_steps:
            inline = step.get("inline") or {}
            called = step.get("testCase") or {}
            if not inline and called:
                called_key = (called.get("testCaseKey") or "").strip()
                action = f"→ Call to test case {called_key or '(unknown)'}, execute its steps here"
                steps.append({"action": action, "expected_result": None, "data": None})
                self.logger.log(
                    f"[Cases] {tc_key}: call-to-test step → rendered as a text step "
                    f"referencing {called_key or 'an unknown case'}"
                )
                continue

            action = _strip_html(inline.get("description") or step.get("description") or "")
            expected = _strip_html(inline.get("expectedResult") or step.get("expectedResult") or "")
            test_data = _strip_html(inline.get("testData") or step.get("testData") or "")

            if not action:
                action = "Step"

            for label, value in (("action", action), ("expected result", expected), ("data", test_data)):
                if value and len(value) > 1000:
                    self.logger.log(
                        f"[Cases] {tc_key}: step {label} exceeds 1000 chars "
                        f"({len(value)}), truncated", "warning",
                    )
            step_data = {
                "action": action[:1000],
                "expected_result": expected[:1000] if expected else None,
                "data": test_data[:1000] if test_data else None,
            }
            steps.append(step_data)
        return steps
