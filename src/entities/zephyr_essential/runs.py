import asyncio
import re
import time

from ...service.qase import QaseService
from ...service.zephyr_essential import ZephyrEssentialService
from ...support.config_manager import ConfigManager
from ...support.html import extract_img_urls, strip_html
from ...support.links import format_issue_links as _format_issue_links
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools
from ...support.zephyr_session import get_cdn_cookies

# Zephyr Essential execution status names → Qase result status slugs
_EXEC_STATUS_MAP = {
    "pass": "passed",
    "passed": "passed",
    "fail": "failed",
    "failed": "failed",
    "blocked": "blocked",
    "in progress": "in_progress",
    "not executed": "untested",
    "unexecuted": "untested",
    "wip": "in_progress",
    "skip": "skipped",
    "skipped": "skipped",
}

# Statuses we skip (leave the case as untested in the Qase run)
_SKIP_STATUSES = {"in_progress", "untested"}

# Identity map so send_bulk_results can resolve our Qase slugs (it expects a mapping dict)
_RESULT_STATUSES = {
    "passed": "passed",
    "failed": "failed",
    "blocked": "blocked",
    "skipped": "skipped",
    "in_progress": "in_progress",
    "untested": "untested",
    "invalid": "invalid",
}


def _now_ts() -> int:
    return int(time.time())


