import datetime
import io
import json
import os
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
    create_work_item,
    get_work_item,
    initialize_database,
    list_work_items,
    main,
    make_server,
    open_database,
    record_agent_review,
    record_human_gate,
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

    def create(self, work_item_id="TI-001", mode="STANDARD", priority="P2"):
        return create_work_item(self.database, work_item_id, work_item_id.split("-")[0], "测试任务", mode, priority)

    def claim(self, work_item_id, seq, agent, role):
        return acquire_claim(
            self.database, work_item_id, "{0}-T{1:02d}".format(work_item_id, seq),
            agent, role, self.expires()
        )

    def complete(self, work_item_id, seq, agent):
        set_task_status(
            self.database, work_item_id, "{0}-T{1:02d}".format(work_item_id, seq),
            agent, "COMPLETED", [{"result": "passed"}]
        )

    def through_plan_approval(self, work_item_id="TI-001"):
        self.create(work_item_id)
        self.claim(work_item_id, 1, "planner-a", "PLANNER")
        self.complete(work_item_id, 1, "planner-a")
        transition(self.database, work_item_id, "submit_plan", "planner-a")
        self.claim(work_item_id, 3, "reviewer-a", "REVIEWER")
        record_agent_review(self.database, work_item_id, "PLAN", "reviewer-a", "APPROVED", "可实施")
        record_human_gate(self.database, work_item_id, "PLAN", "human-a", "APPROVED", "批准")

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
            item = create_work_item(self.database, work_item_id, item_type, "item")
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
            self.database, "TI-001", "TI", "first", request_id="same-request"
        )
        with self.assertRaises(LiteError):
            create_work_item(
                self.database, "TI-001", "TI", "changed", request_id="same-request"
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
        set_task_status(self.database, "TI-001", "TI-001-T01", "planner", "BLOCKED")
        release_claim(self.database, "TI-001", "planner")
        self.assertEqual("BLOCKED", get_work_item(self.database, "TI-001")["queue_state"])
        with self.assertRaises(LiteError):
            self.claim("TI-001", 1, "planner-2", "PLANNER")
        item = unblock_task(self.database, "TI-001", "TI-001-T01", "human", "dependency ready")
        self.assertEqual(("CLAIMABLE", "NOT_STARTED"), (item["queue_state"], item["tasks"][0]["status"]))
        self.claim("TI-001", 1, "planner-2", "PLANNER")

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
        item = record_agent_review(self.database, "TI-001", "PLAN", "reviewer", "REJECTED", "revise")
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
            transition(self.database, "TI-001", "submit_implementation", "implementer", local_tests_passed=True)
        self.complete("TI-001", 2, "implementer")
        with self.assertRaises(LiteError):
            transition(self.database, "TI-001", "submit_implementation", "implementer", local_tests_passed=False)
        item = transition(self.database, "TI-001", "submit_implementation", "implementer", local_tests_passed=True)
        self.assertEqual("IMPLEMENTATION_COMPLETED", item["state"])

    def test_standard_public_api_end_to_end(self):
        self.through_plan_approval()
        self.claim("TI-001", 2, "implementer", "IMPLEMENTER")
        transition(self.database, "TI-001", "start_implementation", "implementer")
        self.complete("TI-001", 2, "implementer")
        transition(self.database, "TI-001", "submit_implementation", "implementer", local_tests_passed=True)
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
        transition(self.database, "TI-001", "submit_implementation", implementer, local_tests_passed=True)

    def test_final_agent_rejection_returns_implementing(self):
        self._through_implementation_submission()
        self.claim("TI-001", 3, "reviewer", "REVIEWER")
        item = record_agent_review(self.database, "TI-001", "FINAL", "reviewer", "REJECTED", "fix")
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

    def cli(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--database", self.database] + list(arguments))
        self.assertEqual(0, code, output.getvalue())
        return json.loads(output.getvalue())

    def test_cli_write_commands_complete_standard_workflow(self):
        self.cli("create", "FE-99", "--type", "FE", "--title", "CLI trial")
        self.cli("claim", "FE-99", "FE-99-T01", "--agent", "planner", "--role", "PLANNER")
        self.cli("lock", "FE-99", "--repository", "repo", "--agent", "planner")
        self.cli("unlock", "FE-99", "--repository", "repo", "--agent", "planner")
        self.cli("task", "FE-99", "FE-99-T01", "--agent", "planner", "--status", "COMPLETED", "--evidence", "plan ready")
        self.cli("transition", "FE-99", "submit_plan", "--agent", "planner")
        self.cli("claim", "FE-99", "FE-99-T03", "--agent", "reviewer", "--role", "REVIEWER")
        self.cli("review", "FE-99", "--stage", "PLAN", "--agent", "reviewer", "--decision", "APPROVED", "--summary", "ok")
        self.cli("gate", "FE-99", "--stage", "PLAN", "--human", "human", "--decision", "APPROVED", "--reason", "ok")
        self.cli("claim", "FE-99", "FE-99-T02", "--agent", "implementer", "--role", "IMPLEMENTER")
        self.cli("transition", "FE-99", "start_implementation", "--agent", "implementer")
        self.cli("task", "FE-99", "FE-99-T02", "--agent", "implementer", "--status", "COMPLETED", "--evidence", "tests passed")
        self.cli("transition", "FE-99", "submit_implementation", "--agent", "implementer", "--local-tests-passed")
        self.cli("claim", "FE-99", "FE-99-T03", "--agent", "reviewer", "--role", "REVIEWER")
        self.cli("review", "FE-99", "--stage", "FINAL", "--agent", "reviewer", "--decision", "APPROVED", "--summary", "ok")
        done = self.cli("gate", "FE-99", "--stage", "FINAL", "--human", "human", "--decision", "APPROVED", "--reason", "ok")
        self.assertEqual("FINAL_ACCEPTANCE_APPROVED", done["state"])
        self.assertGreater(len(self.cli("timeline", "FE-99")), 10)

    def test_cli_block_release_unblock_and_reclaim(self):
        self.cli("create", "TI-88", "--type", "TI", "--title", "blocked trial")
        self.cli("claim", "TI-88", "TI-88-T01", "--agent", "planner", "--role", "PLANNER")
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
