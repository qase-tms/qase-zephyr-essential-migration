import asyncio
import re
from typing import Optional

from ...service.qase import QaseService
from ...service.zephyr_essential import ZephyrEssentialService
from ...support.config_manager import ConfigManager
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools


class Projects:
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
        self.existing_codes = set()
        self.existing_projects_by_title = {}
        self.logger.divider()

    def import_projects(self) -> Mappings:
        return asyncio.run(self._import_projects_async())

    async def _import_projects_async(self) -> Mappings:
        self.logger.log("[Projects] Importing projects from Zephyr Essential")

        await self._load_existing_qase_projects()

        zephyr_projects = await self.pools.source(self.zephyr.get_projects)
        if not zephyr_projects:
            self.logger.log("[Projects] No projects found in Zephyr Essential")
            return self.mappings

        # projects.import_all: true → migrate every Zephyr-enabled project on
        # the tenant (projects.import is ignored). projects.exclude subtracts
        # keys/names from either form.
        import_all = bool(self.config.get("projects.import_all"))
        projects_filter = [] if import_all else (self.config.get("projects.import") or [])
        exclude = {
            str(p).strip() for p in (self.config.get("projects.exclude") or []) if str(p).strip()
        }

        total = len(zephyr_projects)
        self.logger.log(f"[Projects] Found {total} project(s) in Zephyr Essential")
        self.logger.print_status("Importing projects", total=total)

        await asyncio.gather(*[
            self._import_project(i, project, total, projects_filter, exclude)
            for i, project in enumerate(zephyr_projects)
        ])

        return self.mappings

    async def _import_project(
        self, i: int, project: dict, total: int, projects_filter: list, exclude: set
    ):
        key = project.get("key") or ""
        # Essential /projects returns no name — resolve via optional Jira enrichment
        name = project.get("name") or ""
        if not name:
            name = await self.pools.source(self.zephyr.get_jira_project_name, key) or key

        if projects_filter and key not in projects_filter and name not in projects_filter:
            self.logger.log(f"[Projects] Skipping project: {name} [{key}]")
            self.logger.print_status("Importing projects", i + 1, total)
            return

        if key in exclude or name in exclude:
            self.logger.log(f"[Projects] Excluded by projects.exclude: {name} [{key}]")
            self.logger.print_status("Importing projects", i + 1, total)
            return

        # Check if config provides an explicit target Qase project code
        projects_mapping = self.config.get("projects.mapping") or {}
        forced_code = projects_mapping.get(key) or projects_mapping.get(name)
        if forced_code:
            self.logger.log(
                f"[Projects] Using mapped Qase project [{forced_code}] for Zephyr project {name} [{key}]"
            )
            self.mappings.projects.append({"zephyr_key": key, "name": name, "code": forced_code})
            self.mappings.project_map[key] = forced_code
            self.mappings.stats.add_project(forced_code, name)
            self.logger.print_status("Importing projects", i + 1, total)
            return

        self.logger.log(f"[Projects] Importing project: {name} [{key}]")
        code = await self._create_project(name, "")
        if code:
            self.mappings.projects.append({"zephyr_key": key, "name": name, "code": code})
            self.mappings.project_map[key] = code
            self.mappings.stats.add_project(code, name)
        else:
            self.logger.log(f"[Projects] Failed to create/find project: {name}", "error")

        self.logger.print_status("Importing projects", i + 1, total)

    async def _load_existing_qase_projects(self):
        limit, offset = 100, 0
        while True:
            result = await self.pools.qs(self.qase.get_projects, limit, offset)
            entities = getattr(result, "entities", None) if result else None
            if not entities:
                break
            for p in entities:
                code = getattr(p, "code", None)
                title = getattr(p, "title", None)
                if code:
                    self.existing_codes.add(code.upper())
                    if title:
                        self.existing_projects_by_title[title.strip().lower()] = code
            if len(entities) < limit:
                break
            offset += limit
        self.logger.log(
            f"[Projects] Found {len(self.existing_projects_by_title)} existing Qase project(s)"
        )

    def _match_existing_project(self, title: str) -> Optional[str]:
        return self.existing_projects_by_title.get((title or "").strip().lower())

    def _short_code(self, s: str) -> str:
        s = s.replace("-", " ")
        s = re.sub("[^a-zA-Z ]", "", s)
        words = s.split()
        if len(words) > 1:
            code = "".join(word[0] for word in words).upper()
        else:
            code = s.upper()
        code = code.replace(" ", "")[:10]
        original_code = code
        postfix = ""
        while code in self.existing_codes or len(code) < 2:
            postfix = self._next_postfix(postfix)
            code = (original_code[: 10 - len(postfix)] + postfix).upper()
        self.existing_codes.add(code)
        return code

    def _next_postfix(self, postfix: str) -> str:
        if not postfix:
            return "A"
        elif postfix[-1] == "Z":
            return self._next_postfix(postfix[:-1]) + "A"
        else:
            return postfix[:-1] + chr(ord(postfix[-1]) + 1)

    async def _create_project(self, title: str, description: str) -> Optional[str]:
        existing = self._match_existing_project(title)
        if existing:
            self.logger.log(
                f"[Projects] Reusing existing Qase project: {title} [{existing}]"
            )
            # Visible in the migration report: the data lands in a project
            # that existed before this run (title match). The target guard
            # still blocks it later if it already contains data.
            self.mappings.stats.add_issue(
                existing, "project",
                f"importing into pre-existing Qase project [{existing}] (title match "
                f"on {title!r})",
            )
            self.existing_codes.add(existing.upper())
            return existing
        code = self._short_code(title)
        if await self.pools.qs(
            self.qase.create_project, title, description, code, self.mappings.group_id
        ):
            return code
        return None