class Runs:
    """Import Zephyr Essential test cycles + executions → Qase test runs + results."""

    def __init__(
        self,
        qase_service: QaseService,
        source_service: ZephyrEssentialService,
        logger: Logger,
        mappings: Mappings,
        config: ConfigManager,
        project: dict,
        pools: Pools,
    ):
        self.qase = qase_service
        self.zephyr = source_service
        self.config = config
        self.logger = logger
        self.mappings = mappings
        self.project = project
        self.pools = pools

    def import_runs(self) -> Mappings:
        return asyncio.run(self._import_runs_async())

    async def _import_runs_async(self) -> Mappings:
        code = self.project["code"]
        zephyr_key = self.project["zephyr_key"]
        created_after = int(self.config.get("runs.created_after") or 0)

        jira_email = str(self.config.get("jira.email") or "").strip()
        jira_api_token = str(self.config.get("jira.api_token") or "").strip()

        # Prime the browser session once (emits the disclosure / first token
        # mint). Each execution re-fetches the jar below, so a long run re-mints
        # the Forge token mid-flight instead of replaying a dead one.
        await self.pools.source(get_cdn_cookies, self.config, self.logger)
        # Jira numeric project id (for the cycle attachment endpoint), resolved
        # lazily once per project from an execution.
        self._jira_project_id = None

        # Pre-fetch execution statuses so we can resolve numeric ids to names.
        # The execution objects only carry testExecutionStatus.id, not the name.
        exec_status_id_to_name: dict = {}
        try:
            for s in self.zephyr.get_statuses(zephyr_key, "TEST_EXECUTION"):
                sid = s.get("id")
                sname = (s.get("name") or "").lower().strip()
                if sid is not None and sname:
                    exec_status_id_to_name[int(sid)] = sname
            self.logger.log(
                f"[{code}][Runs] Loaded {len(exec_status_id_to_name)} execution status(es)"
            )
        except Exception as e:
            self.logger.log(f"[{code}][Runs] Failed to fetch execution statuses: {e}", "warning")

        # Same for TEST_CYCLE statuses — the cycle 'status' is an {id, self}
        # object, so we resolve its id → name to decide whether the run is done.
        self._cycle_status_id_to_name = {}
        try:
            for s in self.zephyr.get_statuses(zephyr_key, "TEST_CYCLE"):
                sid = s.get("id")
                sname = (s.get("name") or "").lower().strip()
                if sid is not None and sname:
                    self._cycle_status_id_to_name[int(sid)] = sname
        except Exception as e:
            self.logger.log(f"[{code}][Runs] Failed to fetch cycle statuses: {e}", "warning")

        # Step-level results are always migrated (costs one extra GET per
        # execution — accepted as part of full-fidelity migration).
        self._migrate_step_results = True

        # Environments: Zephyr env id → name, mirrored to Qase environments.
        # An execution references its environment as {id, self}; when every
        # execution in a cycle shares one environment the Qase run gets
        # environment_id, otherwise each result comment notes its own.
        self._zephyr_env_names = {}
        self._env_lock = asyncio.Lock()
        # Environments are always migrated (cheap: one GET per project).
        self._migrate_environments = True
        if self._migrate_environments:
            try:
                envs = await self.pools.source(self.zephyr.get_environments, zephyr_key)
                self._zephyr_env_names = {
                    int(e["id"]): (e.get("name") or "").strip()
                    for e in (envs or [])
                    if e.get("id") is not None and (e.get("name") or "").strip()
                }
                if self._zephyr_env_names:
                    self.mappings.stats.add_entity_count(
                        code, "environments", "zephyr-essential", len(self._zephyr_env_names)
                    )
                    self.logger.log(
                        f"[{code}][Runs] Found {len(self._zephyr_env_names)} Zephyr "
                        f"environment(s): {sorted(self._zephyr_env_names.values())}"
                    )
                    # Existing Qase environments (dedup by title, case-insensitive)
                    self.mappings.environments.setdefault(code, {}).update(
                        await self.pools.qs(self.qase.get_environments, code)
                    )
            except Exception as e:
                self.logger.log(f"[{code}][Runs] Environment fetch failed: {e}", "warning")

        # Optional Jira enrichment: cycle.jiraProjectVersion is {id, self} —
        # resolve id → version name so runs keep their release context.
        self._version_names = await self.pools.source(
            self.zephyr.get_version_names, zephyr_key
        )

        self.logger.log(f"[{code}][Runs] Fetching test cycles from Zephyr Essential")

        all_cycles = []
        for page in self.zephyr.get_test_cycles(zephyr_key, created_after):
            all_cycles.extend(page)

        if not all_cycles:
            self.logger.log(f"[{code}][Runs] No test cycles found")
            return self.mappings

        total = len(all_cycles)
        self.logger.log(f"[{code}][Runs] Found {total} cycle(s)")
        self.mappings.stats.add_entity_count(code, "runs", "zephyr-essential", total)

        created = 0

        async def import_cycle(cycle: dict):
            nonlocal created
            qase_run_id = await self._import_one_cycle(
                code, zephyr_key, cycle, exec_status_id_to_name,
                jira_email, jira_api_token,
            )
            if qase_run_id is not None:
                created += 1

        await asyncio.gather(*[import_cycle(cycle) for cycle in all_cycles])

        self.mappings.stats.add_entity_count(code, "runs", "qase", created)
        skipped_runs = getattr(self, "_delta_skipped_runs", 0)
        if skipped_runs:
            self.mappings.stats.add_issue(
                code, "run", f"delta: skipped {skipped_runs} run(s) already in the target",
                "info",
            )
        self.logger.log(
            f"[{code}][Runs] Created {created}/{total} run(s) in Qase"
            + (f" (delta: {skipped_runs} already existed)" if skipped_runs else "")
        )
        return self.mappings

    async def _import_one_cycle(
        self,
        code: str,
        zephyr_key: str,
        cycle: dict,
        exec_status_id_to_name: dict,
        jira_email: str = "",
        jira_api_token: str = "",
    ):
        cycle_key = cycle.get("key") or cycle.get("id") or ""
        name = (cycle.get("name") or cycle_key or "Unnamed Cycle").strip()

        # Delta mode: skip cycles whose final Qase run title already exists in
        # the target. Mirrors create_run's title ("[version] name" when the
        # cycle has a release). Limitation (documented): executions ADDED to an
        # already-migrated cycle are not re-synced — the run is add-only.
        existing_run_titles = (
            (self.mappings.existing_state.get(code) or {}).get("run_titles") or set()
        )
        if existing_run_titles:
            plan_name = (getattr(self, "_version_names", {}) or {}).get(
                str((cycle.get("jiraProjectVersion") or {}).get("id") or "")
            )
            expected_title = f"[{plan_name}] {name}" if plan_name else name
            if expected_title in existing_run_titles:
                self.logger.log(
                    f"[{code}][Runs] Delta: run {expected_title!r} already in target; skipping"
                )
                self._delta_skipped_runs = getattr(self, "_delta_skipped_runs", 0) + 1
                return None

        self.logger.log(f"[{code}][Runs] Importing cycle: {name} [{cycle_key}]")

        # Fetch all executions for this cycle
        all_executions = []
        for page in self.zephyr.get_test_executions(zephyr_key, cycle_key):
            all_executions.extend(page)

        if not all_executions:
            self.logger.log(f"[{code}][Runs] Cycle {name!r} has no executions; skipping")
            self.mappings.stats.add_issue(
                code, "run", f"cycle {name!r} [{cycle_key}] skipped: no executions in source"
            )
            return None

        # Fetch execution-level attachments in parallel, upload to Qase
        exec_attachments: dict = {}  # {ex_id: [qase_hash, ...]}
        exec_missed: dict = {}       # {ex_id: [filename, ...]} — download denied by API
        exec_raw_links: dict = {}    # {ex_id: [issue-link dict, ...]}
        # {ex_id: {src_url: {"name","url"}}} — inline images in execution comments
        exec_comment_images: dict = {}
        # {ex_id: [{"status_name", "actual"}, ...]} — step-level results
        exec_steps: dict = {}
        if all_executions:
            semaphore = asyncio.Semaphore(8)

            async def fetch_exec_steps(ex: dict):
                ex_id = ex.get("id")
                if not ex_id or not self._migrate_step_results:
                    return
                async with semaphore:
                    try:
                        raw = await self.pools.source(
                            self.zephyr.get_test_execution_steps, ex.get("key") or ex_id
                        )
                    except Exception:
                        # Script-type (BDD/plain) executions have no steps
                        return
                    parsed = []
                    for st in (raw or []):
                        inline = st.get("inline") or {}
                        sobj = inline.get("status") or {}
                        sname = ""
                        if isinstance(sobj, dict):
                            sname = (sobj.get("name") or "").lower().strip()
                            if not sname and sobj.get("id") is not None:
                                sname = exec_status_id_to_name.get(int(sobj["id"]), "")
                        else:
                            sname = str(sobj).lower().strip()
                        parsed.append({
                            "status_name": sname,
                            "actual": strip_html(inline.get("actualResult") or ""),
                        })
                    if parsed:
                        exec_steps[str(ex_id)] = parsed

            async def fetch_exec_comment_images(ex: dict):
                ex_id = ex.get("id")
                comment = ex.get("comment") or ""
                if not ex_id or "<img" not in comment.lower():
                    return
                async with semaphore:
                    jar = await self.pools.source(get_cdn_cookies, self.config, self.logger)
                    if not jar:
                        return
                    for img_url in extract_img_urls(comment):
                        raw_name = (img_url.rstrip("/").split("/")[-1]
                                    .split("?")[0].replace("+", " "))
                        m = re.match(r'^[a-f0-9-]+-\d+-(.+)$', raw_name)
                        img_name = m.group(1) if m else (raw_name or "inline_image")
                        raw = await self.pools.source(
                            self.zephyr.download_attachment_bytes,
                            img_url, jira_email, jira_api_token, jar,
                        )
                        if raw is None:
                            continue
                        res = await self.pools.qs(
                            self.qase.upload_attachment, code, (img_name, raw)
                        )
                        if res and res.get("url"):
                            exec_comment_images.setdefault(str(ex_id), {})[img_url] = {
                                "name": img_name, "url": res["url"],
                            }

            async def fetch_exec_links(ex: dict):
                ex_id = ex.get("id")
                if not ex_id:
                    return
                async with semaphore:
                    try:
                        links = await self.pools.source(
                            self.zephyr.get_test_execution_links, ex_id
                        )
                        if links and links.get("issues"):
                            exec_raw_links[str(ex_id)] = links["issues"]
                    except Exception as e:
                        self.logger.log(
                            f"[{code}][Runs] Link fetch failed for exec {ex_id}: {e}",
                            "warning",
                        )

            async def fetch_exec_attachments(ex: dict):
                ex_id = ex.get("id")
                if not ex_id:
                    return
                async with semaphore:
                    try:
                        # Fetch the session jar fresh (cached; re-mints only near
                        # token expiry) so long runs stay authenticated.
                        jar = await self.pools.source(
                            get_cdn_cookies, self.config, self.logger
                        )
                        app_jwt = jar.get("Bearer")
                        rest_base = jar.get("atm-rest-base")
                        # Execution-result attachment binaries are reachable only
                        # via the app backend's pre-signed S3 URLs (needs the
                        # browser-session app JWT). Without a session, fall back to
                        # the public v2 metadata so the filename is at least noted.
                        if app_jwt and rest_base:
                            records = await self.pools.source(
                                self.zephyr.get_result_attachment_records,
                                ex.get("key") or ex_id, rest_base, app_jwt,
                            )
                            atts = [{"url": r["url"], "filename": r["name"]} for r in (records or [])]
                        else:
                            atts = await self.pools.source(
                                self.zephyr.get_test_execution_attachments, ex_id
                            )
                        if not atts:
                            return
                        hashes = []
                        for att in atts:
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
                                    att_url, jira_email, jira_api_token, jar,
                                )
                            if raw is None:
                                exec_missed.setdefault(str(ex_id), []).append(att_name)
                                continue
                            result = await self.pools.qs(
                                self.qase.upload_attachment, code, (att_name, raw)
                            )
                            if result and result.get("hash"):
                                hashes.append(result["hash"])
                        if hashes:
                            exec_attachments[str(ex_id)] = hashes
                    except Exception as e:
                        self.logger.log(
                            f"[{code}][Runs] Attachment fetch failed for exec {ex_id}: {e}",
                            "warning",
                        )

            await asyncio.gather(*[fetch_exec_attachments(ex) for ex in all_executions])
            await asyncio.gather(*[fetch_exec_links(ex) for ex in all_executions])
            await asyncio.gather(*[fetch_exec_comment_images(ex) for ex in all_executions])
            await asyncio.gather(*[fetch_exec_steps(ex) for ex in all_executions])
            if exec_attachments:
                self.logger.log(
                    f"[{code}][Runs] Uploaded attachments for "
                    f"{len(exec_attachments)} execution(s) in cycle {name!r}"
                )

        # Resolve all execution-linked issue ids in one batch, then render
        # per-execution markdown notes for the result comments.
        exec_link_notes: dict = {}  # {ex_id: "🔗 Linked Jira issues: ..."}
        if exec_raw_links:
            all_ids = sorted({
                str(link.get("issueId"))
                for links in exec_raw_links.values()
                for link in links
                if link.get("issueId") is not None
            })
            key_map = await self.pools.source(self.zephyr.resolve_issue_keys, all_ids)
            for ex_id, links in exec_raw_links.items():
                parts = _format_issue_links(links, key_map)
                if parts:
                    exec_link_notes[ex_id] = "🔗 Linked Jira issues: " + " · ".join(parts)

        # Resolve case ids: Zephyr Essential testcase → Qase case id.
        # Execution objects only expose testCase.id (numeric), not testCase.key.
        tc_map = self.mappings.zephyr_tc_id_to_qase_case_id.get(code) or {}
        case_ids = []
        for ex in all_executions:
            tc = ex.get("testCase") or {}
            tc_lookup = tc.get("key") or str(tc.get("id") or "")
            qase_case_id = tc_map.get(tc_lookup)
            if qase_case_id is not None and qase_case_id not in case_ids:
                case_ids.append(qase_case_id)

        if not case_ids:
            self.logger.log(
                f"[{code}][Runs] Cycle {name!r}: no mapped cases found; skipping", "warning"
            )
            self.mappings.stats.add_issue(
                code, "run",
                f"cycle {name!r} [{cycle_key}] skipped: none of its executions "
                f"reference a migrated case",
            )
            return None

        # Environments: one shared environment across the cycle's executions →
        # set it on the Qase run; a mix → note each result's environment in its
        # comment (Qase results carry no environment field).
        run_environment_id = None
        exec_env_notes: dict = {}  # {ex_id: "🌐 Environment: X"}
        if self._migrate_environments and self._zephyr_env_names:
            env_ids = set()
            for ex in all_executions:
                eid = (ex.get("environment") or {}).get("id")
                if eid is not None:
                    env_ids.add(int(eid))
            if len(env_ids) == 1:
                env_name = self._zephyr_env_names.get(next(iter(env_ids)))
                if env_name:
                    run_environment_id = await self._ensure_qase_environment(code, env_name)
            elif len(env_ids) > 1:
                for eid in sorted(env_ids):
                    env_name = self._zephyr_env_names.get(eid)
                    if env_name:
                        await self._ensure_qase_environment(code, env_name)
                for ex in all_executions:
                    eid = (ex.get("environment") or {}).get("id")
                    env_name = self._zephyr_env_names.get(int(eid)) if eid is not None else None
                    if env_name:
                        exec_env_notes[str(ex.get("id"))] = f"🌐 Environment: {env_name}"
                self.logger.log(
                    f"[{code}][Runs] Cycle {name!r} spans {len(env_ids)} environments, "
                    f"noting each result's environment in its comment"
                )

        # Build the run dict for QaseService
        created_on = self._parse_ts(cycle.get("createdOn") or cycle.get("plannedStartDate"))
        completed_on = self._parse_ts(cycle.get("completedOn") or cycle.get("plannedEndDate"))
        # Cycle 'status' is an {id, self} object (not a plain string) — resolve
        # its name so a "Done"/"Completed" cycle marks its Qase run complete.
        status_obj = cycle.get("status") or {}
        if isinstance(status_obj, dict):
            cycle_status = (status_obj.get("name") or "").lower().strip()
            if not cycle_status:
                sid = status_obj.get("id")
                cycle_status = (self._cycle_status_id_to_name or {}).get(int(sid), "") if sid is not None else ""
        else:
            cycle_status = str(status_obj).lower().strip()
        is_done = cycle_status in ("done", "completed", "complete")

        # Cycle-level attachments: a Qase run has no attachments panel, but its
        # description is markdown — so download the binaries and embed them
        # inline (images as ![](url), other files as a [name](url) link). Needs
        # the browser-session app JWT; falls back to a filename note otherwise.
        run_description = (cycle.get("description") or "").strip()
        run_description = await self._append_cycle_attachments(
            code, name, cycle_key, all_executions, run_description
        )

        # Cycle links (Jira issues + web links) → markdown note on the run
        # description (Qase runs have no external-issue field).
        try:
            cycle_links = await self.pools.source(self.zephyr.get_test_cycle_links, cycle_key)
        except Exception:
            cycle_links = {}
        link_parts = []
        cycle_issue_links = cycle_links.get("issues") or []
        if cycle_issue_links:
            key_map = await self.pools.source(
                self.zephyr.resolve_issue_keys,
                [str(l.get("issueId")) for l in cycle_issue_links if l.get("issueId") is not None],
            )
            link_parts.extend(_format_issue_links(cycle_issue_links, key_map))
        for w in (cycle_links.get("webLinks") or []):
            if w.get("url"):
                label = (w.get("description") or w.get("url")).strip()
                link_parts.append(f"[{label}]({w['url']})")
        if link_parts:
            note = "🔗 Links: " + " · ".join(link_parts)
            run_description = f"{run_description}\n\n{note}" if run_description else note

        run_dict = {
            "name": name,
            "description": run_description or None,
            "created_on": created_on or _now_ts(),
            "completed_on": completed_on or (created_on or _now_ts()),
            "is_completed": is_done,
            "environment_id": run_environment_id,
            "author_id": self.mappings.default_user,
            # Version name via Jira enrichment → "[Release 1.2] Cycle name" run titles
            "plan_name": (
                getattr(self, "_version_names", {}) or {}
            ).get(str((cycle.get("jiraProjectVersion") or {}).get("id") or "")) or None,
        }

        try:
            qase_run_id = await self.pools.qs(
                self.qase.create_run, run_dict, code, case_ids, None
            )
        except Exception as e:
            self.logger.log(f"[{code}][Runs] Failed to create run {name!r}: {e}", "error")
            self.mappings.stats.add_issue(
                code, "run", f"cycle {name!r} [{cycle_key}] FAILED to create: {e}", "error"
            )
            return None

        if qase_run_id is None:
            self.logger.log(f"[{code}][Runs] create_run returned None for {name!r}", "error")
            self.mappings.stats.add_issue(
                code, "run", f"cycle {name!r} [{cycle_key}] FAILED to create (no id returned)", "error"
            )
            return None

        self.logger.log(f"[{code}][Runs] Created run id={qase_run_id} for cycle {name!r}")

        # Send bulk results
        results = self._build_results(
            all_executions, tc_map, exec_status_id_to_name, exec_attachments,
            exec_missed, exec_link_notes, exec_comment_images,
            exec_steps, exec_env_notes,
        )
        if results:
            try:
                await self.pools.qs(
                    self.qase.send_bulk_results,
                    run_dict,
                    results,
                    qase_run_id,
                    code,
                    self.mappings,
                    {r["test_id"]: r["_qase_case_id"] for r in results},
                    _RESULT_STATUSES,
                )
            except Exception as e:
                self.logger.log(
                    f"[{code}][Runs] Failed to send results for run {qase_run_id}: {e}", "error"
                )

        # Complete the run if it was done in Zephyr
        if is_done:
            try:
                await self.pools.qs(self.qase.complete_run, code, qase_run_id)
            except Exception as e:
                self.logger.log(
                    f"[{code}][Runs] Failed to complete run {qase_run_id}: {e}", "warning"
                )

        return qase_run_id

    async def _append_cycle_attachments(self, code, name, cycle_key, all_executions, run_description):
        """Embed cycle-level attachments inline in the run description (a Qase
        run has no attachments panel but its description is markdown). Images
        embed as ``![](url)``, other files as ``[name](url)`` links. Falls back
        to a filename note when no session is available or a download fails.
        Returns the (possibly extended) description."""
        jar = await self.pools.source(get_cdn_cookies, self.config, self.logger)
        app_jwt = jar.get("Bearer")
        rest_base = jar.get("atm-rest-base")
        embedded = []    # (name, qase_url, is_image)
        note_names = []
        if app_jwt and rest_base:
            # The testrun (cycle) attachment endpoint needs the numeric Jira
            # project id; resolve it once per project from any execution.
            if self._jira_project_id is None and all_executions:
                ex0 = all_executions[0]
                self._jira_project_id = await self.pools.source(
                    self.zephyr.get_project_id, "testresult",
                    ex0.get("key") or ex0.get("id"), rest_base, app_jwt,
                )
            records = []
            if self._jira_project_id:
                records = await self.pools.source(
                    self.zephyr.get_cycle_attachment_records,
                    cycle_key, self._jira_project_id, rest_base, app_jwt,
                ) or []
            for rec in records:
                raw = await self.pools.source(self.zephyr.download_presigned, rec["url"])
                if raw is None:
                    note_names.append(rec["name"])
                    continue
                res = await self.pools.qs(self.qase.upload_attachment, code, (rec["name"], raw))
                if res and res.get("url"):
                    is_img = str(res.get("mime") or "").startswith("image/")
                    embedded.append((rec["name"], res["url"], is_img))
                else:
                    note_names.append(rec["name"])
        else:
            try:
                meta = await self.pools.source(self.zephyr.get_test_cycle_attachments, cycle_key)
            except Exception:
                meta = []
            note_names = [a.get("filename") or "attachment" for a in (meta or [])]

        if embedded:
            parts = " ".join(
                (f"![{n}]({u})" if is_img else f"[{n}]({u})") for n, u, is_img in embedded
            )
            block = f"🖼 Cycle attachments: {parts}"
            run_description = f"{run_description}\n\n{block}" if run_description else block
            self.logger.log(
                f"[{code}][Runs] Embedded {len(embedded)} cycle attachment(s) in the "
                f"run description for {name!r}"
            )
        if note_names:
            note = ("📎 Attachments in source (not downloaded, enable browser-session "
                    "mode): " + ", ".join(note_names))
            run_description = f"{run_description}\n\n{note}" if run_description else note
            self.logger.log(
                f"[{code}][Runs] {len(note_names)} cycle attachment(s) noted (not "
                f"embedded) for {name!r}", "warning"
            )
        return run_description

    async def _ensure_qase_environment(self, code: str, title: str):
        """Create (or reuse) a Qase environment by title; returns its id or None.
        Registry lives in mappings.environments[code]; asyncio-locked so
        concurrent cycles of a project can't double-create."""
        key = title.strip().lower()
        async with self._env_lock:
            registry = self.mappings.environments.setdefault(code, {})
            if key in registry:
                return registry[key]
            env_id = await self.pools.qs(self.qase.create_environment, code, title)
            if env_id:
                registry[key] = env_id
                self.mappings.stats.add_entity_count(code, "environments", "qase")
                self.logger.log(f"[{code}][Runs] Created Qase environment {title!r} (id={env_id})")
            return registry.get(key)

    def _build_results(
        self,
        executions: list,
        tc_map: dict,
        exec_status_id_to_name: dict,
        exec_attachments: dict = None,
        exec_missed: dict = None,
        exec_link_notes: dict = None,
        exec_comment_images: dict = None,
        exec_steps: dict = None,
        exec_env_notes: dict = None,
    ) -> list:
        """Map Zephyr Essential executions to Qase result dicts."""
        exec_attachments = exec_attachments or {}
        exec_missed = exec_missed or {}
        exec_link_notes = exec_link_notes or {}
        exec_comment_images = exec_comment_images or {}
        exec_steps = exec_steps or {}
        exec_env_notes = exec_env_notes or {}
        # Config-provided mappings for CUSTOM execution statuses win over defaults:
        #   "runs": {"status_map": {"deferred": "skipped"}}
        status_map = dict(_EXEC_STATUS_MAP)
        for k, v in (self.config.get("runs.status_map") or {}).items():
            status_map[str(k).strip().lower()] = str(v).strip().lower()
        unmapped = set()
        results = []
        for ex in executions:
            tc = ex.get("testCase") or {}
            tc_lookup = tc.get("key") or str(tc.get("id") or "")
            qase_case_id = tc_map.get(tc_lookup)
            if qase_case_id is None:
                continue

            status_obj = ex.get("testExecutionStatus") or {}
            # Execution status objects may only carry an id (no name) — resolve via pre-fetched map.
            if isinstance(status_obj, dict):
                status_name = (status_obj.get("name") or "").lower().strip()
                if not status_name:
                    sid = status_obj.get("id")
                    status_name = exec_status_id_to_name.get(sid, "") if sid is not None else ""
            else:
                status_name = str(status_obj).lower().strip()
            qase_status = status_map.get(status_name)
            if qase_status is None:
                if status_name:
                    unmapped.add(status_name)
                qase_status = "untested"

            if qase_status in _SKIP_STATUSES:
                continue

            # Convert the comment HTML to markdown; embed inline images that
            # were uploaded to Qase (![name](url)), strip other tags.
            comment = strip_html(
                ex.get("comment") or "",
                image_map=exec_comment_images.get(str(ex.get("id") or ""), {}),
            )
            missed = exec_missed.get(str(ex.get("id") or ""))
            if missed:
                note = "📎 Attachments in source (not migrated, could not be downloaded): " + ", ".join(missed)
                comment = f"{comment}\n{note}" if comment else note
            link_note = exec_link_notes.get(str(ex.get("id") or ""))
            if link_note:
                comment = f"{comment}\n{link_note}" if comment else link_note
            env_note = exec_env_notes.get(str(ex.get("id") or ""))
            if env_note:
                comment = f"{comment}\n{env_note}" if comment else env_note
            executed_on = self._parse_ts(ex.get("executedOn") or ex.get("createdOn"))

            # Prefer actualStartDate/actualEndDate date math; fall back to executionTime/estimatedTime (ms)
            start_ts = self._parse_ts(ex.get("actualStartDate"))
            end_ts = self._parse_ts(ex.get("actualEndDate"))
            if start_ts and end_ts and end_ts > start_ts:
                elapsed = end_ts - start_ts
            else:
                raw_duration = ex.get("executionTime") or ex.get("estimatedTime")
                elapsed = max(0, int(raw_duration / 1000)) if raw_duration else 0

            result = {
                "test_id": tc_lookup,
                "_qase_case_id": qase_case_id,
                "status_id": qase_status,
                "comment": comment,
                "created_on": executed_on or _now_ts(),
                "elapsed": elapsed,
                # Atlassian account id — resolved via users.map (default user otherwise)
                "created_by": ex.get("executedById"),
            }

            # Step-level results → Qase result steps (positional). Statuses
            # outside passed/failed/blocked map to skipped (Qase's step enum).
            raw_steps = exec_steps.get(str(ex.get("id") or ""))
            if raw_steps:
                step_rows = []
                for st in raw_steps:
                    slug = status_map.get(st.get("status_name") or "") or "skipped"
                    if slug not in ("passed", "failed", "blocked"):
                        slug = "skipped"
                    step_rows.append({
                        "status_id": slug,
                        "actual": st.get("actual") or None,
                    })
                result["custom_step_results"] = step_rows

            ex_id = ex.get("id")
            hashes = exec_attachments.get(str(ex_id)) if ex_id else None
            if hashes:
                result["attachments"] = hashes

            results.append(result)

        if unmapped:
            self.logger.log(
                f"[Runs] Unmapped Zephyr execution status(es) {sorted(unmapped)} → left as "
                f"Untested in the Qase run. Map them via config: "
                f'"runs": {{"status_map": {{"<name>": "skipped|passed|failed|blocked"}}}}',
                "warning",
            )
            if not hasattr(self, "_reported_unmapped"):
                self._reported_unmapped = set()
            for status_name in sorted(unmapped - self._reported_unmapped):
                self._reported_unmapped.add(status_name)
                self.mappings.stats.add_issue(
                    self.project["code"], "result",
                    f"execution status {status_name!r} has no mapping, those results "
                    f"were left as Untested (add it to runs.status_map)",
                )

        return results

    @staticmethod
    def _parse_ts(value) -> int:
        """Parse a Zephyr Essential timestamp (epoch ms or ISO string) to epoch seconds."""
        if value is None:
            return 0
        if isinstance(value, (int, float)) and value > 0:
            # Zephyr Essential returns epoch milliseconds
            if value > 1e10:
                return int(value / 1000)
            return int(value)
        if isinstance(value, str):
            from datetime import datetime, timezone
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
                try:
                    dt = datetime.strptime(value[:26], fmt[:len(fmt)])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return int(dt.timestamp())
                except ValueError:
                    continue
        return 0
