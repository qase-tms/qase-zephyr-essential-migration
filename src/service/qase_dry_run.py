"""Dry-run Qase service, reads hit the real API, writes are logged and faked.

Used by ``python start.py --dry-run``: the full extraction + mapping pipeline
runs (so unmapped statuses, missing fields, oversize values, and attachment
problems all surface), but nothing is created in Qase. Fake ids keep the
mappings consistent so downstream steps (cases → runs → results) still execute.
"""

import itertools
import mimetypes
import os

from .qase import QaseService


class DryRunQaseService(QaseService):
    _fake_ids = itertools.count(10_000_000)

    def _dry(self, message: str):
        self.logger.log(f"[DRY-RUN] {message}")

    # ---- writes: logged, never sent ----------------------------------

    def create_project(self, title, description, code, group_id=None):
        self._dry(f"would create project {title!r} [{code}]")
        return True

    def create_suite(self, code, title, description, parent_id=None):
        self._dry(f"[{code}] would create suite {title!r}")
        return next(self._fake_ids)

    def create_cases(self, code, cases):
        self._dry(f"[{code}] would bulk-create {len(cases)} case(s)")
        return True, [next(self._fake_ids) for _ in cases]

    def create_run(self, run, project_code, cases=[], milestone_id=None):
        env = f", environment_id={run.get('environment_id')}" if run.get("environment_id") else ""
        self._dry(
            f"[{project_code}] would create run {run.get('name')!r} with "
            f"{len(cases)} case(s){env}"
        )
        return next(self._fake_ids)

    def complete_run(self, project_code, run_id):
        self._dry(f"[{project_code}] would complete run {run_id}")
        return True

    def send_bulk_results(self, tr_run, results, qase_run_id, qase_code,
                          mappings, cases_map, result_statuses=None):
        n_steps = sum(1 for r in (results or []) if r.get("custom_step_results"))
        self._dry(
            f"[{qase_code}] would send {len(results or [])} result(s) to run "
            f"{qase_run_id} ({n_steps} with step-level results)"
        )

    def upload_attachment(self, code, attachment_data):
        name = "attachment"
        if isinstance(attachment_data, (tuple, list)) and len(attachment_data) == 2:
            name = os.path.basename(str(attachment_data[0])) or name
        elif isinstance(attachment_data, str):
            name = os.path.basename(attachment_data)
        self._dry(f"[{code}] would upload attachment {name!r}")
        return {
            "hash": f"dry-run-{next(self._fake_ids)}",
            "url": f"https://dry.run/{name}",
            "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
        }

    def create_custom_field(self, data):
        self._dry(f"would create custom field {data.get('title')!r}")
        return next(self._fake_ids)

    def update_custom_field(self, cf_id, data):
        self._dry(f"would update custom field {cf_id}: {data}")
        return True

    def create_environment(self, project_code, title, description=None):
        self._dry(f"[{project_code}] would create environment {title!r}")
        return next(self._fake_ids)

    def create_milestone(self, project_code, title, description, status, due_date):
        self._dry(f"[{project_code}] would create milestone {title!r}")
        return next(self._fake_ids)

    def create_configuration_group(self, project_code, title):
        self._dry(f"[{project_code}] would create configuration group {title!r}")
        return next(self._fake_ids)

    def create_configuration(self, project_code, title, group_id):
        self._dry(f"[{project_code}] would create configuration {title!r}")
        return next(self._fake_ids)

    def create_shared_step(self, project_code, title, steps):
        self._dry(f"[{project_code}] would create shared step {title!r}")
        return f"dry-run-{next(self._fake_ids)}"

    def attach_external_issues(self, code, links, issue_type="jira-cloud"):
        n = sum(len(l.get("external_issues") or []) for l in (links or []))
        self._dry(f"[{code}] would attach {n} external issue link(s) to {len(links or [])} case(s)")
        return True

    def update_case_fields(self, code, case_id, fields):
        self._dry(f"[{code}] would update case {case_id} fields: {list(fields)}")
        return True
