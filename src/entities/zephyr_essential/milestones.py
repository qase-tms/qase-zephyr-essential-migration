from ...service.qase import QaseService
from ...service.zephyr_essential import ZephyrEssentialService
from ...support.config_manager import ConfigManager
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools


class Milestones:
    """Import Zephyr Essential test plans → Qase milestones."""

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
        self.logger = logger
        self.mappings = mappings
        self.config = config
        self.pools = pools

    def import_milestones(self, project: dict) -> Mappings:
        code = project["code"]
        zephyr_key = project["zephyr_key"]

        self.logger.log(f"[{code}][Milestones] Fetching test plans from Zephyr Essential")

        all_plans = []
        for page in self.zephyr.get_test_plans(zephyr_key):
            all_plans.extend(page)

        if not all_plans:
            self.logger.log(f"[{code}][Milestones] No test plans found; skipping")
            return self.mappings

        total = len(all_plans)
        self.logger.log(f"[{code}][Milestones] Found {total} test plan(s)")
        self.mappings.stats.add_entity_count(code, "milestones", "zephyr-essential", total)

        # Delta mode: milestones already in the target (matched by title) are
        # reused, not re-created.
        existing_milestones = (
            (self.mappings.existing_state.get(code) or {}).get("milestones") or {}
        )

        milestone_map = {}
        created = 0
        skipped = 0

        for plan in all_plans:
            plan_key = plan.get("key") or plan.get("id") or ""
            name = (plan.get("name") or plan_key or "Unnamed Plan").strip()

            existing_id = existing_milestones.get(name.lower())
            if existing_id is not None:
                milestone_map[str(plan_key)] = existing_id
                skipped += 1
                self.logger.log(
                    f"[{code}][Milestones] Reusing existing milestone id={existing_id} "
                    f"for plan {name!r}"
                )
                continue

            description = (plan.get("description") or "").strip() or None

            status_obj = plan.get("status") or {}
            status_name = (
                (status_obj.get("name") or "") if isinstance(status_obj, dict) else str(status_obj)
            ).lower().strip()
            qase_status = "completed" if status_name in ("done", "completed") else "active"

            due_date = self._parse_date(plan.get("plannedEndDate"))

            try:
                milestone_id = self.qase.create_milestone(
                    code,
                    title=name,
                    description=description,
                    status=qase_status,
                    due_date=due_date,
                )
                if milestone_id:
                    milestone_map[str(plan_key)] = milestone_id
                    created += 1
                    self.logger.log(
                        f"[{code}][Milestones] Created milestone id={milestone_id} for plan {name!r}"
                    )
            except Exception as e:
                self.logger.log(
                    f"[{code}][Milestones] Failed to create milestone for {name!r}: {e}", "error"
                )

        self.mappings.milestones[code] = milestone_map
        self.mappings.stats.add_entity_count(code, "milestones", "qase", created)
        if skipped:
            self.mappings.stats.add_issue(
                code, "milestone", f"delta: reused {skipped} existing milestone(s)", "info"
            )
        self.logger.log(
            f"[{code}][Milestones] Created {created}/{total} milestone(s)"
            + (f" (reused {skipped} existing)" if skipped else "")
        )
        return self.mappings

    @staticmethod
    def _parse_date(value) -> int:
        """Parse a ZS date/timestamp to epoch seconds, or return None."""
        if value is None:
            return None
        if isinstance(value, (int, float)) and value > 0:
            return int(value / 1000) if value > 1e10 else int(value)
        if isinstance(value, str):
            from datetime import datetime, timezone
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(value[:26], fmt[:len(fmt)])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return int(dt.timestamp())
                except ValueError:
                    continue
        return None
