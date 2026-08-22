import datetime
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_workboard.lite import (
    LiteError,
    acquire_claim,
    acquire_repository_lock,
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
    report_plan_deviation,
    release_claim,
    release_repository_lock,
    set_hold,
    set_task_status,
    timeline,
    transition,
    unblock_task,
)


class LiteWorkboardTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.temporary.name, "workboard.db")
        initialize_database(self.database)

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
        if task["status"] == "NOT_STARTED":
            set_task_status(
                self.database, work_item_id, task["task_id"], agent, "IN_PROGRESS",
                [{"started": True}],
            )
        set_task_status(
            self.database, work_item_id, "{0}-T{1:02d}".format(work_item_id, seq),
            agent, "COMPLETED", [{"result": "passed"}]
        )

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

    def revise_review(self, stage, finding_id):
        return {
            "result": "REVISE", "reviewerMode": "ORDINARY", "summary": "fix required",
            "findings": [{
                "id": finding_id, "stage": stage, "violatedContract": "AC-001",
                "evidence": {"test": "deterministic failure"}, "impact": "acceptance fails",
                "closeCondition": "test passes", "origin": "INITIAL",
                "priorUnavailableReason": "not applicable",
            }],
            "resolvedFindingIds": [], "nonBlockingSuggestions": [],
        }

    def structured_review(self, stage, result, mode="ORDINARY", findings=None,
                          resolved=None, suggestions=None):
        return {
            "result": result, "reviewerMode": mode, "summary": result,
            "findings": findings or [], "resolvedFindingIds": resolved or [],
            "nonBlockingSuggestions": suggestions or [],
        }

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
                    submission={"addressedFindingIds": ["PLAN-F001"], "complexityChanges": []},
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
            submission={"addressedFindingIds": ["PLAN-F001"], "complexityChanges": []},
        )

    def test_clean_init_and_repeat_init_rejected(self):
        connection = open_database(self.database)
        self.assertEqual("MVP-LITE-v1", connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0])
        connection.close()
        with self.assertRaises(LiteError):
            initialize_database(self.database)

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
        with self.assertRaises(LiteError):
            set_task_status(self.database, "TI-901", "TI-901-T02", "planner-2", "IN_PROGRESS")
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
            submission={"addressedFindingIds": ["PLAN-F001"], "complexityChanges": []},
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
            submission={"addressedFindingIds": ["PLAN-F001"], "complexityChanges": []},
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
            local_tests_passed=True,
            quality_baseline=self.quality(addressed=["IMPLEMENTATION-F001"]),
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
        with self.assertRaises(LiteError):
            transition(self.database, "TI-001", "submit_plan", "planner")
        release_repository_lock(self.database, "TI-001", "repo", "planner")
        transition(self.database, "TI-001", "submit_plan", "planner")

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
        release_claim(self.database, "TI-001", "planner")
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

    def test_plan_submit_requires_completed_planning_task(self):
        self.create()
        self.claim("TI-001", 1, "planner", "PLANNER")
        with self.assertRaises(LiteError):
            transition(self.database, "TI-001", "submit_plan", "planner")
        self.complete("TI-001", 1, "planner")
        item = transition(self.database, "TI-001", "submit_plan", "planner")
        self.assertEqual("PLAN_REVIEW_PENDING", item["state"])

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
            local_tests_passed=True, quality_baseline=self.quality(),
        )
        self.claim("TI-030", 3, "final-reviewer", "REVIEWER")
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

    def test_auto_final_quality_drift_fails_closed_without_auto_event(self):
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
            local_tests_passed=True, quality_baseline=self.quality(),
        )
        connection = open_database(self.database)
        row = connection.execute(
            "SELECT event_id,payload_json FROM events WHERE work_item_id='TI-031' "
            "AND event_type='SUBMIT_IMPLEMENTATION'"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload["qualityBaseline"]["tests"][0]["result"] = "FAIL"
        connection.execute(
            "UPDATE events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), row["event_id"]),
        )
        connection.commit()
        connection.close()
        self.claim("TI-031", 3, "final-reviewer", "REVIEWER")
        reviewed = record_agent_review(
            self.database, "TI-031", "FINAL", "final-reviewer", "APPROVED",
            self.structured_review("IMPLEMENTATION", "PASS"),
        )
        self.assertEqual(("IMPLEMENTATION_COMPLETED", "WAITING_HUMAN"), (
            reviewed["state"], reviewed["queue_state"]
        ))
        events = timeline(self.database, "TI-031")
        self.assertEqual(1, sum(row["event_type"] == "AUTO_GATE_APPROVED" for row in events))
        final_review = [row for row in events if row["event_type"] == "AGENT_FINAL_REVIEW"][0]
        self.assertEqual("FAIL_CLOSED", json.loads(final_review["payload_json"])["autoGate"]["status"])

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
                local_tests_passed=True, quality_baseline=self.quality(),
            )
        self.complete("TI-001", 2, "implementer")
        with self.assertRaises(LiteError):
            transition(
                self.database, "TI-001", "submit_implementation", "implementer",
                local_tests_passed=False, quality_baseline=self.quality(),
            )
        item = transition(
            self.database, "TI-001", "submit_implementation", "implementer",
            local_tests_passed=True, quality_baseline=self.quality(),
        )
        self.assertEqual("IMPLEMENTATION_COMPLETED", item["state"])

    def test_standard_public_api_end_to_end(self):
        self.through_plan_approval()
        self.claim("TI-001", 2, "implementer", "IMPLEMENTER")
        transition(self.database, "TI-001", "start_implementation", "implementer")
        self.complete("TI-001", 2, "implementer")
        transition(
            self.database, "TI-001", "submit_implementation", "implementer",
            local_tests_passed=True, quality_baseline=self.quality(),
        )
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        reviewed = record_agent_review(self.database, "TI-001", "FINAL", "reviewer", "APPROVED", "accepted")
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
            local_tests_passed=True, quality_baseline=self.quality(),
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
            record_agent_review(self.database, "TI-001", "FINAL", "same-agent", "APPROVED", "bad")

    def test_human_final_rejection_returns_implementing(self):
        self._through_implementation_submission()
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        record_agent_review(self.database, "TI-001", "FINAL", "reviewer", "APPROVED", "ok")
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
                    submission={"addressedFindingIds": ["PLAN-F001"], "complexityChanges": []},
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
            submission={"addressedFindingIds": ["PLAN-F001"], "complexityChanges": []},
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

    def test_quality_ratchet_rejects_weakened_tests_and_regression(self):
        self.through_plan_approval()
        self.claim("TI-001", 2, "implementer", "IMPLEMENTER")
        transition(self.database, "TI-001", "start_implementation", "implementer")
        self.complete("TI-001", 2, "implementer")
        weakened = self.quality()
        weakened["testsWeakened"] = True
        with self.assertRaises(LiteError):
            transition(
                self.database, "TI-001", "submit_implementation", "implementer",
                local_tests_passed=True, quality_baseline=weakened,
            )
        regressed = self.quality()
        regressed["regressions"] = ["existing behavior failed"]
        with self.assertRaises(LiteError):
            transition(
                self.database, "TI-001", "submit_implementation", "implementer",
                local_tests_passed=True, quality_baseline=regressed,
            )

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
        self.assertEqual(("DRAFT", "BLOCKED", "PLANNER", "PLAN_DEVIATION"), (
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
        quality_file = self.json_file("fe-quality.json", self.quality())
        self.cli("create", "FE-99", "--type", "FE", "--title", "CLI trial",
                 "--management-file", management_file, "--risk-file", risk_file,
                 "--human-review", "manual")
        self.cli("claim", "FE-99", "FE-99-T01", "--agent", "planner", "--role", "PLANNER")
        self.cli("lock", "FE-99", "--repository", "repo", "--agent", "planner")
        self.cli("unlock", "FE-99", "--repository", "repo", "--agent", "planner")
        self.cli("task", "FE-99", "FE-99-T01", "--agent", "planner", "--status", "IN_PROGRESS")
        self.cli("task", "FE-99", "FE-99-T01", "--agent", "planner", "--status", "COMPLETED", "--evidence", "plan ready")
        self.cli("transition", "FE-99", "submit_plan", "--agent", "planner")
        self.cli("claim", "FE-99", "FE-99-T03", "--agent", "reviewer", "--role", "REVIEWER")
        self.cli("review", "FE-99", "--stage", "PLAN", "--agent", "reviewer", "--decision", "APPROVED", "--summary", "ok")
        self.cli("gate", "FE-99", "--stage", "PLAN", "--human", "human", "--decision", "APPROVED", "--reason", "ok")
        self.cli("claim", "FE-99", "FE-99-T02", "--agent", "implementer", "--role", "IMPLEMENTER")
        self.cli("transition", "FE-99", "start_implementation", "--agent", "implementer")
        self.cli("task", "FE-99", "FE-99-T02", "--agent", "implementer", "--status", "IN_PROGRESS")
        self.cli("task", "FE-99", "FE-99-T02", "--agent", "implementer", "--status", "COMPLETED", "--evidence", "tests passed")
        self.cli("transition", "FE-99", "submit_implementation", "--agent", "implementer",
                 "--local-tests-passed", "--quality-file", quality_file)
        self.cli("claim", "FE-99", "FE-99-T03", "--agent", "reviewer", "--role", "REVIEWER")
        self.cli("review", "FE-99", "--stage", "FINAL", "--agent", "reviewer", "--decision", "APPROVED", "--summary", "ok")
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
        self.cli("release", "TI-88", "--agent", "planner")
        item = self.cli("unblock", "TI-88", "TI-88-T01", "--human", "human", "--reason", "ready")
        self.assertEqual("CLAIMABLE", item["queue_state"])
        self.cli("claim", "TI-88", "TI-88-T01", "--agent", "planner-2", "--role", "PLANNER")

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
