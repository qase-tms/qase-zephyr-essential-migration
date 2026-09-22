from concurrent.futures import ThreadPoolExecutor

from .entities.zephyr_essential import (
    Projects,
    Fields,
    Attachments,
    Suites,
    Cases,
    Runs,
    Milestones,
)
from .service import QaseService, ZephyrEssentialService
from .service.qase_dry_run import DryRunQaseService
from .support import ConfigManager, Logger, Mappings, ThrottledThreadPoolExecutor, Pools

_SOURCE_POOL_WORKERS = 8


class Importer:
    def __init__(self, config: ConfigManager, logger: Logger, dry_run: bool = False) -> None:
        self.pools = Pools(
            qase_pool=ThrottledThreadPoolExecutor(max_workers=8, requests=250, interval=12),
            source_pool=ThreadPoolExecutor(max_workers=_SOURCE_POOL_WORKERS),
        )

        self.logger = logger
        self.config = config
        self.dry_run = dry_run

        if dry_run:
            print("\t\033[36m▷\033[0m DRY RUN, reading from Zephyr Essential, writing NOTHING to Qase")
            self.qase_service = DryRunQaseService(config, logger)
        else:
            self.qase_service = QaseService(config, logger)

        self.source_service = ZephyrEssentialService(config, logger)
        self.mappings = Mappings(
            "zephyr-essential",
            self.qase_service.resolve_user_id(self.config.get("users.default")),
            user_map=self.config.get("users.map") or {},
        )
        if self.mappings.user_map:
            self.logger.log(
                f"[Importer] users.map: {len(self.mappings.user_map)} Atlassian account id(s) "
                f"mapped to Qase user id(s)"
            )

    def start(self):
        self.logger.log("Starting Zephyr Essential → Qase migration")

        # Step 1. Import projects and build project map
        self.mappings = Projects(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_projects()

        if not self.mappings.projects:
            self.logger.log("[Importer] No projects to migrate. Exiting.")
            print("No projects to migrate, check projects.import in config.json")
            return

        # cases.preserve_ids defaults to true (missing key = on)
        _pid = self.config.get("cases.preserve_ids")
        preserve_ids = True if _pid is None else bool(_pid)

        if preserve_ids and bool(self.config.get("migration.allow_existing_target")):
            self.logger.log(
                "[Importer] cases.preserve_ids + migration.allow_existing_target are BOTH "
                "on, preserved ids can collide with cases already in the target project "
                "(Qase rejects duplicate ids). Preserved ids are only safe into an empty "
                "project.",
                "warning",
            )

        delta = bool(self.config.get("migration.delta"))
        if delta and not preserve_ids:
            print(
                "❌ migration.delta requires cases.preserve_ids: true, preserved ids are "
                "the only reliable way to match already-migrated cases. (The ORIGINAL run "
                "must have used preserve_ids too.)"
            )
            self.logger.log(
                "[Importer] migration.delta without cases.preserve_ids, aborting", "error"
            )
            return

        if delta:
            # Delta mode: read what each pre-existing target already contains;
            # entity steps then create only what is missing (add-only).
            for project in list(self.mappings.projects):
                state = self.qase_service.get_existing_project_state(project["code"])
                if state is None:
                    # Unknown target state → creating anything could duplicate.
                    self.logger.log(
                        f"[Importer] {project['code']}: could not read existing state, "
                        f"skipping this project (delta cannot proceed safely)",
                        "error",
                    )
                    self.mappings.stats.add_issue(
                        project["code"], "project",
                        "delta: skipped, existing target state unreadable", "error",
                    )
                    self.mappings.projects.remove(project)
                    continue
                if any((state["case_ids"], state["suites"], state["run_titles"], state["milestones"])):
                    self.mappings.existing_state[project["code"]] = state
                    self.logger.log(
                        f"[Importer] {project['code']}: delta baseline, "
                        f"{len(state['case_ids'])} case(s), {len(state['suites'])} suite(s), "
                        f"{len(state['run_titles'])} run(s), {len(state['milestones'])} milestone(s)"
                    )
                else:
                    self.logger.log(
                        f"[Importer] {project['code']}: empty/new target, full migration"
                    )

        # Target guard: everything below project level is create-only, so
        # importing into a Qase project that already holds ANY data (cases,
        # suites, runs, or milestones) would pollute/duplicate it. Skip such
        # projects unless explicitly allowed. (Delta mode replaces the guard
        # with check-before-create semantics above.)
        if not self.dry_run and not delta and not bool(self.config.get("migration.allow_existing_target")):
            kept = []
            for project in self.mappings.projects:
                counts = self.qase_service.count_project_entities(project["code"])
                nonzero = {k: v for k, v in counts.items() if v > 0}
                if nonzero:
                    existing = ", ".join(f"{v} {k}" for k, v in nonzero.items())
                    self.logger.log(
                        f"[Importer] Target Qase project {project['code']} already contains "
                        f"data ({existing}), importing would pollute/duplicate it. "
                        f"Skipping this project. To import anyway set "
                        f'"migration": {{"allow_existing_target": true}} in config.json.',
                        "error",
                    )
                    self.mappings.stats.add_issue(
                        project["code"], "project",
                        f"skipped: target already contains {existing} "
                        f"(target guard; see migration.allow_existing_target)",
                        "error",
                    )
                else:
                    kept.append(project)
            self.mappings.projects = kept
            if not self.mappings.projects:
                self.mappings.stats.print_issues()
                print("Nothing migrated, every target project already contains data.")
                return

        # Step 2. Attachments (no-op — Essential exposes no attachment endpoints)
        self.mappings = Attachments(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_all_attachments()

        # Step 3. Register Qase system fields (priority / status option ids)
        self.mappings = Fields(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_fields()

        # Step 4. Import per-project data (suites → milestones → cases → runs) in parallel
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(self._import_project_data, project)
                for project in self.mappings.projects
            ]
            for future in futures:
                future.result()

        self.mappings.stats.print()
        self.mappings.stats.print_issues()
        prefix = str(self.config.get("prefix") or "zephyr-essential")
        if self.dry_run:
            prefix = f"{prefix}_dry_run"
            print("\n\t\033[36m▷\033[0m DRY RUN complete, nothing was written to Qase.")
        self.mappings.stats.save(prefix)
        self.mappings.stats.save_xlsx(prefix)
        print(f"\nqase-zephyr-essential-migration v{self.logger.version}")
        if self.logger.log_file:
            print(f"Full log: {self.logger.log_file}")

    def _import_project_data(self, project: dict):
        self.logger.print_group(f'Importing project: {project["name"]} [{project["code"]}]')

        # Suites (folders)
        self.mappings = Suites(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_suites(project)

        # Milestones (test plans)
        self.mappings = Milestones(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_milestones(project)

        # Test cases
        Cases(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            self.pools,
        ).import_cases(project)

        # Test runs (cycles + executions)
        self.mappings = Runs(
            self.qase_service,
            self.source_service,
            self.logger,
            self.mappings,
            self.config,
            project,
            self.pools,
        ).import_runs()
