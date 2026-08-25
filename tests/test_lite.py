import datetime
import io
import inspect
import json
import os
import random
import re
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_workboard.lite import (
    LiteError,
    acquire_claim,
    acquire_repository_lock,
    amend_plan_review,
    amend_management,
    backfill_management,
    create_work_item,
    get_work_item,
    initialize_database,
    list_work_items,
    main,
    make_server,
    open_database,
    record_agent_review,
    record_human_gate,
    recover_review_task,
    report_plan_deviation,
    release_claim,
    release_repository_lock,
    set_hold,
    set_task_status,
    timeline,
    transition,
    unblock_task,
    workflow_advance,
    workflow_check,
    workflow_repair,
    workflow_status,
)
from agent_workboard.orchestrator import reconcile_expired
from agent_workboard.project import transfer_export, transfer_import
from agent_workboard import workflow as workflow_kernel
from agent_workboard import candidate as candidate_module
from agent_workboard import orchestrator as orchestrator_module
from agent_workboard import verify as verify_module
import agent_workboard.lite as lite_module


class LiteWorkboardTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project_root = os.path.realpath(self.temporary.name)
        self.database = os.path.join(self.temporary.name, "workboard.db")
        os.makedirs(os.path.join(self.temporary.name, ".awb"))
        with open(os.path.join(self.temporary.name, ".awb", "config.json"),
                  "w", encoding="utf-8") as handle:
            json.dump({
                "configVersion": 1, "projectId": "lite-test",
                "repositoryKey": "test-repository", "database": "workboard.db",
                "runtimeMode": "development", "usagePolicy": "OFF",
                "requiredPackageVersion": "0.3.1b8",
                "requiredSourceCommit": "test", "requiredSourceTree": "test",
                "requiredSourceTag": "test",
            }, handle)
        initialize_database(self.database)
        self.receipts = {}
        self.receipt_sequence = 0

    def tearDown(self):
        self.temporary.cleanup()

    def expires(self, minutes=30):
        return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)).replace(microsecond=0).isoformat()

    def management(self, work_item_id="TI-001", mode="STANDARD", item_type=None):
        item_type = item_type or work_item_id.split("-")[0]
        tasks = [
            {"taskId": work_item_id + "-T01", "seq": 1, "title": "规划与诊断",
             "ownerRole": "PLANNER", "required": True, "acceptance": ["plan ready"],
             "closureEvidenceRequired": ["plan evidence"]},
        ]
        if mode == "STANDARD":
            tasks.extend([
                {"taskId": work_item_id + "-T02", "seq": 2, "title": "实施与本地测试",
                 "ownerRole": "IMPLEMENTER", "required": True, "acceptance": ["implementation ready"],
                 "closureEvidenceRequired": ["test evidence"]},
                {"taskId": work_item_id + "-T03", "seq": 3, "title": "批量最终复审",
                 "ownerRole": "REVIEWER", "required": True, "acceptance": ["review passed"],
                 "closureEvidenceRequired": ["review evidence"]},
            ])
        else:
            tasks.append(
                {"taskId": work_item_id + "-T02", "seq": 2, "title": "规划复审",
                 "ownerRole": "REVIEWER", "required": True, "acceptance": ["review passed"],
                 "closureEvidenceRequired": ["review evidence"]}
            )
        return {
            "contractVersion": "AWB-WORKITEM-MGMT-v1",
            "templateContractVersion": "AWB-MANAGEMENT-v1",
            "scope": ["test scope"], "outOfScope": ["remote writes"],
            "authorization": {"allowed": ["local tests"], "forbidden": ["remote writes"]},
            "safetyConstraints": ["do not weaken tests"], "tasks": tasks,
            "acceptance": [{"id": "AC-001", "criterion": "workflow passes"}],
            "closure": [{"id": "CL-001", "criterion": "evidence recorded"}],
        }

    def create(self, work_item_id="TI-001", mode="STANDARD", priority="P2"):
        return create_work_item(
            self.database, work_item_id, work_item_id.split("-")[0], "测试任务", mode,
            priority, management=self.management(work_item_id, mode), human_review="MANUAL"
        )

    def claim(self, work_item_id, seq, agent, role):
        return acquire_claim(
            self.database, work_item_id, "{0}-T{1:02d}".format(work_item_id, seq),
            agent, role, self.expires()
        )

    def complete(self, work_item_id, seq, agent):
        item = get_work_item(self.database, work_item_id)
        task = item["tasks"][seq - 1]
        # b7 claims atomically enter IN_PROGRESS and submit transitions atomically
        # complete the task.  There is no standalone COMPLETED window.
        self.assertEqual("IN_PROGRESS", task["status"])

    def quality(self, modified_scope=None, addressed=None):
        return {
            "passedAcceptance": [{"id": "AC-001", "evidence": "deterministic workflow test"}],
            "tests": [{"command": "python -m unittest", "result": "PASS"}],
            "modifiedScope": modified_scope or ["src/agent_workboard/lite.py"],
            "knownNonBlockingIssues": [], "addressedFindingIds": addressed or [],
            "complexityChanges": [], "regressions": [], "acceptanceRegressions": [],
            "testsWeakened": False, "planDeviation": False,
            "closureEvidence": [{"id": "CL-001", "evidence": "test evidence"}],
        }

    def verify_receipt(self, work_item_id, agent_id, addressed=None):
        self.receipt_sequence += 1
        relative = "verify-source-" + work_item_id.lower()
        source = os.path.join(self.temporary.name, relative)
        os.makedirs(source, exist_ok=True)
        path = os.path.join(source, "value.py")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("VALUE = 1\n")
        checks = [{
            "checkId": "registered-unittest-final", "phase": "FINAL",
            "result": "PASS", "resultDigest": "a" * 64,
            "covers": ["HC-1", "HC-5", "VP-1", "VP-2"],
        }]
        with mock.patch.object(verify_module, "_run_registered_checks",
                               return_value=checks), mock.patch(
                                   "agent_workboard.candidate._runtime_guard"):
            result = verify_module.run(
                self.database, self.temporary.name, "repo", work_item_id,
                relative, agent_id, "FINAL",
                "test-final-verify-{0}".format(self.receipt_sequence),
                addressed_finding_ids=addressed,
            )
        receipt = result["receipt"]
        binding = {
            "receiptId": receipt["receiptId"],
            "coreFingerprint": receipt["coreFingerprint"],
            "receiptFingerprint": receipt["projection"]["receiptFingerprint"],
            "candidate": receipt["candidate"],
        }
        self.receipts[work_item_id] = binding
        return receipt["receiptId"]

    def revise_review(self, stage, finding_id):
        review = {
            "result": "REVISE", "reviewerMode": "ORDINARY", "summary": "fix required",
            "findings": [{
                "id": finding_id, "stage": stage, "violatedContract": "AC-001",
                "evidence": {"test": "deterministic failure"}, "impact": "acceptance fails",
                "closeCondition": "test passes", "origin": "INITIAL",
                "priorUnavailableReason": "not applicable",
            }],
            "resolvedFindingIds": [], "nonBlockingSuggestions": [],
        }
        if stage == "IMPLEMENTATION" and self.receipts:
            review["reviewedReceipt"] = list(self.receipts.values())[-1]
        return review

    def structured_review(self, stage, result, mode="ORDINARY", findings=None,
                          resolved=None, suggestions=None):
        review = {
            "result": result, "reviewerMode": mode, "summary": result,
            "findings": findings or [], "resolvedFindingIds": resolved or [],
            "nonBlockingSuggestions": suggestions or [],
        }
        if stage == "IMPLEMENTATION" and self.receipts:
            review["reviewedReceipt"] = list(self.receipts.values())[-1]
        return review

    def finding(self, stage, finding_id="PLAN-F001", origin="INITIAL",
                prior="not applicable"):
        return {
            "id": finding_id, "stage": stage, "violatedContract": "AC-001",
            "evidence": {"test": "deterministic failure"}, "impact": "acceptance fails",
            "closeCondition": "test passes", "origin": origin,
            "priorUnavailableReason": prior,
        }

    def json_file(self, name, value):
        path = os.path.join(self.temporary.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return path

    def through_plan_approval(self, work_item_id="TI-001"):
        self.create(work_item_id)
        self.claim(work_item_id, 1, "planner-a", "PLANNER")
        self.complete(work_item_id, 1, "planner-a")
        transition(self.database, work_item_id, "submit_plan", "planner-a")
        self.claim(work_item_id, 3, "reviewer-a", "REVIEWER")
        record_agent_review(self.database, work_item_id, "PLAN", "reviewer-a", "APPROVED", "可实施")
        record_human_gate(self.database, work_item_id, "PLAN", "human-a", "APPROVED", "批准")

    def create_orphaned_review_task(self, work_item_id="AWB-REC", deviation=False,
                                    leave_claim=False):
        create_work_item(
            self.database, work_item_id, "AWB", "orphan recovery",
            management=self.management(work_item_id, item_type="AWB"),
            human_review="AUTO_ON_PASS",
        )
        planner = work_item_id + "-planner"
        reviewer = work_item_id + "-plan-reviewer"
        implementer = work_item_id + "-implementer"
        self.claim(work_item_id, 1, planner, "PLANNER")
        self.complete(work_item_id, 1, planner)
        transition(self.database, work_item_id, "submit_plan", planner)
        self.claim(work_item_id, 3, reviewer, "REVIEWER")
        record_agent_review(
            self.database, work_item_id, "PLAN", reviewer, "APPROVED",
            self.structured_review("PLAN", "PASS"),
        )
        self.claim(work_item_id, 2, implementer, "IMPLEMENTER")
        transition(self.database, work_item_id, "start_implementation", implementer)
        if deviation:
            report_plan_deviation(
                self.database, work_item_id, work_item_id + "-T02", implementer,
                {"reason": "approved plan cannot proceed", "impact": "planning must resume"},
            )
        elif not leave_claim:
            release_claim(self.database, work_item_id, implementer)
        return {"planner": planner, "reviewer": reviewer, "implementer": implementer}

    def create_awb024_b6_orphan(self):
        """Create the one frozen public-b6 projection split in a disposable DB."""
        work_item_id = "AWB-024"
        self.create(work_item_id)
        relative = "docs/work-items/AWB-024-v0.3.1b7-preview-release.md"
        absolute = os.path.join(self.temporary.name, relative)
        os.makedirs(os.path.dirname(absolute))
        with open(absolute, "w", encoding="utf-8") as handle:
            handle.write("disposable exact-path fixture\n")
        planner = work_item_id + "-planner"
        reviewer = work_item_id + "-reviewer"
        self.claim(work_item_id, 1, planner, "PLANNER")
        transition(
            self.database, work_item_id, "submit_plan", planner,
            plan_artifact={"projectRoot": self.temporary.name,
                           "path": relative},
        )
        self.claim(work_item_id, 3, reviewer, "REVIEWER")
        head = get_work_item(self.database, work_item_id)["planArtifact"]
        review = self.structured_review(
            "PLAN", "REVISE_TO_PLANNER", findings=[self.finding("PLAN")]
        )
        review.update({
            "protocolVersion": "AWB-REVIEW-v2",
            "reviewedArtifact": {key: head[key] for key in (
                "path", "revision", "sha256", "editorAgentId"
            )},
        })
        record_agent_review(
            self.database, work_item_id, "PLAN", reviewer, "REJECTED",
            review,
        )
        connection = open_database(self.database)
        connection.execute(
            "UPDATE tasks SET status='IN_PROGRESS' WHERE task_id='AWB-024-T03'"
        )
        connection.commit()
        connection.close()
        return absolute

    def database_snapshot(self, database=None):
        connection = open_database(database or self.database)
        try:
            tables = (
                "work_items", "tasks", "claims", "repository_locks", "reviews",
                "human_gates", "events", "orchestrator_instances",
                "orchestrator_leases", "orchestrator_events", "usage_events",
            )
            return {
                table: [dict(row) for row in connection.execute(
                    "SELECT * FROM {0} ORDER BY rowid".format(table)
                )]
                for table in tables
            }
        finally:
            connection.close()

    def to_plan_convergence_round(self, work_item_id):
        self.create(work_item_id)
        self.claim(work_item_id, 1, work_item_id + "-planner-0", "PLANNER")
        self.complete(work_item_id, 1, work_item_id + "-planner-0")
        transition(self.database, work_item_id, "submit_plan", work_item_id + "-planner-0")
        for round_number in (1, 2, 3):
            reviewer = work_item_id + "-reviewer-" + str(round_number)
            self.claim(work_item_id, 3, reviewer, "REVIEWER")
            record_agent_review(
                self.database, work_item_id, "PLAN", reviewer, "REJECTED",
                self.revise_review("PLAN", "PLAN-F001"),
            )
            if round_number < 3:
                planner = work_item_id + "-planner-" + str(round_number)
                self.claim(work_item_id, 1, planner, "PLANNER")
                self.complete(work_item_id, 1, planner)
                transition(
                    self.database, work_item_id, "submit_plan", planner,
                )

    def after_convergence_revision(self, work_item_id):
        self.to_plan_convergence_round(work_item_id)
        convergence = work_item_id + "-convergence"
        self.claim(work_item_id, 3, convergence, "REVIEWER")
        record_agent_review(
            self.database, work_item_id, "PLAN", convergence, "REJECTED",
            self.structured_review(
                "PLAN", "CONVERGENCE_REVISE", mode="CONVERGENCE",
                findings=[self.finding("PLAN")],
            ),
        )
        planner = work_item_id + "-planner-final"
        self.claim(work_item_id, 1, planner, "PLANNER")
        self.complete(work_item_id, 1, planner)
        transition(
            self.database, work_item_id, "submit_plan", planner,
        )

    def test_clean_init_and_repeat_init_rejected(self):
        connection = open_database(self.database)
        self.assertEqual("MVP-LITE-v1", connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0])
        connection.close()
        with self.assertRaises(LiteError):
            initialize_database(self.database)

    def _opt_in_plan(self, work_item_id):
        self.create(work_item_id)
        path = os.path.join(self.temporary.name, work_item_id + "-plan.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# revision 1\n")
        planner = work_item_id + "-planner"
        self.claim(work_item_id, 1, planner, "PLANNER")
        self.complete(work_item_id, 1, planner)
        transition(
            self.database, work_item_id, "submit_plan", planner,
            plan_artifact={"projectRoot": self.temporary.name,
                           "path": os.path.basename(path)},
        )
        return path

    def _amend_review(self, work_item_id, reviewer, path, revision, mode="ORDINARY"):
        head = get_work_item(self.database, work_item_id)["planArtifact"]
        replacement = os.path.join(
            self.temporary.name, "replacement-{0}-{1}.md".format(work_item_id, revision)
        )
        with open(replacement, "w", encoding="utf-8") as handle:
            handle.write("# revision {0}\n".format(revision))
        review = {
            "protocolVersion": "AWB-REVIEW-v2", "stage": "PLAN",
            "result": "AMENDED", "reviewerMode": mode,
            "reviewedArtifact": {key: head[key] for key in (
                "path", "revision", "sha256", "editorAgentId"
            )},
            "amendments": [{"category": "TEST_OMISSION", "summary": "add test",
                             "traceTo": ["AC-001"]}],
            "findings": [], "resolvedFindingIds": [],
            "nonBlockingSuggestions": [], "summary": "amended",
        }
        return amend_plan_review(
            self.database, work_item_id, reviewer, replacement,
            self.temporary.name, "test-repository", review,
            "amend-{0}-{1}".format(work_item_id, revision),
        )

    def test_opt_in_plan_amendment_is_atomic_and_requires_fresh_reviewer(self):
        work_item_id = "TI-AMEND"
        path = self._opt_in_plan(work_item_id)
        self.claim(work_item_id, 3, "reviewer-a", "REVIEWER")
        self._amend_review(work_item_id, "reviewer-a", path, 2)
        item = get_work_item(self.database, work_item_id)
        self.assertEqual(2, item["planArtifact"]["revision"])
        self.assertEqual("reviewer-a", item["planArtifact"]["editorAgentId"])
        self.assertEqual("CLAIMABLE", item["queue_state"])
        with self.assertRaises(LiteError):
            self.claim(work_item_id, 3, "reviewer-a", "REVIEWER")
        self.claim(work_item_id, 3, "reviewer-b", "REVIEWER")
        head = get_work_item(self.database, work_item_id)["planArtifact"]
        record_agent_review(
            self.database, work_item_id, "PLAN", "reviewer-b", "APPROVED", {
                "protocolVersion": "AWB-REVIEW-v2", "stage": "PLAN",
                "result": "PASS", "reviewerMode": "ORDINARY",
                "reviewedArtifact": {key: head[key] for key in (
                    "path", "revision", "sha256", "editorAgentId"
                )},
                "amendments": [], "findings": [], "resolvedFindingIds": [],
                "nonBlockingSuggestions": [], "summary": "pass",
            },
        )
        self.assertEqual("WAITING_HUMAN", get_work_item(
            self.database, work_item_id
        )["queue_state"])
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual("# revision 2\n", handle.read())

    def test_opt_in_plan_rounds_stop_at_five_and_round_five_cannot_amend(self):
        work_item_id = "TI-ROUNDS"
        path = self._opt_in_plan(work_item_id)
        for round_number in (1, 2, 3, 4):
            reviewer = "round-reviewer-" + str(round_number)
            self.claim(work_item_id, 3, reviewer, "REVIEWER")
            self._amend_review(
                work_item_id, reviewer, path, round_number + 1,
                mode="CONVERGENCE" if round_number == 4 else "ORDINARY",
            )
        self.claim(work_item_id, 3, "round-reviewer-5", "REVIEWER")
        with self.assertRaises(LiteError):
            self._amend_review(work_item_id, "round-reviewer-5", path, 6)
        release_claim(self.database, work_item_id, "round-reviewer-5")
        self.claim(work_item_id, 3, "round-reviewer-final", "REVIEWER")
        head = get_work_item(self.database, work_item_id)["planArtifact"]
        record_agent_review(
            self.database, work_item_id, "PLAN", "round-reviewer-final", "APPROVED", {
                "protocolVersion": "AWB-REVIEW-v2", "stage": "PLAN",
                "result": "PASS", "reviewerMode": "ORDINARY",
                "reviewedArtifact": {key: head[key] for key in (
                    "path", "revision", "sha256", "editorAgentId"
                )},
                "amendments": [], "findings": [], "resolvedFindingIds": [],
                "nonBlockingSuggestions": [], "summary": "round five pass",
            },
        )
        projection = get_work_item(self.database, work_item_id)["reviewConvergence"]["PLAN"]
        self.assertEqual(5, projection["totalRoundsUsed"])

    def test_plan_amend_replace_failure_restores_bytes_and_releases_exact_lock(self):
        work_item_id = "TI-AMEND-FAULT"
        path = self._opt_in_plan(work_item_id)
        self.claim(work_item_id, 3, "reviewer-fault", "REVIEWER")
        with open(path, "rb") as handle:
            original = handle.read()
        real_replace = __import__("agent_workboard.lite", fromlist=["_atomic_plan_bytes"])._atomic_plan_bytes
        calls = {"count": 0}

        def fail_once(target, raw):
            calls["count"] += 1
            if calls["count"] == 1:
                raise IOError("injected replace failure")
            return real_replace(target, raw)

        with mock.patch("agent_workboard.lite._atomic_plan_bytes", side_effect=fail_once):
            with self.assertRaises(IOError):
                self._amend_review(work_item_id, "reviewer-fault", path, 2)
        with open(path, "rb") as handle:
            self.assertEqual(original, handle.read())
        connection = open_database(self.database)
        try:
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM repository_locks WHERE status='ACTIVE'"
            ).fetchone()[0])
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM reviews WHERE work_item_id=?", (work_item_id,)
            ).fetchone()[0])
        finally:
            connection.close()

    def test_usage_off_skips_binding_and_mutation_boundaries(self):
        work_item_id = "TI-USAGE-OFF"
        self.create(work_item_id)
        with mock.patch("agent_workboard.usage.prepare_interval_boundary",
                        side_effect=AssertionError("must not parse")), mock.patch(
                            "agent_workboard.usage.best_effort_sync",
                            side_effect=AssertionError("must not sync")):
            result = acquire_claim(
                self.database, work_item_id, work_item_id + "-T01", "planner-off",
                "PLANNER", self.expires(), session_id="missing-session",
                usage_provider="codex-local", model="test", usage_policy="OFF",
            )
            self.assertEqual("DISABLED", result["usageStatus"])
            set_task_status(
                self.database, work_item_id, work_item_id + "-T01", "planner-off",
                "IN_PROGRESS", usage_policy="OFF",
            )
            transition(
                self.database, work_item_id, "submit_plan", "planner-off",
                usage_policy="OFF",
            )
        connection = open_database(self.database)
        try:
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM usage_events WHERE event_type='USAGE_BINDING_CREATED'"
            ).fetchone()[0])
        finally:
            connection.close()

    def test_plan_amend_exact_replay_is_noop_and_conflicting_replay_is_zero_write(self):
        work_item_id = "TI-AMEND-REPLAY"
        path = self._opt_in_plan(work_item_id)
        reviewer = "reviewer-replay"
        self.claim(work_item_id, 3, reviewer, "REVIEWER")
        head = get_work_item(self.database, work_item_id)["planArtifact"]
        replacement = os.path.join(self.temporary.name, "replay-replacement.md")
        with open(replacement, "w", encoding="utf-8") as handle:
            handle.write("# replacement\n")
        review = {
            "protocolVersion": "AWB-REVIEW-v2", "stage": "PLAN",
            "result": "AMENDED", "reviewerMode": "ORDINARY",
            "reviewedArtifact": {key: head[key] for key in (
                "path", "revision", "sha256", "editorAgentId"
            )},
            "amendments": [{"category": "COMMAND_OR_PATH", "summary": "fix command",
                             "traceTo": ["AC-001"]}],
            "findings": [], "resolvedFindingIds": [],
            "nonBlockingSuggestions": [], "summary": "amended",
        }
        arguments = (self.database, work_item_id, reviewer, replacement,
                     self.temporary.name, "test-repository", review, "exact-replay")
        amend_plan_review(*arguments)
        snapshot = self.database_snapshot()
        amend_plan_review(*arguments)
        self.assertEqual(snapshot, self.database_snapshot())
        changed = dict(review)
        changed["summary"] = "conflict"
        with self.assertRaises(LiteError):
            amend_plan_review(
                self.database, work_item_id, reviewer, replacement,
                self.temporary.name, "test-repository", changed, "exact-replay",
            )
        self.assertEqual(snapshot, self.database_snapshot())
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual("# replacement\n", handle.read())

    def test_plan_amend_refuses_competing_writer_without_file_or_review_change(self):
        path = self._opt_in_plan("TI-AMEND-BUSY")
        self.create("TI-OTHER")
        self.claim("TI-OTHER", 1, "other-planner", "PLANNER")
        acquire_repository_lock(
            self.database, "TI-OTHER", "test-repository", "other-planner", self.expires()
        )
        self.claim("TI-AMEND-BUSY", 3, "reviewer-busy", "REVIEWER")
        before = self.database_snapshot()
        with self.assertRaises(LiteError):
            self._amend_review("TI-AMEND-BUSY", "reviewer-busy", path, 2)
        after = self.database_snapshot()
        self.assertEqual(before["reviews"], after["reviews"])
        self.assertEqual(before["events"], after["events"])
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual("# revision 1\n", handle.read())

    def test_opt_in_material_review_returns_to_planner_and_rejects_symlink_artifact(self):
        work_item_id = "TI-MATERIAL"
        self._opt_in_plan(work_item_id)
        self.claim(work_item_id, 3, "material-reviewer", "REVIEWER")
        head = get_work_item(self.database, work_item_id)["planArtifact"]
        record_agent_review(
            self.database, work_item_id, "PLAN", "material-reviewer", "REJECTED", {
                "protocolVersion": "AWB-REVIEW-v2", "stage": "PLAN",
                "result": "REVISE_TO_PLANNER", "reviewerMode": "ORDINARY",
                "reviewedArtifact": {key: head[key] for key in (
                    "path", "revision", "sha256", "editorAgentId"
                )},
                "amendments": [], "findings": [self.finding("PLAN", "MATERIAL-F001")],
                "resolvedFindingIds": [], "nonBlockingSuggestions": [],
                "summary": "material scope decision required",
            },
        )
        item = get_work_item(self.database, work_item_id)
        self.assertEqual("DRAFT", item["state"])
        self.assertEqual("PLANNER", item["current_role"])

        self.create("TI-SYMLINK")
        target = os.path.join(self.temporary.name, "target-plan.md")
        link = os.path.join(self.temporary.name, "linked-plan.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("plan\n")
        os.symlink(target, link)
        self.claim("TI-SYMLINK", 1, "symlink-planner", "PLANNER")
        self.complete("TI-SYMLINK", 1, "symlink-planner")
        with self.assertRaises(LiteError):
            transition(
                self.database, "TI-SYMLINK", "submit_plan", "symlink-planner",
                plan_artifact={"projectRoot": self.temporary.name,
                               "path": os.path.basename(link)},
            )

    def test_all_work_item_types_create_same_id_top_level(self):
        for index, item_type in enumerate(("TI", "FE", "R", "WA", "AWB")):
            work_item_id = "{0}-{1:03d}".format(item_type, index + 1)
            item = create_work_item(
                self.database, work_item_id, item_type, "item",
                management=self.management(work_item_id, item_type=item_type),
            )
            self.assertEqual(work_item_id, item["work_item_id"])
        self.assertEqual(5, len(list_work_items(self.database)))

    def test_internal_tasks_are_not_top_level_cards(self):
        item = self.create()
        self.assertEqual(3, len(item["tasks"]))
        self.assertEqual(1, len(list_work_items(self.database)))

    def test_readonly_has_no_implementation_task(self):
        item = self.create(mode="READ_ONLY_DIAGNOSIS")
        self.assertEqual(["PLANNER", "REVIEWER"], [task["owner_role"] for task in item["tasks"]])
        self.assertNotIn("IMPLEMENTER", [task["owner_role"] for task in item["tasks"]])

    def test_create_requires_management_and_is_atomic(self):
        with self.assertRaises(LiteError):
            create_work_item(self.database, "TI-900", "TI", "missing envelope")
        connection = open_database(self.database)
        try:
            self.assertEqual(0, connection.execute(
                "SELECT count(*) FROM work_items WHERE work_item_id='TI-900'"
            ).fetchone()[0])
        finally:
            connection.close()

    def test_management_projection_progress_and_single_in_progress(self):
        management = self.management("TI-901")
        management["tasks"].insert(1, {
            "taskId": "TI-901-T02", "seq": 2, "title": "第二规划任务",
            "ownerRole": "PLANNER", "required": True, "acceptance": ["second plan"],
            "closureEvidenceRequired": ["second evidence"],
        })
        for index, task in enumerate(management["tasks"], 1):
            task["taskId"], task["seq"] = "TI-901-T{0:02d}".format(index), index
        create_work_item(self.database, "TI-901", "TI", "projection", management=management)
        self.claim("TI-901", 1, "planner-1", "PLANNER")
        set_task_status(self.database, "TI-901", "TI-901-T01", "planner-1", "IN_PROGRESS")
        release_claim(self.database, "TI-901", "planner-1")
        self.claim("TI-901", 2, "planner-2", "PLANNER")
        duplicate = set_task_status(
            self.database, "TI-901", "TI-901-T02", "planner-2", "IN_PROGRESS"
        )
        self.assertIsNone(duplicate)
        self.assertEqual("IN_PROGRESS", get_work_item(
            self.database, "TI-901"
        )["tasks"][1]["status"])
        item = get_work_item(self.database, "TI-901")
        self.assertEqual("AWB-WORKITEM-MGMT-v1", item["management"]["contractVersion"])
        self.assertEqual((0, 4, 0), (
            item["progress"]["completed"], item["progress"]["total"], item["progress"]["percent"]
        ))

    def test_legacy_backfill_and_human_amendment_are_append_only(self):
        self.create("AWB-901")
        connection = open_database(self.database)
        connection.execute(
            "UPDATE events SET payload_json=? WHERE work_item_id='AWB-901' AND event_type='WORK_ITEM_CREATED'",
            (json.dumps({"type": "AWB", "title": "测试任务", "mode": "STANDARD", "priority": "P2"}),),
        )
        connection.commit()
        connection.close()
        self.assertTrue(get_work_item(self.database, "AWB-901")["managementBackfillRequired"])
        self.claim("AWB-901", 1, "planner", "PLANNER")
        backfilled = backfill_management(
            self.database, "AWB-901", "planner", self.management("AWB-901"), "approved plan"
        )
        self.assertFalse(backfilled["managementBackfillRequired"])
        amended_input = self.management("AWB-901")
        amended_input["scope"].append("authorized addition")
        amended_input["tasks"].append({
            "taskId": "AWB-901-T04", "seq": 4, "title": "授权附加验证",
            "ownerRole": "REVIEWER", "required": False, "acceptance": ["optional check"],
            "closureEvidenceRequired": ["optional evidence"],
        })
        amended = amend_management(
            self.database, "AWB-901", "human", amended_input, "user expanded scope"
        )
        self.assertIn("authorized addition", amended["management"]["scope"])
        self.assertEqual(4, len(amended["tasks"]))
        events = [event["event_type"] for event in timeline(self.database, "AWB-901")]
        self.assertIn("WORK_ITEM_MANAGEMENT_BACKFILLED", events)
        self.assertIn("WORK_ITEM_MANAGEMENT_AMENDED", events)

    def test_plan_review_with_only_suggestions_is_forced_to_pass(self):
        self.create()
        self.claim("TI-001", 1, "planner", "PLANNER")
        self.complete("TI-001", 1, "planner")
        transition(self.database, "TI-001", "submit_plan", "planner")
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        item = record_agent_review(
            self.database, "TI-001", "PLAN", "reviewer", "REJECTED",
            self.structured_review("PLAN", "REVISE", suggestions=["prefer another architecture"]),
        )
        self.assertEqual("WAITING_HUMAN", item["queue_state"])
        self.assertEqual("PASS", item["reviewConvergence"]["PLAN"]["latestResult"])

    def test_incomplete_finding_is_nonblocking_and_does_not_return_author(self):
        self.create()
        self.claim("TI-001", 1, "planner", "PLANNER")
        self.complete("TI-001", 1, "planner")
        transition(self.database, "TI-001", "submit_plan", "planner")
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        incomplete = {"id": "PLAN-F001", "stage": "PLAN", "impact": "vague"}
        item = record_agent_review(
            self.database, "TI-001", "PLAN", "reviewer", "REJECTED",
            self.structured_review("PLAN", "REVISE", findings=[incomplete]),
        )
        self.assertEqual(("WAITING_HUMAN", None), (item["queue_state"], item["current_role"]))

    def test_substantive_but_unstructured_review_counts_without_driving_revision(self):
        self.create()
        self.claim("TI-001", 1, "planner", "PLANNER")
        self.complete("TI-001", 1, "planner")
        transition(self.database, "TI-001", "submit_plan", "planner")
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        item = record_agent_review(
            self.database, "TI-001", "PLAN", "reviewer", "REJECTED",
            "Please use my preferred framework",
        )
        counter = item["reviewConvergence"]["PLAN"]
        self.assertEqual((1, "PASS", "WAITING_HUMAN"), (
            counter["totalRoundsUsed"], counter["latestResult"], item["queue_state"]
        ))

    def test_later_unrelated_finding_is_demoted_without_new_evidence(self):
        self.create()
        self.claim("TI-001", 1, "planner", "PLANNER")
        self.complete("TI-001", 1, "planner")
        transition(self.database, "TI-001", "submit_plan", "planner")
        self.claim("TI-001", 3, "reviewer-1", "REVIEWER")
        record_agent_review(
            self.database, "TI-001", "PLAN", "reviewer-1", "REJECTED",
            self.revise_review("PLAN", "PLAN-F001"),
        )
        self.claim("TI-001", 1, "planner-2", "PLANNER")
        self.complete("TI-001", 1, "planner-2")
        transition(
            self.database, "TI-001", "submit_plan", "planner-2",
        )
        self.claim("TI-001", 3, "reviewer-2", "REVIEWER")
        unrelated = self.finding("PLAN", "PLAN-F002")
        item = record_agent_review(
            self.database, "TI-001", "PLAN", "reviewer-2", "REJECTED",
            self.structured_review("PLAN", "REVISE", findings=[unrelated],
                                   resolved=["PLAN-F001"]),
        )
        self.assertEqual("PASS", item["reviewConvergence"]["PLAN"]["latestResult"])
        self.assertFalse(item["reviewConvergence"]["PLAN"]["openFindings"])

    def test_plan_pass_requires_explicit_closure_of_prior_open_finding(self):
        self.create()
        self.claim("TI-001", 1, "planner-1", "PLANNER")
        self.complete("TI-001", 1, "planner-1")
        transition(self.database, "TI-001", "submit_plan", "planner-1")
        self.claim("TI-001", 3, "reviewer-1", "REVIEWER")
        record_agent_review(
            self.database, "TI-001", "PLAN", "reviewer-1", "REJECTED",
            self.revise_review("PLAN", "PLAN-F001"),
        )
        self.claim("TI-001", 1, "planner-2", "PLANNER")
        self.complete("TI-001", 1, "planner-2")
        transition(
            self.database, "TI-001", "submit_plan", "planner-2",
        )
        self.claim("TI-001", 3, "reviewer-2", "REVIEWER")
        with self.assertRaisesRegex(LiteError, "open blocking Finding"):
            record_agent_review(
                self.database, "TI-001", "PLAN", "reviewer-2", "APPROVED",
                self.structured_review("PLAN", "PASS"),
            )
        rejected = get_work_item(self.database, "TI-001")
        self.assertEqual(("PLAN_REVIEW_PENDING", "CLAIMED"), (
            rejected["state"], rejected["queue_state"]
        ))
        passed = record_agent_review(
            self.database, "TI-001", "PLAN", "reviewer-2", "APPROVED",
            self.structured_review("PLAN", "PASS", resolved=["PLAN-F001"]),
        )
        self.assertEqual("WAITING_HUMAN", passed["queue_state"])
        self.assertFalse(passed["reviewConvergence"]["PLAN"]["openFindings"])

    def test_implementation_pass_requires_explicit_closure_of_prior_open_finding(self):
        self._through_implementation_submission()
        self.claim("TI-001", 3, "reviewer-1", "REVIEWER")
        record_agent_review(
            self.database, "TI-001", "FINAL", "reviewer-1", "REJECTED",
            self.revise_review("IMPLEMENTATION", "IMPLEMENTATION-F001"),
        )
        self.claim("TI-001", 2, "implementer-2", "IMPLEMENTER")
        self.complete("TI-001", 2, "implementer-2")
        transition(
            self.database, "TI-001", "submit_implementation", "implementer-2",
            verify_receipt=self.verify_receipt(
                "TI-001", "implementer-2", ["IMPLEMENTATION-F001"]),
            project_root=self.temporary.name,
        )
        self.claim("TI-001", 3, "reviewer-2", "REVIEWER")
        with self.assertRaisesRegex(LiteError, "open blocking Finding"):
            record_agent_review(
                self.database, "TI-001", "FINAL", "reviewer-2", "APPROVED",
                self.structured_review("IMPLEMENTATION", "PASS"),
            )
        rejected = get_work_item(self.database, "TI-001")
        self.assertEqual(("IMPLEMENTATION_COMPLETED", "CLAIMED"), (
            rejected["state"], rejected["queue_state"]
        ))
        passed = record_agent_review(
            self.database, "TI-001", "FINAL", "reviewer-2", "APPROVED",
            self.structured_review(
                "IMPLEMENTATION", "PASS", resolved=["IMPLEMENTATION-F001"]
            ),
        )
        self.assertEqual("WAITING_HUMAN", passed["queue_state"])
        self.assertFalse(
            passed["reviewConvergence"]["IMPLEMENTATION"]["openFindings"]
        )

    def test_readonly_public_api_stops_after_plan_review(self):
        self.create(mode="READ_ONLY_DIAGNOSIS")
        self.claim("TI-001", 1, "planner", "PLANNER")
        self.complete("TI-001", 1, "planner")
        transition(self.database, "TI-001", "submit_plan", "planner")
        self.claim("TI-001", 2, "reviewer", "REVIEWER")
        item = record_agent_review(
            self.database, "TI-001", "PLAN", "reviewer", "APPROVED", "diagnosis accepted"
        )
        self.assertEqual(("PLAN_REVIEW_APPROVED", "HELD", "TARGET_REACHED"),
                         (item["state"], item["queue_state"], item["held_reason"]))

    def test_create_request_id_rejects_changed_payload(self):
        create_work_item(
            self.database, "TI-001", "TI", "first", management=self.management(),
            request_id="same-request"
        )
        with self.assertRaises(LiteError):
            create_work_item(
                self.database, "TI-001", "TI", "changed", management=self.management(),
                request_id="same-request"
            )

    def test_priority_order_is_stable(self):
        self.create("TI-003", priority="P3")
        self.create("TI-001", priority="P0")
        self.create("TI-002", priority="P1")
        self.assertEqual(["TI-001", "TI-002", "TI-003"], [row["work_item_id"] for row in list_work_items(self.database)])

    def test_claim_is_exclusive_and_generation_increases_after_expiry(self):
        self.create()
        first = self.claim("TI-001", 1, "planner-a", "PLANNER")
        with self.assertRaises(LiteError):
            self.claim("TI-001", 1, "planner-b", "PLANNER")
        connection = open_database(self.database)
        connection.execute("UPDATE claims SET expires_at='2000-01-01T00:00:00+00:00' WHERE claim_id=?", (first["claimId"],))
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(LiteError, "IN_PROGRESS_WITHOUT_LIVE_CLAIM"):
            self.claim("TI-001", 1, "planner-b", "PLANNER")
        check = workflow_check(self.database, self.project_root, "TI-001")
        arguments = check["nextStep"]["arguments"]
        target = arguments["expectedActivity"][0]
        reconciled = reconcile_expired(
            self.database, "TI-001", "claim", target["resourceId"],
            target["ownerId"], target["generation"], arguments["requestId"],
            fingerprint=arguments["fingerprint"],
            expected_activity=arguments["expectedActivity"],
            not_after=arguments["notAfter"], project_root=self.project_root,
        )
        self.assertEqual("OK", reconciled["status"])
        second = self.claim("TI-001", 1, "planner-b", "PLANNER")
        self.assertGreater(second["generation"], first["generation"])

    def test_concurrent_claim_has_one_winner(self):
        self.create()
        results = []
        barrier = threading.Barrier(2)

        def run(agent):
            barrier.wait()
            try:
                self.claim("TI-001", 1, agent, "PLANNER")
                results.append("ok")
            except LiteError:
                results.append("rejected")

        threads = [threading.Thread(target=run, args=("planner-{0}".format(i),)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(["ok", "rejected"], sorted(results))

    def test_repository_has_one_writer(self):
        self.create("TI-001")
        self.create("TI-002")
        self.claim("TI-001", 1, "planner-a", "PLANNER")
        self.claim("TI-002", 1, "planner-b", "PLANNER")
        acquire_repository_lock(self.database, "TI-001", "repo", "planner-a", self.expires())
        with self.assertRaises(LiteError):
            acquire_repository_lock(self.database, "TI-002", "repo", "planner-b", self.expires())
        release_repository_lock(self.database, "TI-001", "repo", "planner-a")
        acquire_repository_lock(self.database, "TI-002", "repo", "planner-b", self.expires())

    def test_reviewer_cannot_take_repository_writer(self):
        self.create()
        self.claim("TI-001", 1, "planner", "PLANNER")
        self.complete("TI-001", 1, "planner")
        transition(self.database, "TI-001", "submit_plan", "planner")
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        with self.assertRaises(LiteError):
            acquire_repository_lock(self.database, "TI-001", "repo", "reviewer", self.expires())

    def test_claim_and_submission_require_writer_release(self):
        self.create()
        self.claim("TI-001", 1, "planner", "PLANNER")
        acquire_repository_lock(self.database, "TI-001", "repo", "planner", self.expires())
        self.complete("TI-001", 1, "planner")
        with self.assertRaises(LiteError):
            release_claim(self.database, "TI-001", "planner")
        item = transition(self.database, "TI-001", "submit_plan", "planner")
        self.assertEqual("PLAN_REVIEW_PENDING", item["state"])
        connection = open_database(self.database)
        self.assertEqual(0, connection.execute(
            "SELECT count(*) FROM repository_locks WHERE work_item_id='TI-001' "
            "AND status='ACTIVE'"
        ).fetchone()[0])
        connection.close()

    def test_hold_blocks_claim_and_resume_restores(self):
        self.create()
        set_hold(self.database, "TI-001", "human", True)
        with self.assertRaises(LiteError):
            self.claim("TI-001", 1, "planner", "PLANNER")
        set_hold(self.database, "TI-001", "human", False)
        self.claim("TI-001", 1, "planner", "PLANNER")

    def test_blocked_item_is_not_claimable(self):
        self.create()
        self.claim("TI-001", 1, "planner", "PLANNER")
        set_task_status(self.database, "TI-001", "TI-001-T01", "planner", "IN_PROGRESS")
        set_task_status(
            self.database, "TI-001", "TI-001-T01", "planner", "BLOCKED", ["dependency missing"]
        )
        self.assertEqual("BLOCKED", get_work_item(self.database, "TI-001")["queue_state"])
        with self.assertRaises(LiteError):
            self.claim("TI-001", 1, "planner-2", "PLANNER")
        item = unblock_task(self.database, "TI-001", "TI-001-T01", "human", "dependency ready")
        self.assertEqual(("CLAIMABLE", "NOT_STARTED"), (item["queue_state"], item["tasks"][0]["status"]))
        self.claim("TI-001", 1, "planner-2", "PLANNER")

    def test_plan_review_blocked_unblock_restores_reviewer_reclaim(self):
        self.create()
        self.claim("TI-001", 1, "planner", "PLANNER")
        self.complete("TI-001", 1, "planner")
        transition(self.database, "TI-001", "submit_plan", "planner")
        self.claim("TI-001", 3, "reviewer-1", "REVIEWER")
        blocked = record_agent_review(
            self.database, "TI-001", "PLAN", "reviewer-1", "REJECTED",
            self.structured_review(
                "PLAN", "BLOCKED", findings=[self.finding("PLAN")]
            ),
        )
        self.assertEqual(("BLOCKED", None), (
            blocked["queue_state"], blocked["current_role"]
        ))
        resumed = unblock_task(
            self.database, "TI-001", "TI-001-T03", "human", "dependency ready"
        )
        self.assertEqual(("CLAIMABLE", "REVIEWER", "claim TI-001-T03 as REVIEWER"), (
            resumed["queue_state"], resumed["current_role"], resumed["nextStep"]
        ))
        self.claim("TI-001", 3, "reviewer-2", "REVIEWER")

    def test_implementation_review_blocked_unblock_restores_reviewer_reclaim(self):
        self._through_implementation_submission()
        self.claim("TI-001", 3, "reviewer-1", "REVIEWER")
        blocked = record_agent_review(
            self.database, "TI-001", "FINAL", "reviewer-1", "REJECTED",
            self.structured_review(
                "IMPLEMENTATION", "BLOCKED",
                findings=[self.finding("IMPLEMENTATION", "IMPLEMENTATION-F001")],
            ),
        )
        self.assertEqual(("BLOCKED", None), (
            blocked["queue_state"], blocked["current_role"]
        ))
        resumed = unblock_task(
            self.database, "TI-001", "TI-001-T03", "human", "dependency ready"
        )
        self.assertEqual(("CLAIMABLE", "REVIEWER", "claim TI-001-T03 as REVIEWER"), (
            resumed["queue_state"], resumed["current_role"], resumed["nextStep"]
        ))
        self.claim("TI-001", 3, "reviewer-2", "REVIEWER")

    def test_plan_submit_atomically_completes_planning_task(self):
        self.create()
        self.claim("TI-001", 1, "planner", "PLANNER")
        item = transition(self.database, "TI-001", "submit_plan", "planner")
        self.assertEqual("PLAN_REVIEW_PENDING", item["state"])
        self.assertEqual("COMPLETED", item["tasks"][0]["status"])

    def test_plan_review_rejects_self_review(self):
        self.create()
        self.claim("TI-001", 1, "same-agent", "PLANNER")
        self.complete("TI-001", 1, "same-agent")
        transition(self.database, "TI-001", "submit_plan", "same-agent")
        self.claim("TI-001", 3, "same-agent", "REVIEWER")
        with self.assertRaises(LiteError):
            record_agent_review(self.database, "TI-001", "PLAN", "same-agent", "APPROVED", "bad")

    def test_plan_agent_rejection_returns_draft(self):
        self.create()
        self.claim("TI-001", 1, "planner", "PLANNER")
        self.complete("TI-001", 1, "planner")
        transition(self.database, "TI-001", "submit_plan", "planner")
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        item = record_agent_review(
            self.database, "TI-001", "PLAN", "reviewer", "REJECTED",
            self.revise_review("PLAN", "PLAN-F001"),
        )
        self.assertEqual(("DRAFT", "CLAIMABLE", "PLANNER"), (item["state"], item["queue_state"], item["current_role"]))

    def test_standard_plan_requires_human_gate(self):
        self.create()
        self.claim("TI-001", 1, "planner", "PLANNER")
        self.complete("TI-001", 1, "planner")
        transition(self.database, "TI-001", "submit_plan", "planner")
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        reviewed = record_agent_review(self.database, "TI-001", "PLAN", "reviewer", "APPROVED", "ok")
        self.assertEqual("WAITING_HUMAN", reviewed["queue_state"])
        approved = record_human_gate(self.database, "TI-001", "PLAN", "human", "APPROVED", "ok")
        self.assertEqual("PLAN_REVIEW_APPROVED", approved["state"])

    def test_standard_create_defaults_auto_and_plan_pass_is_system_approved(self):
        created = create_work_item(
            self.database, "TI-010", "TI", "auto item",
            management=self.management("TI-010"),
        )
        self.assertEqual(("AUTO_ON_PASS", "DEFAULT"), (
            created["humanGatePolicy"], created["humanGatePolicySource"]
        ))
        self.claim("TI-010", 1, "planner-auto", "PLANNER")
        self.complete("TI-010", 1, "planner-auto")
        transition(self.database, "TI-010", "submit_plan", "planner-auto")
        self.claim("TI-010", 3, "reviewer-auto", "REVIEWER")
        reviewed = record_agent_review(
            self.database, "TI-010", "PLAN", "reviewer-auto", "APPROVED",
            self.structured_review("PLAN", "PASS"), request_id="auto-plan-review",
        )
        self.assertEqual(("PLAN_REVIEW_APPROVED", "CLAIMABLE", "IMPLEMENTER"), (
            reviewed["state"], reviewed["queue_state"], reviewed["current_role"]
        ))
        connection = open_database(self.database)
        self.assertEqual(0, connection.execute(
            "SELECT count(*) FROM human_gates WHERE work_item_id='TI-010'"
        ).fetchone()[0])
        event = connection.execute(
            "SELECT actor_kind,actor_id,payload_json FROM events "
            "WHERE work_item_id='TI-010' AND event_type='AUTO_GATE_APPROVED'"
        ).fetchone()
        connection.close()
        self.assertEqual(("SYSTEM", "auto-gate"), (event[0], event[1]))
        payload = json.loads(event[2])
        self.assertEqual(("PLAN", "AUTO_ON_PASS", "auto-plan-review"), (
            payload["stage"], payload["policy"], payload["reviewRequestId"]
        ))

    def test_risk_requires_explicit_choice_and_audits_without_authorizing_action(self):
        for index, kind in enumerate(("REMOTE", "DESTRUCTIVE", "ANOMALOUS_STATE")):
            risk = {"protocolVersion": "AWB-CREATION-RISK-v1", "signals": [{
                "kind": kind, "source": "authorization.allowed",
                "evidence": "actual classified operation",
            }]}
            work_item_id = "TI-01{0}".format(index + 1)
            with self.assertRaisesRegex(LiteError, "explicit human review choice"):
                create_work_item(
                    self.database, work_item_id, "TI", "risk item",
                    management=self.management(work_item_id), creation_risk=risk,
                )
        connection = open_database(self.database)
        self.assertEqual(0, connection.execute(
            "SELECT count(*) FROM work_items WHERE work_item_id IN ('TI-011','TI-012','TI-013')"
        ).fetchone()[0])
        connection.close()
        risk = {"protocolVersion": "AWB-CREATION-RISK-v1", "signals": [{
            "kind": "REMOTE", "source": "authorization.allowed",
            "evidence": "create a remote repository",
        }]}
        created = create_work_item(
            self.database, "TI-011", "TI", "remote item",
            management=self.management("TI-011"), creation_risk=risk,
            human_review="AUTO_ON_PASS", decision_actor="user-a",
        )
        self.assertEqual("AUTO_ON_PASS", created["humanGatePolicy"])
        creation = timeline(self.database, "TI-011")[0]
        payload = json.loads(creation["payload_json"])
        self.assertEqual(("AUTO_ON_PASS", "user-a", False), (
            payload["riskPromptDecision"], payload["decisionActor"],
            payload["riskActionAuthorized"],
        ))

    def test_creation_risk_rejects_duplicates_unknown_kinds_and_credentials(self):
        signal = {"kind": "DESTRUCTIVE", "source": "scope",
                  "evidence": "delete generated state"}
        invalid = [
            {"protocolVersion": "AWB-CREATION-RISK-v1", "signals": [signal, signal]},
            {"protocolVersion": "AWB-CREATION-RISK-v1", "signals": [
                {"kind": "OTHER", "source": "scope", "evidence": "unknown"}
            ]},
            {"protocolVersion": "AWB-CREATION-RISK-v1", "signals": [
                {"kind": "REMOTE", "source": "scope", "evidence": "api_key=abcd"}
            ]},
        ]
        for index, risk in enumerate(invalid):
            with self.assertRaises(LiteError):
                create_work_item(
                    self.database, "TI-02{0}".format(index), "TI", "invalid risk",
                    management=self.management("TI-02{0}".format(index)),
                    creation_risk=risk, human_review="MANUAL", decision_actor="user-a",
                )

    def test_auto_final_requires_quality_and_review_replay_is_zero_write(self):
        create_work_item(
            self.database, "TI-030", "TI", "auto final",
            management=self.management("TI-030"),
        )
        self.claim("TI-030", 1, "planner-auto", "PLANNER")
        self.complete("TI-030", 1, "planner-auto")
        transition(self.database, "TI-030", "submit_plan", "planner-auto")
        self.claim("TI-030", 3, "plan-reviewer", "REVIEWER")
        record_agent_review(
            self.database, "TI-030", "PLAN", "plan-reviewer", "APPROVED",
            self.structured_review("PLAN", "PASS"), request_id="plan-replay",
        )
        first_count = len(timeline(self.database, "TI-030"))
        replayed = record_agent_review(
            self.database, "TI-030", "PLAN", "plan-reviewer", "APPROVED",
            self.structured_review("PLAN", "PASS"), request_id="plan-replay",
        )
        self.assertEqual(first_count, len(timeline(self.database, "TI-030")))
        self.assertEqual("PLAN_REVIEW_APPROVED", replayed["state"])
        with self.assertRaisesRegex(LiteError, "different content"):
            record_agent_review(
                self.database, "TI-030", "PLAN", "plan-reviewer", "REJECTED",
                self.structured_review("PLAN", "REVISE"), request_id="plan-replay",
            )
        self.claim("TI-030", 2, "implementer-auto", "IMPLEMENTER")
        transition(self.database, "TI-030", "start_implementation", "implementer-auto")
        self.complete("TI-030", 2, "implementer-auto")
        transition(
            self.database, "TI-030", "submit_implementation", "implementer-auto",
            verify_receipt=self.verify_receipt("TI-030", "implementer-auto"),
            project_root=self.temporary.name,
        )
        self.claim("TI-030", 3, "final-reviewer", "REVIEWER")
        connection = open_database(self.database)
        now, expiry = datetime.datetime.now(datetime.timezone.utc).replace(
            microsecond=0).isoformat(), self.expires()
        connection.execute("INSERT INTO orchestrator_instances VALUES(?,?,?)",
                           ("terminal-owner", now, now))
        connection.execute(
            "INSERT INTO orchestrator_leases VALUES(?,?,?,1,'ACTIVE',?,?,?,NULL)",
            ("terminal-lease", "TI-030", "terminal-owner", now, now, expiry),
        )
        connection.execute(
            "INSERT INTO repository_locks VALUES(?,?,?,?,1,'ACTIVE',?,?,NULL)",
            ("terminal-writer", "terminal-repo", "TI-030", "final-reviewer",
             now, expiry),
        )
        connection.commit()
        connection.close()
        with mock.patch("agent_workboard.lite._review_materialized",
                        side_effect=RuntimeError("terminal post-apply fault")):
            with self.assertRaisesRegex(RuntimeError, "terminal post-apply fault"):
                record_agent_review(
                    self.database, "TI-030", "FINAL", "final-reviewer", "APPROVED",
                    self.structured_review("IMPLEMENTATION", "PASS"),
                    request_id="final-terminal-fault",
                )
        connection = open_database(self.database)
        self.assertEqual("IMPLEMENTATION_COMPLETED", connection.execute(
            "SELECT state FROM work_items WHERE work_item_id='TI-030'"
        ).fetchone()[0])
        self.assertEqual(3, connection.execute(
            "SELECT (SELECT count(*) FROM claims WHERE work_item_id='TI-030' AND status='ACTIVE') + "
            "(SELECT count(*) FROM repository_locks WHERE work_item_id='TI-030' AND status='ACTIVE') + "
            "(SELECT count(*) FROM orchestrator_leases WHERE work_item_id='TI-030' AND status='ACTIVE')"
        ).fetchone()[0])
        connection.close()
        done = record_agent_review(
            self.database, "TI-030", "FINAL", "final-reviewer", "APPROVED",
            self.structured_review("IMPLEMENTATION", "PASS"),
        )
        self.assertEqual(("FINAL_ACCEPTANCE_APPROVED", "HELD", "TERMINAL_STATE"), (
            done["state"], done["queue_state"], done["held_reason"]
        ))
        self.assertEqual(2, sum(
            row["event_type"] == "AUTO_GATE_APPROVED"
            for row in timeline(self.database, "TI-030")
        ))
        connection = open_database(self.database)
        self.assertEqual(0, connection.execute(
            "SELECT count(*) FROM claims WHERE work_item_id='TI-030' AND status='ACTIVE'"
        ).fetchone()[0])
        self.assertEqual("RELEASED", connection.execute(
            "SELECT status FROM repository_locks WHERE lock_id='terminal-writer'"
        ).fetchone()[0])
        self.assertEqual("RELEASED", connection.execute(
            "SELECT status FROM orchestrator_leases WHERE lease_id='terminal-lease'"
        ).fetchone()[0])
        connection.close()
        terminal = [row for row in timeline(self.database, "TI-030")
                    if row["event_type"] == "TERMINAL_ACTIVITY_RECONCILED"]
        self.assertEqual(1, len(terminal))
        self.assertEqual(3, len(json.loads(terminal[0]["payload_json"])["resources"]))
        final_review = [row for row in timeline(self.database, "TI-030")
                        if row["event_type"] == "AGENT_FINAL_REVIEW"][-1]
        reviewer_claim = json.loads(final_review["payload_json"])["reviewerClaim"]
        self.assertEqual(("final-reviewer", "REVIEWER", "RELEASED"), (
            reviewer_claim["agentId"], reviewer_claim["role"],
            reviewer_claim["status"],
        ))
        resources = json.loads(terminal[0]["payload_json"])["resources"]
        bound = [value for value in resources
                 if value.get("source") == "FINAL_REVIEW_CLAIM"]
        self.assertEqual(1, len(bound))
        self.assertEqual(("RELEASED", "INACTIVE", "RELEASED", False), (
            bound[0]["beforeStatus"], bound[0]["effectiveStatus"],
            bound[0]["afterStatus"], bound[0]["mutated"],
        ))

    def test_manual_final_binds_released_reviewer_and_gate_fault_is_atomic(self):
        self._through_implementation_submission()
        self.claim("TI-001", 3, "manual-reviewer", "REVIEWER")
        record_agent_review(
            self.database, "TI-001", "FINAL", "manual-reviewer", "APPROVED",
            self.structured_review("IMPLEMENTATION", "PASS"),
            request_id="manual-final-review",
        )
        connection = open_database(self.database)
        review_event = connection.execute(
            "SELECT payload_json FROM events WHERE request_id='manual-final-review'"
        ).fetchone()
        binding = json.loads(review_event[0])["reviewerClaim"]
        self.assertEqual("RELEASED", binding["status"])
        before = "\n".join(connection.iterdump())
        connection.close()
        with mock.patch("agent_workboard.lite._terminal_activity_materialized",
                        side_effect=RuntimeError("manual terminal fault")):
            with self.assertRaisesRegex(RuntimeError, "manual terminal fault"):
                record_human_gate(
                    self.database, "TI-001", "FINAL", "human", "APPROVED", "ok",
                    request_id="manual-final-fault",
                )
        connection = open_database(self.database)
        self.assertEqual(before, "\n".join(connection.iterdump()))
        connection.close()
        done = record_human_gate(
            self.database, "TI-001", "FINAL", "human", "APPROVED", "ok",
            request_id="manual-final-success",
        )
        self.assertEqual("FINAL_ACCEPTANCE_APPROVED", done["state"])
        terminal = [row for row in timeline(self.database, "TI-001")
                    if row["event_type"] == "TERMINAL_ACTIVITY_RECONCILED"][-1]
        special = [value for value in json.loads(terminal["payload_json"])["resources"]
                   if value.get("source") == "FINAL_REVIEW_CLAIM"]
        self.assertEqual(binding["claimId"], special[0]["resourceId"])

    def test_manual_final_legacy_binding_is_unique_and_fail_closed(self):
        self._through_implementation_submission()
        self.claim("TI-001", 3, "legacy-reviewer", "REVIEWER")
        record_agent_review(
            self.database, "TI-001", "FINAL", "legacy-reviewer", "APPROVED",
            self.structured_review("IMPLEMENTATION", "PASS"),
            request_id="legacy-final-review",
        )
        connection = open_database(self.database)
        row = connection.execute(
            "SELECT event_id,payload_json,created_at FROM events "
            "WHERE request_id='legacy-final-review'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload.pop("reviewerClaim")
        connection.execute(
            "UPDATE events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")), row["event_id"]),
        )
        connection.commit()
        connection.close()
        done = record_human_gate(
            self.database, "TI-001", "FINAL", "human", "APPROVED", "legacy ok",
            request_id="legacy-final-success",
        )
        self.assertEqual("FINAL_ACCEPTANCE_APPROVED", done["state"])

        self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.temporary.name, "workboard.db")
        initialize_database(self.database)
        self._through_implementation_submission()
        self.claim("TI-001", 3, "legacy-reviewer", "REVIEWER")
        record_agent_review(
            self.database, "TI-001", "FINAL", "legacy-reviewer", "APPROVED",
            self.structured_review("IMPLEMENTATION", "PASS"),
            request_id="legacy-ambiguous-review",
        )
        connection = open_database(self.database)
        row = connection.execute(
            "SELECT event_id,payload_json,created_at FROM events "
            "WHERE request_id='legacy-ambiguous-review'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload.pop("reviewerClaim")
        reviewer_task = connection.execute(
            "SELECT task_id FROM tasks WHERE work_item_id='TI-001' AND owner_role='REVIEWER'"
        ).fetchone()[0]
        generation = connection.execute(
            "SELECT max(generation)+1 FROM claims WHERE work_item_id='TI-001'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")), row["event_id"]),
        )
        connection.execute(
            "INSERT INTO claims VALUES(?,?,?,?,?,?,'RELEASED',?,?,?)",
            ("ambiguous-review-claim", "TI-001", reviewer_task, "legacy-reviewer",
             "REVIEWER", generation, row["created_at"], self.expires(),
             row["created_at"]),
        )
        connection.commit()
        before = "\n".join(connection.iterdump())
        connection.close()
        with self.assertRaisesRegex(LiteError, "FINAL_REVIEW_CLAIM_BINDING_INVALID"):
            record_human_gate(
                self.database, "TI-001", "FINAL", "human", "APPROVED", "ambiguous",
                request_id="legacy-final-refused",
            )
        connection = open_database(self.database)
        self.assertEqual(before, "\n".join(connection.iterdump()))
        connection.close()

    def test_auto_final_receipt_drift_fails_closed_without_review_event(self):
        create_work_item(
            self.database, "TI-031", "TI", "auto fail closed",
            management=self.management("TI-031"),
        )
        self.claim("TI-031", 1, "planner-auto", "PLANNER")
        self.complete("TI-031", 1, "planner-auto")
        transition(self.database, "TI-031", "submit_plan", "planner-auto")
        self.claim("TI-031", 3, "plan-reviewer", "REVIEWER")
        record_agent_review(
            self.database, "TI-031", "PLAN", "plan-reviewer", "APPROVED",
            self.structured_review("PLAN", "PASS"),
        )
        self.claim("TI-031", 2, "implementer-auto", "IMPLEMENTER")
        transition(self.database, "TI-031", "start_implementation", "implementer-auto")
        self.complete("TI-031", 2, "implementer-auto")
        transition(
            self.database, "TI-031", "submit_implementation", "implementer-auto",
            verify_receipt=self.verify_receipt("TI-031", "implementer-auto"),
            project_root=self.temporary.name,
        )
        connection = open_database(self.database)
        row = connection.execute(
            "SELECT event_id,payload_json FROM events WHERE work_item_id='TI-031' "
            "AND event_type='VERIFY_RECEIPT_RECORDED'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["receipt"]["core"]["checks"][0]["result"] = "FAIL"
        connection.execute(
            "UPDATE events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), row["event_id"]),
        )
        connection.commit()
        connection.close()
        self.claim("TI-031", 3, "final-reviewer", "REVIEWER")
        with self.assertRaisesRegex(LiteError, "VERIFY_FINAL_PHASE_REQUIRED"):
            record_agent_review(
                self.database, "TI-031", "FINAL", "final-reviewer", "APPROVED",
                self.structured_review("IMPLEMENTATION", "PASS"),
            )
        events = timeline(self.database, "TI-031")
        self.assertFalse(any(row["event_type"] == "AGENT_FINAL_REVIEW"
                             for row in events))
        self.assertEqual(1, sum(row["event_type"] == "AUTO_GATE_APPROVED"
                                for row in events))

    def test_human_plan_rejection_returns_draft(self):
        self.create()
        self.claim("TI-001", 1, "planner", "PLANNER")
        self.complete("TI-001", 1, "planner")
        transition(self.database, "TI-001", "submit_plan", "planner")
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        record_agent_review(self.database, "TI-001", "PLAN", "reviewer", "APPROVED", "ok")
        item = record_human_gate(self.database, "TI-001", "PLAN", "human", "REJECTED", "revise")
        self.assertEqual(("DRAFT", "PLANNER"), (item["state"], item["current_role"]))

    def test_implementation_cannot_start_before_plan_approval(self):
        self.create()
        with self.assertRaises(LiteError):
            self.claim("TI-001", 2, "implementer", "IMPLEMENTER")

    def test_submit_implementation_requires_completed_task_and_tests(self):
        self.through_plan_approval()
        self.claim("TI-001", 2, "implementer", "IMPLEMENTER")
        transition(self.database, "TI-001", "start_implementation", "implementer")
        with self.assertRaises(LiteError):
            transition(
                self.database, "TI-001", "submit_implementation", "implementer",
                project_root=self.temporary.name,
            )
        item = transition(
            self.database, "TI-001", "submit_implementation", "implementer",
            verify_receipt=self.verify_receipt("TI-001", "implementer"),
            project_root=self.temporary.name,
        )
        self.assertEqual("IMPLEMENTATION_COMPLETED", item["state"])

    def test_management_risk_drift_refuses_submission_review_and_final_zero_write(self):
        def begin(work_item_id):
            self.through_plan_approval(work_item_id)
            implementer = work_item_id + "-implementer"
            self.claim(work_item_id, 2, implementer, "IMPLEMENTER")
            transition(self.database, work_item_id, "start_implementation", implementer)
            return implementer, self.verify_receipt(work_item_id, implementer)

        def amend_risk(work_item_id, suffix):
            management = self.management(work_item_id)
            management["scope"] = [
                "cross-version migration and rollback implementation"
            ]
            amend_management(
                self.database, work_item_id, "human", management,
                "risk scope changed", request_id="risk-drift-" + suffix,
            )

        implementer, receipt_id = begin("TI-DRIFT-SUBMIT")
        amend_risk("TI-DRIFT-SUBMIT", "submit")
        before = self.database_snapshot()
        with self.assertRaisesRegex(LiteError, "VERIFY_CLASSIFIER_DRIFT"):
            transition(
                self.database, "TI-DRIFT-SUBMIT", "submit_implementation",
                implementer, verify_receipt=receipt_id,
                project_root=self.temporary.name,
            )
        self.assertEqual(before, self.database_snapshot())

        implementer, receipt_id = begin("TI-DRIFT-REVIEW")
        transition(
            self.database, "TI-DRIFT-REVIEW", "submit_implementation",
            implementer, verify_receipt=receipt_id,
            project_root=self.temporary.name,
        )
        self.claim("TI-DRIFT-REVIEW", 3, "drift-reviewer", "REVIEWER")
        amend_risk("TI-DRIFT-REVIEW", "review")
        before = self.database_snapshot()
        with self.assertRaisesRegex(LiteError, "VERIFY_CLASSIFIER_DRIFT"):
            record_agent_review(
                self.database, "TI-DRIFT-REVIEW", "FINAL", "drift-reviewer",
                "APPROVED", self.structured_review("IMPLEMENTATION", "PASS"),
            )
        self.assertEqual(before, self.database_snapshot())

        implementer, receipt_id = begin("TI-DRIFT-FINAL")
        transition(
            self.database, "TI-DRIFT-FINAL", "submit_implementation",
            implementer, verify_receipt=receipt_id,
            project_root=self.temporary.name,
        )
        self.claim("TI-DRIFT-FINAL", 3, "final-reviewer", "REVIEWER")
        record_agent_review(
            self.database, "TI-DRIFT-FINAL", "FINAL", "final-reviewer",
            "APPROVED", self.structured_review("IMPLEMENTATION", "PASS"),
        )
        amend_risk("TI-DRIFT-FINAL", "final")
        before = self.database_snapshot()
        with self.assertRaisesRegex(LiteError, "VERIFY_CLASSIFIER_DRIFT"):
            record_human_gate(
                self.database, "TI-DRIFT-FINAL", "FINAL", "human",
                "APPROVED", "stale receipt must fail",
            )
        self.assertEqual(before, self.database_snapshot())

    def test_standard_public_api_end_to_end(self):
        self.through_plan_approval()
        self.claim("TI-001", 2, "implementer", "IMPLEMENTER")
        transition(self.database, "TI-001", "start_implementation", "implementer")
        self.complete("TI-001", 2, "implementer")
        transition(
            self.database, "TI-001", "submit_implementation", "implementer",
            verify_receipt=self.verify_receipt("TI-001", "implementer"),
            project_root=self.temporary.name,
        )
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        reviewed = record_agent_review(
            self.database, "TI-001", "FINAL", "reviewer", "APPROVED",
            self.structured_review("IMPLEMENTATION", "PASS"),
        )
        self.assertEqual("WAITING_HUMAN", reviewed["queue_state"])
        done = record_human_gate(self.database, "TI-001", "FINAL", "human", "APPROVED", "accepted")
        self.assertEqual(("FINAL_ACCEPTANCE_APPROVED", "HELD", "TERMINAL_STATE"),
                         (done["state"], done["queue_state"], done["held_reason"]))
        self.assertEqual("COMPLETED", done["tasks"][2]["status"])
        self.assertGreaterEqual(len(timeline(self.database, "TI-001")), 12)

    def _through_implementation_submission(self, implementer="implementer"):
        self.through_plan_approval()
        self.claim("TI-001", 2, implementer, "IMPLEMENTER")
        transition(self.database, "TI-001", "start_implementation", implementer)
        self.complete("TI-001", 2, implementer)
        transition(
            self.database, "TI-001", "submit_implementation", implementer,
            verify_receipt=self.verify_receipt("TI-001", implementer),
            project_root=self.temporary.name,
        )

    def test_final_agent_rejection_returns_implementing(self):
        self._through_implementation_submission()
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        item = record_agent_review(
            self.database, "TI-001", "FINAL", "reviewer", "REJECTED",
            self.revise_review("IMPLEMENTATION", "IMPLEMENTATION-F001"),
        )
        self.assertEqual(("IMPLEMENTING", "IMPLEMENTER"), (item["state"], item["current_role"]))

    def test_implementer_cannot_review_own_delivery(self):
        self._through_implementation_submission(implementer="same-agent")
        self.claim("TI-001", 3, "same-agent", "REVIEWER")
        with self.assertRaises(LiteError):
            record_agent_review(
                self.database, "TI-001", "FINAL", "same-agent", "APPROVED",
                self.structured_review("IMPLEMENTATION", "PASS"),
            )

    def test_human_final_rejection_returns_implementing(self):
        self._through_implementation_submission()
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        record_agent_review(
            self.database, "TI-001", "FINAL", "reviewer", "APPROVED",
            self.structured_review("IMPLEMENTATION", "PASS"),
        )
        item = record_human_gate(self.database, "TI-001", "FINAL", "human", "REJECTED", "fix")
        self.assertEqual(("IMPLEMENTING", "IMPLEMENTER"), (item["state"], item["current_role"]))

    def test_plan_review_strict_three_plus_one_plus_one_stops_before_round_six(self):
        self.create()
        self.claim("TI-001", 1, "planner-0", "PLANNER")
        self.complete("TI-001", 1, "planner-0")
        transition(self.database, "TI-001", "submit_plan", "planner-0")
        for round_number in (1, 2, 3):
            reviewer = "reviewer-{0}".format(round_number)
            self.claim("TI-001", 3, reviewer, "REVIEWER")
            item = record_agent_review(
                self.database, "TI-001", "PLAN", reviewer, "REJECTED",
                self.revise_review("PLAN", "PLAN-F001"),
            )
            if round_number < 3:
                planner = "planner-{0}".format(round_number)
                self.claim("TI-001", 1, planner, "PLANNER")
                self.complete("TI-001", 1, planner)
                transition(
                    self.database, "TI-001", "submit_plan", planner,
                )
        self.assertEqual(("REVIEWER", "CLAIMABLE"), (item["current_role"], item["queue_state"]))
        self.claim("TI-001", 3, "convergence", "REVIEWER")
        convergence = self.structured_review(
            "PLAN", "CONVERGENCE_REVISE", mode="CONVERGENCE",
            findings=[self.finding("PLAN")],
        )
        record_agent_review(
            self.database, "TI-001", "PLAN", "convergence", "REJECTED", convergence
        )
        self.claim("TI-001", 1, "planner-final", "PLANNER")
        self.complete("TI-001", 1, "planner-final")
        transition(
            self.database, "TI-001", "submit_plan", "planner-final",
        )
        self.claim("TI-001", 3, "reviewer-5", "REVIEWER")
        exhausted = record_agent_review(
            self.database, "TI-001", "PLAN", "reviewer-5", "REJECTED",
            self.revise_review("PLAN", "PLAN-F001"),
        )
        self.assertEqual(("WAITING_HUMAN", None), (
            exhausted["queue_state"], exhausted["current_role"]
        ))
        counters = exhausted["reviewConvergence"]["PLAN"]
        self.assertEqual((4, True, 5, "REVISE"), (
            counters["ordinaryRoundsUsed"], counters["convergenceUsed"],
            counters["totalRoundsUsed"], counters["latestResult"],
        ))
        with self.assertRaises(LiteError):
            self.claim("TI-001", 3, "reviewer-6", "REVIEWER")

    def test_convergence_pass_waiting_human_and_blocked_paths(self):
        cases = (
            ("TI-911", "PASS", "APPROVED", "WAITING_HUMAN", None),
            ("TI-912", "WAITING_HUMAN", "REJECTED", "WAITING_HUMAN", None),
            ("TI-913", "BLOCKED", "REJECTED", "BLOCKED", "REVIEW_BLOCKED"),
        )
        for work_item_id, result, decision, queue, blocked_reason in cases:
            self.to_plan_convergence_round(work_item_id)
            reviewer = work_item_id + "-convergence"
            self.claim(work_item_id, 3, reviewer, "REVIEWER")
            findings = [] if result in ("PASS", "WAITING_HUMAN") else [self.finding("PLAN")]
            resolved = ["PLAN-F001"] if result == "PASS" else []
            item = record_agent_review(
                self.database, work_item_id, "PLAN", reviewer, decision,
                self.structured_review(
                    "PLAN", result, mode="CONVERGENCE", findings=findings,
                    resolved=resolved,
                ),
            )
            self.assertEqual((queue, blocked_reason), (item["queue_state"], item["blocked_reason"]))
            self.assertEqual(4, item["reviewConvergence"]["PLAN"]["totalRoundsUsed"])

    def test_round_five_pass_revise_and_blocked_paths(self):
        cases = (
            ("TI-921", "PASS", "APPROVED", "WAITING_HUMAN", ["PLAN-F001"]),
            ("TI-922", "REVISE", "REJECTED", "WAITING_HUMAN", []),
            ("TI-923", "BLOCKED", "REJECTED", "BLOCKED", []),
        )
        for work_item_id, result, decision, queue, resolved in cases:
            self.after_convergence_revision(work_item_id)
            reviewer = work_item_id + "-reviewer-5"
            self.claim(work_item_id, 3, reviewer, "REVIEWER")
            findings = [] if result == "PASS" else [self.finding("PLAN")]
            item = record_agent_review(
                self.database, work_item_id, "PLAN", reviewer, decision,
                self.structured_review(
                    "PLAN", result, findings=findings, resolved=resolved
                ),
            )
            self.assertEqual(queue, item["queue_state"])
            self.assertEqual(5, item["reviewConvergence"]["PLAN"]["totalRoundsUsed"])

    def test_implementation_suggestions_pass_and_stage_counter_is_independent(self):
        self._through_implementation_submission()
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        item = record_agent_review(
            self.database, "TI-001", "FINAL", "reviewer", "REJECTED",
            self.structured_review(
                "IMPLEMENTATION", "REVISE", suggestions=["future optimization"]
            ),
        )
        self.assertEqual("PASS", item["reviewConvergence"]["IMPLEMENTATION"]["latestResult"])
        self.assertEqual((1, 1), (
            item["reviewConvergence"]["PLAN"]["totalRoundsUsed"],
            item["reviewConvergence"]["IMPLEMENTATION"]["totalRoundsUsed"],
        ))

    def test_plan_deviation_blocks_and_returns_authority_to_planner_without_reset(self):
        self.through_plan_approval()
        self.claim("TI-001", 2, "implementer", "IMPLEMENTER")
        transition(self.database, "TI-001", "start_implementation", "implementer")
        acquire_repository_lock(
            self.database, "TI-001", "repo", "implementer", self.expires()
        )
        item = report_plan_deviation(
            self.database, "TI-001", "TI-001-T02", "implementer",
            {"reason": "external contract must change", "impact": "approved plan cannot work"},
        )
        self.assertEqual(("DRAFT", "BLOCKED", None, "PLAN_DEVIATION"), (
            item["state"], item["queue_state"], item["current_role"], item["blocked_reason"]
        ))
        self.assertIsNone(item["activeClaim"])
        self.assertEqual(1, item["reviewConvergence"]["PLAN"]["totalRoundsUsed"])
        resumed = unblock_task(
            self.database, "TI-001", "TI-001-T02", "human", "planning can resume"
        )
        self.assertEqual(("DRAFT", "CLAIMABLE", "PLANNER", None), (
            resumed["state"], resumed["queue_state"], resumed["current_role"],
            resumed["blocked_reason"],
        ))
        self.claim("TI-001", 1, "planner-2", "PLANNER")

    def test_fe_weighted_progress_requires_stable_ids_and_total_one_hundred(self):
        management = self.management("FE-901", item_type="FE")
        management["progressMethod"] = "WEIGHTED"
        management["stages"] = [
            {"id": "design", "weight": 25, "completion": 100},
            {"id": "delivery", "weight": 75, "completion": 0},
        ]
        item = create_work_item(
            self.database, "FE-901", "FE", "weighted", management=management
        )
        self.assertEqual(("WEIGHTED", 25), (
            item["progress"]["method"], item["progress"]["percent"]
        ))
        invalid = self.management("FE-902", item_type="FE")
        invalid["progressMethod"] = "WEIGHTED"
        invalid["stages"] = [{"id": "only", "weight": 99}]
        with self.assertRaises(LiteError):
            create_work_item(self.database, "FE-902", "FE", "bad weights", management=invalid)

    def test_fe_weighted_completion_requires_integer_zero_through_one_hundred(self):
        for index, completion in enumerate((True, 1.5, "50", -1, 101), 1):
            work_item_id = "FE-91{0}".format(index)
            management = self.management(work_item_id, item_type="FE")
            management["progressMethod"] = "WEIGHTED"
            management["stages"] = [{
                "id": "delivery", "weight": 100, "completion": completion,
            }]
            with self.assertRaisesRegex(LiteError, "completion"):
                create_work_item(
                    self.database, work_item_id, "FE", "invalid completion",
                    management=management,
                )

        for work_item_id, stage, expected in (
            ("FE-921", {"id": "delivery", "weight": 100}, 0),
            ("FE-922", {"id": "delivery", "weight": 100, "completion": 0}, 0),
            ("FE-923", {"id": "delivery", "weight": 100, "completion": 100}, 100),
        ):
            management = self.management(work_item_id, item_type="FE")
            management["progressMethod"] = "WEIGHTED"
            management["stages"] = [stage]
            item = create_work_item(
                self.database, work_item_id, "FE", "valid completion",
                management=management,
            )
            self.assertEqual(expected, item["progress"]["percent"])

    def test_formal_templates_and_consumers_share_contract_version(self):
        repository = os.path.dirname(os.path.dirname(__file__))
        template_root = os.path.join(repository, "docs", "work-item-templates")
        expected = {
            "README.md", "test-issue-template.md", "test-issue-trigger-rules.md",
            "fe-template.md", "remediation-plan-template.md", "wa-template.md",
            "awb-template.md", "usage-observation-template.md",
        }
        self.assertEqual(expected, set(os.listdir(template_root)))
        for name in expected:
            with open(os.path.join(template_root, name), encoding="utf-8") as handle:
                content = handle.read()
            self.assertIn("AWB-WORKITEM-MGMT-v1", content)
            if name != "README.md":
                self.assertIn("README.md", content)
            self.assertIsNone(re.search(r"^\|[^\n]*-T00(?:\s|`|\|)", content, re.MULTILINE))
        consumers = [
            os.path.join(repository, "src", "agent_workboard", "lite.py"),
            os.path.join(repository, "src", "agent_workboard", "resources", "spec", "mvp_lite_v1_1", "workitem.schema.json"),
            os.path.join(repository, "src", "agent_workboard", "resources", "spec", "mvp_lite_v1_1", "workflow.yaml"),
            os.path.join(repository, "src", "agent_workboard", "resources", "spec", "mvp_lite_v1_1", "AGENT-GUIDE.md"),
            os.path.join(repository, ".codex", "skills", "awb-orchestrator", "SKILL.md"),
        ]
        for path in consumers:
            with open(path, encoding="utf-8") as handle:
                self.assertIn("AWB-WORKITEM-MGMT-v1", handle.read())

    def test_transition_kernel_matrix_and_public_registration_are_closed(self):
        self.assertEqual(len(workflow_kernel.TRANSITION_MATRIX), len(set(
            workflow_kernel.TRANSITION_MATRIX
        )))
        self.assertTrue(workflow_kernel.PUBLIC_MUTATION_INTENTS)
        self.assertTrue(set(workflow_kernel.PUBLIC_MUTATION_INTENTS.values()).issubset(
            set(workflow_kernel.TRANSITION_MATRIX)
        ))
        snapshot = {
            "projectionFingerprint": "a" * 64,
        }
        for operation in workflow_kernel.TRANSITION_MATRIX:
            plan = workflow_kernel.build_transition_plan(
                operation, snapshot, {"operation": operation, "requestId": "r"},
                [{"table": "fixture", "compareAndSet": True}],
                {"stateHash": "b" * 64}, {"type": operation},
                {"status": "OK"}, {"action": "NONE", "arguments": {}},
            )
            self.assertEqual(operation, plan.as_dict()["operation"])
            self.assertEqual(64, len(plan.intent_fingerprint))
        with self.assertRaisesRegex(ValueError, "unregistered"):
            workflow_kernel.build_transition_plan(
                "UNKNOWN_EDGE", snapshot, {}, [], {}, {}, {},
                {"action": "NONE", "arguments": {}},
            )

    def test_transition_materializer_is_unique_and_write_ops_are_immutable(self):
        operation = workflow_kernel.WriteOp(
            "UPDATE", "tasks", (("status", "IN_PROGRESS"),),
            (("task_id", "AWB-900-T01"), ("status", "NOT_STARTED")),
        )
        with self.assertRaises(AttributeError):
            operation.table = "claims"
        snapshot = {"projectionFingerprint": "a" * 64}
        plan = workflow_kernel.build_transition_plan(
            "HOLD", snapshot, {"operation": "HOLD"}, (), {}, {},
            {"status": "OK", "nested": {"value": 1}},
            {"action": "NONE", "arguments": {}},
        )
        with self.assertRaises(TypeError):
            plan.receipt["nested"]["value"] = 2
        for function in (
                acquire_claim, release_claim, acquire_repository_lock,
                release_repository_lock):
            source = inspect.getsource(function)
            self.assertIn("_kernel_apply", source, function.__name__)
            self.assertIsNone(re.search(
                r'\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+'
                r'(?:work_items|tasks|claims|repository_locks|events)\b',
                source, re.IGNORECASE,
            ), function.__name__)
        materializer = inspect.getsource(workflow_kernel.apply_transition_plan)
        self.assertIn("connection.execute", materializer)
        self.assertIn("TRANSITION_COMPARE_AND_SET_CONFLICT", materializer)

    def test_public_adapters_cannot_bypass_managed_lifecycle_materializer(self):
        managed = (
            "work_items|tasks|claims|repository_locks|orchestrator_leases|"
            "reviews|events|human_gates|human_gate_reviews"
        )
        bypass = re.compile(
            r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:" + managed + r")\b",
            re.IGNORECASE | re.MULTILINE,
        )
        for module in (lite_module, orchestrator_module, candidate_module):
            source = inspect.getsource(module)
            self.assertIsNone(
                bypass.search(source),
                "managed lifecycle SQL bypass in {0}".format(module.__name__),
            )
            self.assertNotIn(
                "plan_lifecycle_bundle", source,
                "raw lifecycle bundle bypass in {0}".format(module.__name__),
            )
            self.assertIsNone(
                re.search(r'["\']writes["\']\s*:\s*[\(\[\{]', source),
                "raw managed-write intent in {0}".format(module.__name__),
            )

    def test_public_lifecycle_sql_has_a_kernel_transaction_guard(self):
        guarded = (
            create_work_item, backfill_management, amend_management,
            acquire_claim, release_claim, acquire_repository_lock,
            release_repository_lock, set_task_status, transition,
            record_agent_review, amend_plan_review, record_human_gate,
            set_hold, unblock_task, report_plan_deviation, workflow_advance,
            workflow_repair,
            candidate_module.prepare, candidate_module.freeze,
            candidate_module.build, candidate_module.quarantine,
            candidate_module.finalize,
            candidate_module.publication_authorize,
            candidate_module.publication_postflight,
            candidate_module.publication_retry,
        )
        for function in guarded:
            source = inspect.getsource(function)
            self.assertIn("BEGIN IMMEDIATE", source, function.__name__)
            self.assertTrue(
                "_kernel_assert" in source or
                "_kernel_apply" in source,
                function.__name__,
            )
        for function in (
                orchestrator_module.claim, orchestrator_module.claim_next,
                orchestrator_module.renew, orchestrator_module.release,
                orchestrator_module.recover):
            self.assertIn("_mutate", inspect.getsource(function), function.__name__)
        source = inspect.getsource(orchestrator_module.reconcile_expired)
        self.assertIn("BEGIN IMMEDIATE", source)
        self.assertIn("_kernel_apply", source)

    def test_review_route_table_rejects_every_undeclared_round_result(self):
        results = {
            "PASS", "REVISE", "REVISE_TO_PLANNER", "CONVERGENCE_REVISE",
            "BLOCKED", "WAITING_HUMAN", "AMENDED",
        }
        allowed = {
            1: {"PASS", "REVISE", "REVISE_TO_PLANNER", "BLOCKED"},
            2: {"PASS", "REVISE", "REVISE_TO_PLANNER", "BLOCKED"},
            3: {"PASS", "REVISE", "REVISE_TO_PLANNER", "BLOCKED"},
            4: {"PASS", "CONVERGENCE_REVISE", "WAITING_HUMAN", "BLOCKED", "AMENDED"},
            5: {"PASS", "REVISE", "BLOCKED", "WAITING_HUMAN"},
        }
        for stage in ("PLAN", "FINAL"):
            for round_number in range(1, 6):
                for result in results:
                    if result in allowed[round_number]:
                        route = workflow_kernel.review_route(
                            stage, result, round_number, "STANDARD", "MANUAL"
                        )
                        self.assertEqual({
                            "state", "queue", "role", "authorTask",
                            "reviewerTask", "autoEligible",
                        }.issubset(route), True)
                    else:
                        with self.assertRaises(ValueError):
                            workflow_kernel.review_route(
                                stage, result, round_number, "STANDARD", "MANUAL"
                            )

    def test_seeded_claim_review_model_matches_disposable_sqlite_projection(self):
        generator = random.Random(250317)
        for index in range(20):
            work_item_id = "AWB-MODEL-{0:02d}".format(index)
            self.create(work_item_id)
            planner = work_item_id + "-planner"
            self.claim(work_item_id, 1, planner, "PLANNER")
            item = get_work_item(self.database, work_item_id)
            self.assertEqual(("CLAIMED", "IN_PROGRESS"), (
                item["queue_state"], item["tasks"][0]["status"],
            ))
            if generator.choice((False, True)):
                release_claim(self.database, work_item_id, planner)
                item = get_work_item(self.database, work_item_id)
                self.assertEqual(("CLAIMABLE", "NOT_STARTED"), (
                    item["queue_state"], item["tasks"][0]["status"],
                ))
                self.claim(work_item_id, 1, planner, "PLANNER")
            transition(self.database, work_item_id, "submit_plan", planner)
            reviewer = work_item_id + "-reviewer"
            self.claim(work_item_id, 3, reviewer, "REVIEWER")
            result = generator.choice(("PASS", "REVISE"))
            review = self.structured_review(
                "PLAN", result,
                findings=([] if result == "PASS" else [self.finding("PLAN")]),
            )
            item = record_agent_review(
                self.database, work_item_id, "PLAN", reviewer,
                "APPROVED" if result == "PASS" else "REJECTED", review,
            )
            reference = workflow_kernel.review_route(
                "PLAN", result, 1, "STANDARD", "MANUAL"
            )
            self.assertEqual((reference["queue"], reference["role"]),
                             (item["queue_state"], item["current_role"]))
            self.assertEqual(reference["reviewerTask"], item["tasks"][2]["status"])

    def test_expiry_boundary_uses_one_clock_and_combined_bundle_is_atomic(self):
        work_item_id = "AWB-EXPIRY-MODEL"
        self.create(work_item_id)
        claim = self.claim(work_item_id, 1, "planner", "PLANNER")
        writer = acquire_repository_lock(
            self.database, work_item_id, "repo-expiry", "planner", self.expires()
        )
        boundary = "2030-01-01T00:00:00+00:00"
        connection = open_database(self.database)
        connection.execute(
            "UPDATE claims SET expires_at=? WHERE claim_id=?",
            (boundary, claim["claimId"]),
        )
        connection.execute(
            "UPDATE repository_locks SET expires_at=? WHERE lock_id=?",
            (boundary, writer["lockId"]),
        )
        connection.commit()
        connection.execute("BEGIN")
        before = lite_module._check_one(
            connection, self.temporary.name, work_item_id,
            evaluation_time="2029-12-31T23:59:59.999999+00:00",
        )
        at = lite_module._check_one(
            connection, self.temporary.name, work_item_id,
            evaluation_time=boundary,
        )
        connection.rollback(); connection.close()
        self.assertEqual("PASS", before["status"])
        self.assertEqual(("VIOLATION", 2), (
            at["status"], len(at["nextStep"]["arguments"]["expectedActivity"]),
        ))
        # Move the exact boundary behind the trusted apply clock without
        # changing any identity/generation or business projection.
        connection = open_database(self.database)
        past = "2000-01-01T00:00:00+00:00"
        connection.execute("UPDATE claims SET expires_at=? WHERE claim_id=?",
                           (past, claim["claimId"]))
        connection.execute("UPDATE repository_locks SET expires_at=? WHERE lock_id=?",
                           (past, writer["lockId"]))
        connection.commit(); connection.close()
        check = workflow_check(self.database, self.project_root, work_item_id)
        proof = check["nextStep"]["arguments"]
        result = reconcile_expired(
            self.database, work_item_id, "claim", claim["claimId"], "planner",
            claim["generation"], proof["requestId"],
            fingerprint=proof["fingerprint"],
            expected_activity=proof["expectedActivity"],
            not_after=proof["notAfter"], project_root=self.project_root,
        )
        self.assertEqual("OK", result["status"])
        item = get_work_item(self.database, work_item_id)
        self.assertEqual(("CLAIMABLE", "NOT_STARTED"), (
            item["queue_state"], item["tasks"][0]["status"],
        ))
        self.assertEqual("PASS", workflow_check(
            self.database, self.temporary.name, work_item_id
        )["status"])

    def test_workflow_check_is_read_only_and_content_free(self):
        work_item_id = "AWB-PRIVACY"
        self.create(work_item_id)
        connection = open_database(self.database)
        connection.execute(
            "UPDATE work_items SET title=? WHERE work_item_id=?",
            ("SECRET-PROMPT-NEVER-EMIT", work_item_id),
        )
        connection.commit(); before = "\n".join(connection.iterdump())
        connection.close()
        result = workflow_check(self.database, self.temporary.name, work_item_id)
        encoded = json.dumps(result, sort_keys=True)
        self.assertNotIn("SECRET-PROMPT-NEVER-EMIT", encoded)
        self.assertNotIn(self.temporary.name, encoded)
        connection = open_database(self.database)
        self.assertEqual(before, "\n".join(connection.iterdump()))
        connection.close()

    def test_review_row_event_coherence_detects_disposable_tampering(self):
        mutations = ("stage", "round", "result", "request", "findings", "actor")
        for mutation in mutations:
            work_item_id = "AWB-REVIEW-TAMPER-" + mutation.upper()
            self.create(work_item_id)
            planner = work_item_id + "-planner"
            self.claim(work_item_id, 1, planner, "PLANNER")
            transition(self.database, work_item_id, "submit_plan", planner)
            reviewer = work_item_id + "-reviewer"
            self.claim(work_item_id, 3, reviewer, "REVIEWER")
            record_agent_review(
                self.database, work_item_id, "PLAN", reviewer, "REJECTED",
                self.structured_review("PLAN", "REVISE",
                                       findings=[self.finding("PLAN")]),
            )
            connection = open_database(self.database)
            row = connection.execute(
                "SELECT * FROM reviews WHERE work_item_id=?", (work_item_id,)
            ).fetchone()
            event = connection.execute(
                "SELECT * FROM events WHERE work_item_id=? "
                "AND event_type='AGENT_PLAN_REVIEW'", (work_item_id,)
            ).fetchone()
            review = json.loads(row["summary"])
            payload = json.loads(event["payload_json"])
            if mutation == "stage":
                connection.execute("UPDATE reviews SET stage='FINAL' WHERE review_id=?",
                                   (row["review_id"],))
            elif mutation == "round":
                review["round"] = 2; payload["review"] = review
                connection.execute("UPDATE reviews SET summary=? WHERE review_id=?",
                                   (json.dumps(review), row["review_id"]))
                connection.execute("UPDATE events SET payload_json=? WHERE event_id=?",
                                   (json.dumps(payload), event["event_id"]))
            elif mutation == "result":
                review["result"] = "PASS"; payload["review"] = review
                connection.execute("UPDATE reviews SET summary=? WHERE review_id=?",
                                   (json.dumps(review), row["review_id"]))
                connection.execute("UPDATE events SET payload_json=? WHERE event_id=?",
                                   (json.dumps(payload), event["event_id"]))
            elif mutation == "request":
                connection.execute("UPDATE events SET request_id='' WHERE event_id=?",
                                   (event["event_id"],))
            elif mutation == "findings":
                review["resolvedFindingIds"] = ["F-NEVER-OPEN"]
                payload["review"] = review
                connection.execute("UPDATE reviews SET summary=? WHERE review_id=?",
                                   (json.dumps(review), row["review_id"]))
                connection.execute("UPDATE events SET payload_json=? WHERE event_id=?",
                                   (json.dumps(payload), event["event_id"]))
            else:
                connection.execute("UPDATE events SET actor_id='wrong-reviewer' "
                                   "WHERE event_id=?", (event["event_id"],))
            connection.commit(); connection.close()
            checked = workflow_check(self.database, self.temporary.name, work_item_id)
            self.assertEqual("VIOLATION", checked["status"], mutation)
            with self.assertRaisesRegex(LiteError, "WORKFLOW_INVARIANT_VIOLATION"):
                acquire_claim(
                    self.database, work_item_id, work_item_id + "-T01",
                    work_item_id + "-planner-2", "PLANNER", self.expires(),
                )

        valid_id = "AWB-REVIEW-VALID-GATE"
        self.create(valid_id)
        self.claim(valid_id, 1, valid_id + "-planner", "PLANNER")
        transition(self.database, valid_id, "submit_plan", valid_id + "-planner")
        self.claim(valid_id, 3, valid_id + "-reviewer", "REVIEWER")
        record_agent_review(
            self.database, valid_id, "PLAN", valid_id + "-reviewer", "APPROVED",
            self.structured_review("PLAN", "PASS"),
        )
        if get_work_item(self.database, valid_id)["queue_state"] == "WAITING_HUMAN":
            record_human_gate(self.database, valid_id, "PLAN", "human-a",
                              "APPROVED", "valid ordered gate")
        self.assertEqual("PASS", workflow_check(
            self.database, self.temporary.name, valid_id
        )["status"])

    def test_released_review_formats_are_closed_and_unknown_near_shape_refuses(self):
        for event_shape in ("structured", "summary"):
            work_item_id = "AWB-LEGACY-" + event_shape.upper()
            self.create(work_item_id)
            planner = work_item_id + "-planner"
            self.claim(work_item_id, 1, planner, "PLANNER")
            transition(self.database, work_item_id, "submit_plan", planner)
            reviewer = work_item_id + "-reviewer"
            self.claim(work_item_id, 3, reviewer, "REVIEWER")
            record_agent_review(
                self.database, work_item_id, "PLAN", reviewer, "APPROVED",
                self.structured_review("PLAN", "PASS"),
            )
            connection = open_database(self.database)
            event = connection.execute(
                "SELECT * FROM events WHERE work_item_id=? "
                "AND event_type='AGENT_PLAN_REVIEW'", (work_item_id,)
            ).fetchone()
            payload = json.loads(event["payload_json"])
            if event_shape == "structured":
                released = {"decision": payload["decision"],
                            "review": payload["review"]}
            else:
                released = {"decision": payload["decision"],
                            "summary": json.dumps(payload["review"],
                                                  sort_keys=True,
                                                  separators=(",", ":"))}
                connection.execute(
                    "UPDATE reviews SET summary=? WHERE work_item_id=?",
                    (released["summary"], work_item_id),
                )
            connection.execute(
                "UPDATE events SET payload_json=? WHERE event_id=?",
                (json.dumps(released, sort_keys=True, separators=(",", ":")),
                 event["event_id"]),
            )
            connection.commit(); connection.close()
            self.assertEqual("PASS", workflow_check(
                self.database, self.temporary.name, work_item_id
            )["status"])

            connection = open_database(self.database)
            released["unknownField"] = "must not resemble a released format"
            connection.execute(
                "UPDATE events SET payload_json=? WHERE event_id=?",
                (json.dumps(released), event["event_id"]),
            )
            connection.commit(); connection.close()
            self.assertEqual("VIOLATION", workflow_check(
                self.database, self.temporary.name, work_item_id
            )["status"])

    def test_current_released_board_snapshot_has_only_registered_awb024_violation(self):
        # Recreate the three closed public-b3-to-b6 event encodings without
        # reading the mutable workspace board.  The first two releases stored
        # the structured AWB-REVIEW-v1 value with and without a request
        # fingerprint.  The earlier summary encoding stored the closed
        # pre-protocol review JSON in both the review row and event.
        released_shapes = (
            ("AWB-004", "structured-with-request"),
            ("AWB-009", "structured"),
            ("AWB-015", "summary"),
        )
        expected_formats = {
            "AWB-004": "AWB-REVIEW-EVENT-v6",
            "AWB-009": "AWB-REVIEW-EVENT-v3-v6",
            "AWB-015": "AWB-REVIEW-SUMMARY-EVENT-v3-v6",
        }
        for work_item_id, shape in released_shapes:
            self.create(work_item_id)
            planner = work_item_id + "-planner"
            self.claim(work_item_id, 1, planner, "PLANNER")
            transition(self.database, work_item_id, "submit_plan", planner)
            reviewer = work_item_id + "-reviewer"
            self.claim(work_item_id, 3, reviewer, "REVIEWER")
            record_agent_review(
                self.database, work_item_id, "PLAN", reviewer, "APPROVED",
                self.structured_review("PLAN", "PASS"),
            )
            record_human_gate(
                self.database, work_item_id, "PLAN", "human-a", "APPROVED",
                "released ordered gate",
            )
            connection = open_database(self.database)
            row = connection.execute(
                "SELECT * FROM reviews WHERE work_item_id=?", (work_item_id,)
            ).fetchone()
            event = connection.execute(
                "SELECT * FROM events WHERE work_item_id=? "
                "AND event_type='AGENT_PLAN_REVIEW'", (work_item_id,)
            ).fetchone()
            review = json.loads(row["summary"])
            if shape == "structured-with-request":
                payload = {
                    "decision": row["decision"], "review": review,
                    "requestFingerprint": "released-b6-request-fingerprint",
                }
            elif shape == "structured":
                payload = {"decision": row["decision"], "review": review}
            else:
                legacy_review = {key: review[key] for key in (
                    "result", "round", "reviewerMode", "findings",
                    "resolvedFindingIds", "nonBlockingSuggestions",
                )}
                released_summary = json.dumps(
                    legacy_review, sort_keys=True, separators=(",", ":")
                )
                connection.execute(
                    "UPDATE reviews SET summary=? WHERE review_id=?",
                    (released_summary, row["review_id"]),
                )
                payload = {"decision": row["decision"],
                           "summary": released_summary}
            connection.execute(
                "UPDATE events SET payload_json=? WHERE event_id=?",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")),
                 event["event_id"]),
            )
            connection.commit()
            connection.close()

        self.create_awb024_b6_orphan()
        exact_artifact_sha = (
            "c406cffab3dc3ec91853637f08f2d7d80f45632457bc59e9d9e40019af07c09a"
        )
        before = self.database_snapshot()
        with mock.patch("agent_workboard.lite._artifact_sha",
                        return_value=exact_artifact_sha):
            result = workflow_check(self.database, self.temporary.name)
            replay = workflow_check(self.database, self.temporary.name)
            connection = open_database(self.database)
            try:
                snapshots = {
                    work_item_id: lite_module._kernel_snapshot(
                        connection, work_item_id, self.temporary.name,
                    )
                    for work_item_id, unused_shape in released_shapes
                }
            finally:
                connection.close()
        self.assertEqual(before, self.database_snapshot())
        violations = [row for row in result["results"]
                      if row["status"] != "PASS"]
        self.assertEqual(["AWB-024"],
                         [row["workItemId"] for row in violations])
        self.assertEqual(result, replay)
        self.assertEqual(
            ("DETERMINISTIC", workflow_kernel.RESET_ORPHAN_REVIEWER_TASK),
            (violations[0]["repairability"],
             violations[0]["nextStep"]["action"]),
        )
        for work_item_id, unused_shape in released_shapes:
            self.assertEqual("PASS", next(
                row for row in result["results"]
                if row["workItemId"] == work_item_id
            )["status"])
            self.assertEqual(
                expected_formats[work_item_id],
                snapshots[work_item_id]["reviewEvents"][0]["recordFormat"],
            )

        # This is an independent candidate-native corruption shape.  It must
        # remain a strict b7 violation; the hermetic legacy fixture above is
        # not a runtime exemption for a live claim whose task is not running.
        strict_id = "AWB-B7-STRICT-LIVE-CLAIM"
        self.create(strict_id)
        self.claim(strict_id, 1, strict_id + "-planner", "PLANNER")
        connection = open_database(self.database)
        connection.execute(
            "UPDATE tasks SET status='NOT_STARTED' WHERE task_id=?",
            (strict_id + "-T01",),
        )
        connection.commit()
        connection.close()
        strict = workflow_check(
            self.database, self.temporary.name, strict_id
        )
        self.assertEqual("VIOLATION", strict["status"])
        self.assertIn("LIVE_CLAIM_WITHOUT_IN_PROGRESS_TASK", {
            row["code"] for row in strict["violations"]
        })

    def test_human_recovery_is_exact_audited_and_final_review_can_reclaim_task(self):
        artifact = self.create_awb024_b6_orphan()
        before = self.database_snapshot()
        legacy = recover_review_task(
            self.database, "AWB-024", "AWB-024-T03", "human-a",
            "legacy reset must not write", "legacy-reset",
        )
        self.assertEqual(
            ("REFUSED", "USE_WORKFLOW_CHECK_AND_EXACT_REPAIR"),
            (legacy["status"], legacy["reasonCode"]),
        )
        self.assertEqual(before, self.database_snapshot())
        exact_sha = "c406cffab3dc3ec91853637f08f2d7d80f45632457bc59e9d9e40019af07c09a"
        with mock.patch("agent_workboard.lite._artifact_sha", return_value=exact_sha):
            check = workflow_check(self.database, self.temporary.name, "AWB-024")
            self.assertEqual(("VIOLATION", "DETERMINISTIC"), (
                check["status"], check["repairability"],
            ))
            arguments = check["nextStep"]["arguments"]
            with mock.patch("agent_workboard.lite._workflow_repair_materialized",
                            side_effect=RuntimeError("repair fault")):
                with self.assertRaisesRegex(LiteError, "repair fault"):
                    workflow_repair(
                        self.database, self.temporary.name, "AWB-024",
                        arguments["action"], arguments["fingerprint"],
                        arguments["requestId"], "human-a",
                    )
            self.assertEqual(before, self.database_snapshot())
            repaired = workflow_repair(
                self.database, self.temporary.name, "AWB-024",
                arguments["action"], arguments["fingerprint"],
                arguments["requestId"], "human-a",
            )
            replay = workflow_repair(
                self.database, self.temporary.name, "AWB-024",
                arguments["action"], arguments["fingerprint"],
                arguments["requestId"], "human-a",
            )
            self.assertEqual(("OK", "OK"),
                             (repaired["status"], replay["status"]))
            self.assertEqual(repaired, replay)
            repair_event = timeline(self.database, "AWB-024")[-1]
            self.assertEqual(repaired,
                             json.loads(repair_event["payload_json"])["receipt"])
            self.assertEqual("PASS", workflow_check(
                self.database, self.temporary.name, "AWB-024"
            )["status"])
            set_hold(self.database, "AWB-024", "human-a", True,
                     reason="prove immutable repair replay",
                     request_id="repair-replay-transition")
            post_transition_replay = workflow_repair(
                self.database, self.temporary.name, "AWB-024",
                arguments["action"], arguments["fingerprint"],
                arguments["requestId"], "human-a",
            )
            self.assertEqual(repaired, post_transition_replay)
            with self.assertRaisesRegex(LiteError, "REQUEST_REPLAY_CONFLICT"):
                workflow_repair(
                    self.database, self.temporary.name, "AWB-024",
                    arguments["action"], arguments["fingerprint"],
                    arguments["requestId"], "human-b",
                )
        with open(artifact, encoding="utf-8") as handle:
            self.assertEqual("disposable exact-path fixture\n", handle.read())

    def test_exact_workflow_repair_concurrency_returns_one_immutable_receipt(self):
        self.create_awb024_b6_orphan()
        exact_sha = "c406cffab3dc3ec91853637f08f2d7d80f45632457bc59e9d9e40019af07c09a"
        with mock.patch("agent_workboard.lite._artifact_sha", return_value=exact_sha):
            proof = workflow_check(
                self.database, self.temporary.name, "AWB-024"
            )["nextStep"]["arguments"]
            barrier = threading.Barrier(2)
            receipts = []
            errors = []
            def compete():
                try:
                    barrier.wait()
                    receipts.append(workflow_repair(
                        self.database, self.temporary.name, "AWB-024",
                        proof["action"], proof["fingerprint"],
                        proof["requestId"], "human-a",
                    ))
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=compete) for unused in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual([], errors)
            self.assertEqual(2, len(receipts))
            self.assertEqual(receipts[0], receipts[1])
            self.assertTrue(receipts[0]["toFingerprint"])
            self.assertEqual(1, sum(
                row["event_type"] == "WORKFLOW_REPAIRED"
                for row in timeline(self.database, "AWB-024")
            ))

    def test_plan_deviation_recovery_does_not_unblock_and_public_flow_resumes(self):
        work_item_id = "AWB-DEV"
        self.create_orphaned_review_task(work_item_id, deviation=True)
        before = self.database_snapshot()
        recovered = recover_review_task(
            self.database, work_item_id, work_item_id + "-T03", "human-a",
            "generic deviation reset is forbidden", "recover-deviation",
        )
        self.assertEqual("USE_WORKFLOW_CHECK_AND_EXACT_REPAIR",
                         recovered["reasonCode"])
        self.assertEqual(before, self.database_snapshot())

    def test_recovery_replay_conflict_second_request_and_active_claim_are_zero_write(self):
        work_item_id = "AWB-IDEM"
        self.create_orphaned_review_task(work_item_id)
        before = self.database_snapshot()
        arguments = (
            self.database, work_item_id, work_item_id + "-T03", "human-a",
            "legacy reset is closed", "recover-idempotent",
        )
        first = recover_review_task(*arguments)
        replay = recover_review_task(*arguments)
        conflict = recover_review_task(
            self.database, work_item_id, work_item_id + "-T03", "human-b",
            "different", "recover-idempotent",
        )
        self.assertEqual(["REFUSED", "REFUSED", "REFUSED"],
                         [first["status"], replay["status"], conflict["status"]])
        self.assertEqual(before, self.database_snapshot())

    def test_recovery_rejects_invalid_input_writer_ambiguity_and_evidence_drift_zero_write(self):
        before = self.database_snapshot()
        invalid = recover_review_task(
            self.database, "AWB-X", "AWB-X-T03", " ", "reason", "request",
        )
        self.assertEqual(("REFUSED", "USE_WORKFLOW_CHECK_AND_EXACT_REPAIR"), (
            invalid["status"], invalid["reasonCode"],
        ))
        self.assertEqual(before, self.database_snapshot())

    def test_recovery_fault_rolls_back_and_concurrent_requests_have_one_winner(self):
        work_item_id = "AWB-FAULT"
        self.create_orphaned_review_task(work_item_id)
        before = self.database_snapshot()
        with mock.patch("agent_workboard.lite._workflow_repair_materialized",
                        side_effect=RuntimeError("must remain unused")):
            refused = recover_review_task(
                self.database, work_item_id, work_item_id + "-T03", "human-a",
                "closed legacy surface", "recover-fault",
            )
        self.assertEqual("REFUSED", refused["status"])
        self.assertEqual(before, self.database_snapshot())

    def test_recovery_cli_exit_json_and_transfer_exact_replay(self):
        work_item_id = "AWB-CLI-REC"
        self.create_orphaned_review_task(work_item_id)
        before = self.database_snapshot()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "--database", self.database, "recover-review-task", work_item_id,
                work_item_id + "-T03", "--human", "human-a", "--reason",
                "public CLI", "--request-id", "recover-cli",
            ])
        payload = json.loads(output.getvalue())
        self.assertEqual((2, "REFUSED", "USE_WORKFLOW_CHECK_AND_EXACT_REPAIR"),
                         (code, payload["status"], payload["reasonCode"]))
        self.assertEqual(before, self.database_snapshot())

    def cli(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--database", self.database] + list(arguments))
        self.assertEqual(0, code, output.getvalue())
        return json.loads(output.getvalue())

    def test_cli_write_commands_complete_standard_workflow(self):
        management_file = self.json_file("fe-management.json", self.management("FE-99", item_type="FE"))
        risk_file = self.json_file("fe-risk.json", {
            "protocolVersion": "AWB-CREATION-RISK-v1", "signals": []
        })
        self.cli("create", "FE-99", "--type", "FE", "--title", "CLI trial",
                 "--management-file", management_file, "--risk-file", risk_file,
                 "--human-review", "manual")
        self.cli("claim", "FE-99", "FE-99-T01", "--agent", "planner", "--role", "PLANNER")
        self.cli("lock", "FE-99", "--repository", "repo", "--agent", "planner")
        self.cli("unlock", "FE-99", "--repository", "repo", "--agent", "planner")
        self.cli("task", "FE-99", "FE-99-T01", "--agent", "planner", "--status", "IN_PROGRESS")
        self.cli("transition", "FE-99", "submit_plan", "--agent", "planner")
        self.cli("claim", "FE-99", "FE-99-T03", "--agent", "reviewer", "--role", "REVIEWER")
        self.cli("review", "FE-99", "--stage", "PLAN", "--agent", "reviewer", "--decision", "APPROVED", "--summary", "ok")
        self.cli("gate", "FE-99", "--stage", "PLAN", "--human", "human", "--decision", "APPROVED", "--reason", "ok")
        self.cli("claim", "FE-99", "FE-99-T02", "--agent", "implementer", "--role", "IMPLEMENTER")
        self.cli("transition", "FE-99", "start_implementation", "--agent", "implementer")
        self.cli("task", "FE-99", "FE-99-T02", "--agent", "implementer", "--status", "IN_PROGRESS")
        receipt_id = self.verify_receipt("FE-99", "implementer")
        self.cli("transition", "FE-99", "submit_implementation", "--agent", "implementer",
                 "--verify-receipt", receipt_id)
        self.cli("claim", "FE-99", "FE-99-T03", "--agent", "reviewer", "--role", "REVIEWER")
        final_review = self.json_file(
            "fe-final-review.json", self.structured_review("IMPLEMENTATION", "PASS")
        )
        self.cli("review", "FE-99", "--stage", "FINAL", "--agent", "reviewer",
                 "--decision", "APPROVED", "--review-file", final_review)
        done = self.cli("gate", "FE-99", "--stage", "FINAL", "--human", "human", "--decision", "APPROVED", "--reason", "ok")
        self.assertEqual("FINAL_ACCEPTANCE_APPROVED", done["state"])
        self.assertGreater(len(self.cli("timeline", "FE-99")), 10)

    def test_cli_block_release_unblock_and_reclaim(self):
        management_file = self.json_file("ti-management.json", self.management("TI-88"))
        risk_file = self.json_file("ti-risk.json", {
            "protocolVersion": "AWB-CREATION-RISK-v1", "signals": []
        })
        self.cli("create", "TI-88", "--type", "TI", "--title", "blocked trial",
                 "--management-file", management_file, "--risk-file", risk_file)
        self.cli("claim", "TI-88", "TI-88-T01", "--agent", "planner", "--role", "PLANNER")
        self.cli("task", "TI-88", "TI-88-T01", "--agent", "planner", "--status", "IN_PROGRESS")
        self.cli("task", "TI-88", "TI-88-T01", "--agent", "planner", "--status", "BLOCKED", "--evidence", "waiting")
        item = self.cli("unblock", "TI-88", "TI-88-T01", "--human", "human", "--reason", "ready")
        self.assertEqual("AWB-MUTATION-RECEIPT-v1", item["protocolVersion"])
        self.assertEqual("CLAIMABLE", item["queueState"])
        self.cli("claim", "TI-88", "TI-88-T01", "--agent", "planner-2", "--role", "PLANNER")

    def test_workflow_advance_begin_implementation_is_atomic_and_replay_safe(self):
        self.through_plan_approval("AWB-ADV")
        before = workflow_status(
            self.database, "AWB-ADV", "implementer", "IMPLEMENTER", "repo"
        )
        self.assertEqual("READY", before["status"])
        self.assertEqual("BEGIN_IMPLEMENTATION", before["nextStep"]["action"])
        result = workflow_advance(
            self.database, self.temporary.name, "AWB-ADV", "implementer",
            "IMPLEMENTER", "repo", before["nextStep"]["fingerprint"],
            before["rowVersion"], "advance-begin-1",
        )
        self.assertEqual("AWB-MUTATION-RECEIPT-v1", result["protocolVersion"])
        self.assertEqual("IMPLEMENTING", result["state"])
        item = get_work_item(self.database, "AWB-ADV")
        self.assertEqual("IN_PROGRESS", item["tasks"][1]["status"])
        self.assertEqual("implementer", item["activeClaim"]["agent_id"])
        snapshot = self.database_snapshot()
        replay = workflow_advance(
            self.database, self.temporary.name, "AWB-ADV", "implementer",
            "IMPLEMENTER", "repo", before["nextStep"]["fingerprint"],
            before["rowVersion"], "advance-begin-1",
        )
        self.assertEqual(result, replay)
        self.assertEqual(snapshot, self.database_snapshot())
        with self.assertRaisesRegex(LiteError, "REQUEST_REPLAY_CONFLICT"):
            workflow_advance(
                self.database, self.temporary.name, "AWB-ADV", "implementer",
                "IMPLEMENTER", "repo", before["nextStep"]["fingerprint"],
                before["rowVersion"], "advance-begin-1", ttl=901,
            )

    def test_workflow_advance_refuses_human_step_without_write(self):
        self.create("AWB-HUMAN")
        self.claim("AWB-HUMAN", 1, "planner", "PLANNER")
        self.complete("AWB-HUMAN", 1, "planner")
        transition(self.database, "AWB-HUMAN", "submit_plan", "planner")
        self.claim("AWB-HUMAN", 3, "reviewer", "REVIEWER")
        record_agent_review(self.database, "AWB-HUMAN", "PLAN", "reviewer",
                            "APPROVED", "pass")
        status = workflow_status(
            self.database, "AWB-HUMAN", "implementer", "IMPLEMENTER", "repo"
        )
        self.assertEqual("WAITING_HUMAN", status["status"])
        self.assertEqual("HUMAN", status["nextStep"]["riskClass"])
        snapshot = self.database_snapshot()
        refused = workflow_advance(
            self.database, self.temporary.name, "AWB-HUMAN", "implementer",
            "IMPLEMENTER", "repo", status["nextStep"]["fingerprint"],
            status["rowVersion"], "advance-human-refused",
        )
        self.assertEqual("REFUSED", refused["status"])
        self.assertEqual(snapshot, self.database_snapshot())

    def test_workflow_advance_all_eight_local_bundles_preserve_manual_gates(self):
        work_item_id = "AWB-EIGHT"
        self.create(work_item_id)

        def advance(agent, role, request_id, **inputs):
            status = workflow_status(
                self.database, work_item_id, agent, role, "repo",
                project_root=self.temporary.name,
            )
            self.assertEqual("READY", status["status"])
            return workflow_advance(
                self.database, self.temporary.name, work_item_id, agent, role,
                "repo", status["nextStep"]["fingerprint"],
                status["rowVersion"], request_id, **inputs
            )

        self.assertEqual("BEGIN_PLANNING", advance(
            "planner", "PLANNER", "eight-begin-plan"
        )["operation"])
        with open(os.path.join(self.temporary.name, "plan.md"), "w",
                  encoding="utf-8") as handle:
            handle.write("# exact plan\n")
        self.assertEqual("SUBMIT_PLAN", advance(
            "planner", "PLANNER", "eight-submit-plan",
            plan_artifact="plan.md",
        )["operation"])
        self.assertEqual("BEGIN_PLAN_REVIEW", advance(
            "plan-reviewer", "REVIEWER", "eight-begin-plan-review"
        )["operation"])
        plan_head = json.loads(next(
            row["payload_json"] for row in reversed(timeline(self.database, work_item_id))
            if row["event_type"] == "PLAN_ARTIFACT_HEAD"
        ))
        plan_review_input = self.structured_review("PLAN", "PASS")
        plan_review_input["reviewedArtifact"] = {
            key: plan_head[key] for key in
            ("path", "revision", "sha256", "editorAgentId")
        }
        with open(os.path.join(self.temporary.name, "plan-review.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(plan_review_input, handle)
        plan_review = advance(
            "plan-reviewer", "REVIEWER", "eight-submit-plan-review",
            review_file="plan-review.json", decision="APPROVED",
        )
        self.assertEqual(("SUBMIT_PLAN_REVIEW", "WAITING_HUMAN"), (
            plan_review["operation"], plan_review["queueState"],
        ))
        self.assertEqual("NOT_STARTED", get_work_item(
            self.database, work_item_id
        )["tasks"][2]["status"])
        record_human_gate(
            self.database, work_item_id, "PLAN", "human", "APPROVED", "approved"
        )
        self.assertEqual("BEGIN_IMPLEMENTATION", advance(
            "implementer", "IMPLEMENTER", "eight-begin-implementation"
        )["operation"])
        receipt_id = self.verify_receipt(work_item_id, "implementer")
        self.assertEqual("SUBMIT_IMPLEMENTATION", advance(
            "implementer", "IMPLEMENTER", "eight-submit-implementation",
            verify_receipt=receipt_id,
        )["operation"])
        self.assertEqual("BEGIN_IMPLEMENTATION_REVIEW", advance(
            "implementation-reviewer", "REVIEWER", "eight-begin-final-review"
        )["operation"])
        with open(os.path.join(self.temporary.name, "final-review.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(self.structured_review("IMPLEMENTATION", "PASS"), handle)
        final_review = advance(
            "implementation-reviewer", "REVIEWER", "eight-submit-final-review",
            review_file="final-review.json", decision="APPROVED",
        )
        self.assertEqual(("SUBMIT_IMPLEMENTATION_REVIEW", "WAITING_HUMAN"), (
            final_review["operation"], final_review["queueState"],
        ))
        self.assertEqual("COMPLETED", get_work_item(
            self.database, work_item_id
        )["tasks"][2]["status"])

    def test_workflow_advance_fault_rolls_back_the_entire_bundle(self):
        self.through_plan_approval("AWB-ADV-FAULT")
        status = workflow_status(
            self.database, "AWB-ADV-FAULT", "implementer", "IMPLEMENTER", "repo"
        )
        before = self.database_snapshot()
        with mock.patch("agent_workboard.lite._workflow_materialized",
                        side_effect=RuntimeError("workflow commit fault")):
            with self.assertRaisesRegex(RuntimeError, "workflow commit fault"):
                workflow_advance(
                    self.database, self.temporary.name, "AWB-ADV-FAULT",
                    "implementer", "IMPLEMENTER", "repo",
                    status["nextStep"]["fingerprint"], status["rowVersion"],
                    "advance-fault",
                )
        self.assertEqual(before, self.database_snapshot())

    def test_workflow_advance_concurrency_has_one_atomic_winner(self):
        self.through_plan_approval("AWB-ADV-RACE")
        status = workflow_status(
            self.database, "AWB-ADV-RACE", "implementer", "IMPLEMENTER", "repo"
        )
        results = []
        lock = threading.Lock()

        def contender(request_id):
            try:
                value = workflow_advance(
                    self.database, self.temporary.name, "AWB-ADV-RACE",
                    "implementer", "IMPLEMENTER", "repo",
                    status["nextStep"]["fingerprint"], status["rowVersion"],
                    request_id,
                )
            except Exception as exc:
                value = exc
            with lock:
                results.append(value)

        threads = [threading.Thread(target=contender, args=(request_id,))
                   for request_id in ("advance-race-a", "advance-race-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, sum(isinstance(value, dict) and
                                value.get("status") == "OK" for value in results))
        self.assertEqual(1, sum(isinstance(value, LiteError) for value in results))
        item = get_work_item(self.database, "AWB-ADV-RACE")
        self.assertEqual(("IMPLEMENTING", "implementer"), (
            item["state"], item["activeClaim"]["agent_id"],
        ))
        connection = open_database(self.database)
        try:
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM repository_locks WHERE work_item_id=? "
                "AND status='ACTIVE'", ("AWB-ADV-RACE",),
            ).fetchone()[0])
        finally:
            connection.close()

    def test_cli_mutation_receipt_is_compact_and_full_is_compatible(self):
        management_file = self.json_file("receipt-management.json", self.management("TI-RCPT"))
        risk_file = self.json_file("receipt-risk.json", {
            "protocolVersion": "AWB-CREATION-RISK-v1", "signals": []
        })
        receipt = self.cli(
            "create", "TI-RCPT", "--type", "TI", "--title", "receipt",
            "--management-file", management_file, "--risk-file", risk_file,
            "--request-id", "receipt-create",
        )
        self.assertEqual("AWB-MUTATION-RECEIPT-v1", receipt["protocolVersion"])
        self.assertNotIn("statusHistory", receipt)
        full = self.cli("claim", "TI-RCPT", "TI-RCPT-T01", "--agent", "planner",
                        "--role", "PLANNER", "--request-id", "receipt-claim", "--full")
        self.assertEqual("TI-RCPT", full["work_item_id"])
        self.assertIn("statusHistory", full)

    def test_lite_http_board_reads_lite_database_and_is_read_only(self):
        self.create("TI-001")
        server = make_server(self.database, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            base = "http://127.0.0.1:{0}".format(server.server_address[1])
            with urlopen(base + "/") as response:
                board = response.read().decode("utf-8")
            self.assertIn("MVP-LITE-v1", board)
            self.assertIn("TI-001", board)
            with urlopen(base + "/v1/work-items") as response:
                items = json.loads(response.read().decode("utf-8"))
            self.assertEqual(["TI-001"], [item["work_item_id"] for item in items])
            with urlopen(base + "/v1/work-items/TI-001") as response:
                detail = json.loads(response.read().decode("utf-8"))
            self.assertEqual(3, len(detail["tasks"]))
            with self.assertRaises(HTTPError) as raised:
                urlopen(Request(base + "/v1/work-items", data=b"{}", method="POST"))
            self.assertEqual(405, raised.exception.code)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_lite_http_rejects_non_loopback_listener(self):
        with self.assertRaises(LiteError):
            make_server(self.database, "0.0.0.0", 0)


if __name__ == "__main__":
    unittest.main()
