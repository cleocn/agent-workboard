import datetime
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

from agent_workboard.cli import main as cli_main
from agent_workboard.lite import (LiteError, acquire_claim, acquire_repository_lock,
                                  create_work_item, initialize_database,
                                  open_database, release_claim,
                                  release_repository_lock, set_task_status,
                                  workflow_check)
from agent_workboard.orchestrator import (ORCHESTRATOR_SCHEMA_VERSION, claim,
                                          activity_snapshot, claim_next, list_activity,
                                          list_leases, reconcile_expired, recover,
                                          register, release, renew, schema_state,
                                          show, show_activity)
import agent_workboard.orchestrator as orchestrator_module


class OrchestratorCoordinationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.temporary.name, "workboard.db")
        initialize_database(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def management(self, work_item_id):
        return {
            "contractVersion": "AWB-WORKITEM-MGMT-v1",
            "templateContractVersion": "AWB-MANAGEMENT-v1",
            "scope": ["local coordination"], "outOfScope": ["remote control"],
            "authorization": {"allowed": ["local"], "forbidden": ["remote"]},
            "safetyConstraints": ["one owner"],
            "tasks": [
                {"taskId": work_item_id + "-T01", "seq": 1, "title": "plan",
                 "ownerRole": "PLANNER", "required": True,
                 "acceptance": ["planned"], "closureEvidenceRequired": ["plan"]},
                {"taskId": work_item_id + "-T02", "seq": 2, "title": "implement",
                 "ownerRole": "IMPLEMENTER", "required": True,
                 "acceptance": ["built"], "closureEvidenceRequired": ["tests"]},
                {"taskId": work_item_id + "-T03", "seq": 3, "title": "review",
                 "ownerRole": "REVIEWER", "required": True,
                 "acceptance": ["reviewed"], "closureEvidenceRequired": ["review"]},
            ],
            "acceptance": [{"id": "AC-001", "criterion": "works"}],
            "closure": [{"id": "CL-001", "criterion": "closed"}],
        }

    def create(self, work_item_id, priority="P2"):
        return create_work_item(
            self.database, work_item_id, "AWB", "coordination", priority=priority,
            management=self.management(work_item_id),
        )

    def now(self, seconds=0):
        return (datetime.datetime.now(datetime.timezone.utc) +
                datetime.timedelta(seconds=seconds)).replace(microsecond=0).isoformat()

    def agent_expiry(self):
        return self.now(600)

    def assert_envelope(self, value, operation):
        self.assertEqual(ORCHESTRATOR_SCHEMA_VERSION, value["protocolVersion"])
        self.assertEqual(operation, value["operation"])
        self.assertEqual({"action", "arguments"}, set(value["nextStep"]))
        self.assertTrue(value["nextStep"]["action"])

    def cli(self, arguments):
        output = io.StringIO()
        with mock.patch("agent_workboard.cli._project_database", return_value=self.database), \
                redirect_stdout(output):
            code = cli_main(["orchestrator"] + arguments + ["--project", "."])
        return code, json.loads(output.getvalue())

    def reconcile_checked(self, work_item_id, kind, resource_id, owner,
                          generation):
        check = workflow_check(self.database, self.temporary.name, work_item_id)
        proof = check["nextStep"]["arguments"]
        return reconcile_expired(
            self.database, work_item_id, kind, resource_id, owner, generation,
            proof["requestId"], fingerprint=proof["fingerprint"],
            expected_activity=proof["expectedActivity"],
            not_after=proof["notAfter"],
        )

    def test_register_claim_replay_conflict_and_parallel_work_items(self):
        self.create("AWB-101")
        self.create("AWB-102")
        first_register = register(self.database, "local-a", "register-a")
        self.assertEqual("OK", first_register["status"])
        self.assertEqual(first_register, register(self.database, "local-a", "register-a"))
        first = claim(self.database, "AWB-101", "local-a", 900, "claim-a")
        self.assertEqual("OK", first["status"])
        self.assertEqual(first, claim(self.database, "AWB-101", "local-a", 900, "claim-a"))
        reused = claim(self.database, "AWB-102", "local-a", 900, "claim-a")
        self.assertEqual("REQUEST_ID_REUSED", reused["reasonCode"])
        conflict = claim(self.database, "AWB-101", "local-b", 900, "claim-b")
        self.assertEqual("LEASE_HELD", conflict["reasonCode"])
        second = claim(self.database, "AWB-102", "local-b", 900, "claim-c")
        self.assertEqual("OK", second["status"])
        active = list_leases(self.database, status="ACTIVE")
        self.assertEqual({"AWB-101", "AWB-102"},
                         set(row["work_item_id"] for row in active["leases"]))

    def test_effective_projection_and_exact_reconciliation_are_zero_write_and_idempotent(self):
        self.create("AWB-103")
        stale = claim(self.database, "AWB-103", "old-owner", 10, "claim-old",
                      self.now(-30))["lease"]
        connection = open_database(self.database)
        before = "\n".join(connection.iterdump())
        snapshot = activity_snapshot(connection)
        connection.close()
        self.assertEqual(1, snapshot["counts"]["staleOrchestratorLeases"])
        self.assertEqual(0, snapshot["counts"]["liveOrchestratorLeases"])
        shown = show_activity(self.database, "orchestrator-lease", stale["lease_id"])
        self.assertEqual("STALE", shown["resource"]["effectiveStatus"])
        listed = list_activity(self.database, "AWB-103", "STALE")
        self.assertEqual([stale["lease_id"]],
                         [row["resourceId"] for row in listed["resources"]])
        connection = open_database(self.database)
        self.assertEqual(before, "\n".join(connection.iterdump()))
        connection.close()

        check = workflow_check(self.database, self.temporary.name, "AWB-103")
        proof = check["nextStep"]["arguments"]

        wrong = reconcile_expired(
            self.database, "AWB-103", "orchestrator-lease", stale["lease_id"],
            "wrong-owner", stale["generation"], proof["requestId"],
            fingerprint=proof["fingerprint"],
            expected_activity=proof["expectedActivity"],
            not_after=proof["notAfter"],
        )
        self.assertEqual("WRONG_OWNER", wrong["reasonCode"])
        with mock.patch.object(orchestrator_module, "_activity_materialized",
                               side_effect=RuntimeError("post-apply fault")):
            with self.assertRaisesRegex(RuntimeError, "post-apply fault"):
                reconcile_expired(
                    self.database, "AWB-103", "orchestrator-lease", stale["lease_id"],
                    "old-owner", stale["generation"], proof["requestId"],
                    fingerprint=proof["fingerprint"],
                    expected_activity=proof["expectedActivity"],
                    not_after=proof["notAfter"],
                )
        connection = open_database(self.database)
        self.assertEqual("ACTIVE", connection.execute(
            "SELECT status FROM orchestrator_leases WHERE lease_id=?",
            (stale["lease_id"],),
        ).fetchone()[0])
        connection.close()
        reconciled = reconcile_expired(
            self.database, "AWB-103", "orchestrator-lease", stale["lease_id"],
            "old-owner", stale["generation"], proof["requestId"],
            fingerprint=proof["fingerprint"],
            expected_activity=proof["expectedActivity"],
            not_after=proof["notAfter"],
        )
        self.assertEqual("OK", reconciled["status"])
        replay = reconcile_expired(
            self.database, "AWB-103", "orchestrator-lease", stale["lease_id"],
            "old-owner", stale["generation"], proof["requestId"],
            fingerprint=proof["fingerprint"],
            expected_activity=proof["expectedActivity"],
            not_after=proof["notAfter"],
        )
        self.assertEqual("NO_OP", replay["status"])
        connection = open_database(self.database)
        self.assertEqual(1, connection.execute(
            "SELECT count(*) FROM events WHERE event_type='ACTIVITY_EXPIRED_AND_RECONCILED'"
        ).fetchone()[0])
        connection.close()

    def test_reconciliation_refuses_live_conflict_and_concurrent_mutation_has_one_winner(self):
        self.create("AWB-104")
        acquire_claim(
            self.database, "AWB-104", "AWB-104-T01", "planner", "PLANNER",
            self.agent_expiry(),
        )
        writer = acquire_repository_lock(
            self.database, "AWB-104", "repo", "planner", self.agent_expiry()
        )
        connection = open_database(self.database)
        connection.execute(
            "UPDATE repository_locks SET expires_at=? WHERE lock_id=?",
            (self.now(-30), writer["lockId"]),
        )
        connection.commit()
        connection.close()
        proof = workflow_check(
            self.database, self.temporary.name, "AWB-104"
        )["nextStep"]["arguments"]
        reconciled = reconcile_expired(
            self.database, "AWB-104", "repository-writer", writer["lockId"],
            "planner", writer["generation"], proof["requestId"],
            fingerprint=proof["fingerprint"],
            expected_activity=proof["expectedActivity"],
            not_after=proof["notAfter"],
        )
        self.assertEqual("OK", reconciled["status"])
        self.assertEqual("CLAIMED", self.snapshot("AWB-104")["item"][0])
        release_claim(self.database, "AWB-104", "planner")
        acquire_claim(
            self.database, "AWB-104", "AWB-104-T01", "planner", "PLANNER",
            self.agent_expiry(),
        )
        writer = acquire_repository_lock(
            self.database, "AWB-104", "repo", "planner", self.agent_expiry()
        )
        release_repository_lock(self.database, "AWB-104", "repo", "planner")
        release_claim(self.database, "AWB-104", "planner")
        connection = open_database(self.database)
        connection.execute(
            "UPDATE repository_locks SET status='ACTIVE',released_at=NULL,expires_at=? "
            "WHERE lock_id=?", (self.now(-30), writer["lockId"]),
        )
        connection.commit(); connection.close()
        proof = workflow_check(
            self.database, self.temporary.name, "AWB-104"
        )["nextStep"]["arguments"]
        barrier = threading.Barrier(2)
        results = []
        def compete(suffix):
            barrier.wait()
            results.append(reconcile_expired(
                self.database, "AWB-104", "repository-writer", writer["lockId"],
                "planner", writer["generation"], proof["requestId"],
                fingerprint=proof["fingerprint"],
                expected_activity=proof["expectedActivity"],
                not_after=proof["notAfter"],
            ))
        threads = [threading.Thread(target=compete, args=(value,))
                   for value in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, sum(result["status"] == "OK" for result in results))
        self.assertEqual(1, sum(result["status"] == "NO_OP" for result in results))

    def test_stale_agent_bundle_reconciles_without_releasing_live_orchestrator(self):
        for with_writer in (False, True):
            work_item_id = "AWB-LIVE-ORCH-{0}".format(int(with_writer))
            self.create(work_item_id)
            lease = claim(self.database, work_item_id, "live-orchestrator", 900,
                          "live-orch-{0}".format(int(with_writer)))["lease"]
            agent = acquire_claim(
                self.database, work_item_id, work_item_id + "-T01", "planner",
                "PLANNER", self.agent_expiry(),
                orchestrator_id="live-orchestrator",
                orchestrator_generation=lease["generation"],
            )
            writer = (acquire_repository_lock(
                self.database, work_item_id, "repo-" + work_item_id,
                "planner", self.agent_expiry()) if with_writer else None)
            connection = open_database(self.database)
            connection.execute(
                "UPDATE claims SET expires_at=? WHERE claim_id=?",
                (self.now(-30), agent["claimId"]),
            )
            if writer is not None:
                connection.execute(
                    "UPDATE repository_locks SET expires_at=? WHERE lock_id=?",
                    (self.now(-30), writer["lockId"]),
                )
            connection.commit(); connection.close()
            check = workflow_check(self.database, self.temporary.name, work_item_id)
            self.assertEqual("VIOLATION", check["status"])
            proof = check["nextStep"]["arguments"]
            expected_kinds = {row["kind"] for row in proof["expectedActivity"]}
            self.assertEqual(
                ({"AGENT_CLAIM", "REPOSITORY_WRITER"} if with_writer else
                 {"AGENT_CLAIM"}), expected_kinds,
            )
            self.assertNotIn("ORCHESTRATOR_LEASE", expected_kinds)
            def reconcile_live_shape():
                return reconcile_expired(
                    self.database, work_item_id, "claim", agent["claimId"],
                    "planner", agent["generation"], proof["requestId"],
                    fingerprint=proof["fingerprint"],
                    expected_activity=proof["expectedActivity"],
                    not_after=proof["notAfter"],
                )
            if with_writer:
                barrier = threading.Barrier(2)
                results = []
                def compete():
                    barrier.wait(); results.append(reconcile_live_shape())
                threads = [threading.Thread(target=compete) for unused in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                self.assertEqual(["NO_OP", "OK"],
                                 sorted(row["status"] for row in results))
            else:
                self.assertEqual("OK", reconcile_live_shape()["status"])
            live = show(self.database, work_item_id)
            projected = show_activity(
                self.database, "orchestrator-lease", lease["lease_id"])
            self.assertEqual(("ACTIVE", "LIVE"),
                             (live["lease"]["status"],
                              projected["resource"]["effectiveStatus"]))
            replay = reconcile_live_shape()
            self.assertEqual("NO_OP", replay["status"])

    def test_claim_next_stable_order_and_concurrent_single_winner(self):
        self.create("AWB-202", "P1")
        self.create("AWB-201", "P0")
        selected = claim_next(self.database, "queue-a", 900, "next-a")
        self.assertEqual("AWB-201", selected["lease"]["work_item_id"])
        selected = claim_next(self.database, "queue-b", 900, "next-b")
        self.assertEqual("AWB-202", selected["lease"]["work_item_id"])

        self.create("AWB-203", "P0")
        barrier = threading.Barrier(2)
        results = []

        def compete(name):
            barrier.wait()
            results.append(claim(self.database, "AWB-203", name, 900,
                                 "race-" + name))

        threads = [threading.Thread(target=compete, args=(name,))
                   for name in ("race-a", "race-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, sum(value["status"] == "OK" for value in results))
        self.assertEqual(1, sum(value["reasonCode"] == "LEASE_HELD" for value in results))

        self.create("AWB-204", "P1")
        self.create("AWB-205", "P1")
        barrier = threading.Barrier(2)
        results = []

        def next_compete(name):
            barrier.wait()
            results.append(claim_next(self.database, name, 900, "next-" + name))

        threads = [threading.Thread(target=next_compete, args=(name,))
                   for name in ("next-c", "next-d")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual({"AWB-204", "AWB-205"}, set(
            value["lease"]["work_item_id"] for value in results
        ))

    def test_claim_ineligible_is_structured_zero_write_and_claim_next_skips_it(self):
        self.create("AWB-206", "P0")
        self.create("AWB-207", "P1")
        connection = open_database(self.database)
        connection.execute(
            "UPDATE work_items SET queue_state='WAITING_HUMAN',current_role=NULL "
            "WHERE work_item_id='AWB-206'"
        )
        connection.commit()
        before = "\n".join(connection.iterdump())
        direct = orchestrator_module._eligible(
            connection, "AWB-206", self.now()
        )
        self.assertEqual(3, len(direct))
        self.assertEqual(("AWB-206", "NOT_ELIGIBLE", []),
                         (direct[0]["work_item_id"], direct[1], direct[2]))
        connection.close()

        refused = claim(
            self.database, "AWB-206", "ineligible-owner", 900,
            "ineligible-direct",
        )
        self.assertEqual(("AWB-ORCHESTRATOR-v1", "REFUSED", "NOT_ELIGIBLE"), (
            refused["protocolVersion"], refused["status"], refused["reasonCode"],
        ))
        self.assertEqual({"action", "arguments"}, set(refused["nextStep"]))
        connection = open_database(self.database)
        self.assertEqual(before, "\n".join(connection.iterdump()))
        connection.close()

        code, value = self.cli([
            "claim", "AWB-206", "--orchestrator", "cli-ineligible",
            "--ttl", "30", "--request-id", "cli-ineligible-claim",
        ])
        self.assertEqual(2, code)
        self.assertEqual(("REFUSED", "NOT_ELIGIBLE"),
                         (value["status"], value["reasonCode"]))
        self.assertEqual({"action", "arguments"}, set(value["nextStep"]))
        connection = open_database(self.database)
        self.assertEqual(before, "\n".join(connection.iterdump()))
        connection.close()

        selected = claim_next(
            self.database, "next-eligible", 900, "next-skips-ineligible"
        )
        self.assertEqual("AWB-207", selected["lease"]["work_item_id"])
        release(
            self.database, "AWB-207", "next-eligible",
            selected["lease"]["generation"], "release-next-eligible",
        )
        connection = open_database(self.database)
        connection.execute(
            "UPDATE work_items SET queue_state='WAITING_HUMAN',current_role=NULL "
            "WHERE work_item_id='AWB-207'"
        )
        connection.commit()
        connection.close()
        no_candidate = claim_next(
            self.database, "next-none", 900, "next-all-ineligible"
        )
        self.assertEqual(("NO_OP", "NO_CANDIDATE"),
                         (no_candidate["status"], no_candidate["reasonCode"]))
        self.assertEqual({"action", "arguments"}, set(no_candidate["nextStep"]))

    def test_renew_release_recover_fencing_and_inflight_agent_independence(self):
        self.create("AWB-301")
        current = self.now()
        held = claim(self.database, "AWB-301", "owner-a", 2, "claim-301", current)
        generation = held["lease"]["generation"]
        agent = acquire_claim(
            self.database, "AWB-301", "AWB-301-T01", "planner", "PLANNER",
            self.agent_expiry(), orchestrator_id="owner-a",
            orchestrator_generation=generation,
        )
        renewed = renew(self.database, "AWB-301", "owner-a", generation, 30,
                        "renew-301", current)
        self.assertEqual("OK", renewed["status"])
        released = release(self.database, "AWB-301", "owner-a", generation,
                           "release-301", current)
        self.assertEqual("OK", released["status"])
        # Existing Agent work remains authorized after the coordinator leaves.
        set_task_status(self.database, "AWB-301", "AWB-301-T01", "planner",
                        "IN_PROGRESS", ["started"])
        release_claim(self.database, "AWB-301", "planner")
        before = self.snapshot("AWB-301")
        with self.assertRaisesRegex(LiteError, "current orchestrator fence"):
            acquire_claim(self.database, "AWB-301", "AWB-301-T01", "stale",
                          "PLANNER", self.agent_expiry())
        self.assertEqual(before, self.snapshot("AWB-301"))
        stale = renew(self.database, "AWB-301", "owner-a", generation, 30,
                      "renew-stale")
        self.assertEqual("NO_ACTIVE_LEASE", stale["reasonCode"])

        self.create("AWB-302")
        recovery_clock = self.now()
        expired = claim(self.database, "AWB-302", "owner-old", 30,
                        "claim-302", recovery_clock)
        acquire_claim(
            self.database, "AWB-302", "AWB-302-T01", "active-planner", "PLANNER",
            self.agent_expiry(), orchestrator_id="owner-old",
            orchestrator_generation=expired["lease"]["generation"],
        )
        refused = recover(self.database, "AWB-302", "owner-new", 30,
                          "recover-302-refused", self.now(31))
        self.assertEqual("EXPIRED_ACTIVITY_RECONCILIATION_REQUIRED",
                         refused["reasonCode"])
        connection = open_database(self.database)
        connection.execute(
            "UPDATE orchestrator_leases SET expires_at=? WHERE lease_id=?",
            (self.now(-1), expired["lease"]["lease_id"]),
        )
        connection.commit(); connection.close()
        reconciled = self.reconcile_checked(
            "AWB-302", "orchestrator-lease", expired["lease"]["lease_id"],
            "owner-old", expired["lease"]["generation"],
        )
        self.assertEqual("OK", reconciled["status"])
        recovered = recover(self.database, "AWB-302", "owner-new", 30,
                            "recover-302", self.now(31))
        self.assertEqual("OK", recovered["status"])
        self.assertEqual(expired["lease"]["generation"] + 1,
                         recovered["lease"]["generation"])
        self.assertEqual("WRONG_OWNER", release(
            self.database, "AWB-302", "owner-old",
            expired["lease"]["generation"], "old-release")["reasonCode"])
        connection = open_database(self.database)
        self.assertEqual(1, connection.execute(
            "SELECT count(*) FROM claims WHERE work_item_id='AWB-302' AND status='ACTIVE'"
        ).fetchone()[0])
        connection.close()

    def test_dispatch_fence_refusals_are_zero_side_effect_and_fault_is_atomic(self):
        self.create("AWB-310")
        active = claim(self.database, "AWB-310", "owner", 900, "claim-310")
        generation = active["lease"]["generation"]
        cases = (
            {},
            {"orchestrator_id": "owner"},
            {"orchestrator_generation": generation},
            {"orchestrator_id": "wrong", "orchestrator_generation": generation},
            {"orchestrator_id": "owner", "orchestrator_generation": generation + 1},
        )
        for index, values in enumerate(cases):
            before = self.snapshot("AWB-310")
            with self.assertRaises(LiteError):
                acquire_claim(
                    self.database, "AWB-310", "AWB-310-T01", "agent-" + str(index),
                    "PLANNER", self.agent_expiry(), **values
                )
            self.assertEqual(before, self.snapshot("AWB-310"))

        self.create("AWB-311")
        old = self.now(-5)
        expired = claim(self.database, "AWB-311", "expired-owner", 1,
                        "claim-311", old)
        before = self.snapshot("AWB-311")
        with self.assertRaisesRegex(LiteError, "WORKFLOW_INVARIANT_VIOLATION"):
            acquire_claim(
                self.database, "AWB-311", "AWB-311-T01", "expired-agent", "PLANNER",
                self.agent_expiry(), orchestrator_id="expired-owner",
                orchestrator_generation=expired["lease"]["generation"],
            )
        self.assertEqual(before, self.snapshot("AWB-311"))

        self.create("AWB-312")
        with mock.patch.object(orchestrator_module, "_event",
                               side_effect=RuntimeError("commit-boundary fault")):
            with self.assertRaisesRegex(RuntimeError, "commit-boundary"):
                claim(self.database, "AWB-312", "fault-owner", 900, "claim-312")
        connection = open_database(self.database)
        self.assertEqual(0, connection.execute(
            "SELECT count(*) FROM orchestrator_instances WHERE orchestrator_id='fault-owner'"
        ).fetchone()[0])
        self.assertEqual(0, connection.execute(
            "SELECT count(*) FROM orchestrator_leases WHERE work_item_id='AWB-312'"
        ).fetchone()[0])
        connection.close()

    def test_database_busy_is_structured_and_zero_write(self):
        self.create("AWB-320")
        blocker = open_database(self.database)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch("agent_workboard.lite.BUSY_TIMEOUT_MS", 1):
                result = claim(self.database, "AWB-320", "busy-owner", 900,
                               "claim-busy")
            self.assertEqual("DATABASE_BUSY", result["reasonCode"])
        finally:
            blocker.rollback()
            blocker.close()
        connection = open_database(self.database)
        self.assertEqual(0, connection.execute(
            "SELECT count(*) FROM orchestrator_leases WHERE work_item_id='AWB-320'"
        ).fetchone()[0])
        connection.close()

    def snapshot(self, work_item_id):
        connection = open_database(self.database)
        try:
            return {
                "item": tuple(connection.execute(
                    "SELECT queue_state,row_version,updated_at FROM work_items WHERE work_item_id=?",
                    (work_item_id,)).fetchone()),
                "claims": connection.execute(
                    "SELECT count(*) FROM claims WHERE work_item_id=?", (work_item_id,)
                ).fetchone()[0],
                "workflowEvents": connection.execute(
                    "SELECT count(*) FROM events WHERE work_item_id=?", (work_item_id,)
                ).fetchone()[0],
                "orchestratorEvents": connection.execute(
                    "SELECT count(*) FROM orchestrator_events WHERE work_item_id=?",
                    (work_item_id,)).fetchone()[0],
            }
        finally:
            connection.close()

    def database_dump(self):
        connection = open_database(self.database)
        try:
            return "\n".join(connection.iterdump())
        finally:
            connection.close()

    def test_expired_release_is_zero_write_and_preserves_recovery(self):
        self.create("AWB-303")
        acquired = claim(
            self.database, "AWB-303", "expired-owner", 1, "claim-303",
            "2025-01-01T00:00:00+00:00",
        )
        before = self.database_dump()
        refused = release(
            self.database, "AWB-303", "expired-owner",
            acquired["lease"]["generation"], "expired-release-303",
            "2025-01-01T00:00:02+00:00",
        )
        self.assertEqual("REFUSED", refused["status"])
        self.assertEqual("EXPIRED_FENCE", refused["reasonCode"])
        self.assertEqual(before, self.database_dump())
        recovered = recover(
            self.database, "AWB-303", "new-owner", 30, "recover-303",
            "2025-01-01T00:00:02+00:00",
        )
        self.assertEqual("EXPIRED_ACTIVITY_RECONCILIATION_REQUIRED",
                         recovered["reasonCode"])
        reconciled = self.reconcile_checked(
            "AWB-303", "orchestrator-lease", acquired["lease"]["lease_id"],
            "expired-owner", acquired["lease"]["generation"],
        )
        self.assertEqual("OK", reconciled["status"])
        recovered = recover(
            self.database, "AWB-303", "new-owner", 30, "recover-303-after-check",
        )
        self.assertEqual("OK", recovered["status"])
        self.assertEqual(acquired["lease"]["generation"] + 1,
                         recovered["lease"]["generation"])

    def test_schema_fingerprint_rejects_active_predicate_drift_zero_write(self):
        self.create("AWB-304")
        connection = open_database(self.database)
        for sequence, predicate in enumerate(("'RELEASED'", "' ACTIVE '", "'active '")):
            with self.subTest(predicate=predicate):
                connection.executescript(
                    "DROP INDEX one_active_orchestrator_per_work_item;"
                    "CREATE UNIQUE INDEX one_active_orchestrator_per_work_item "
                    "ON orchestrator_leases(work_item_id) WHERE status={0};".format(
                        predicate
                    )
                )
                connection.commit()
                self.assertEqual("INVALID", schema_state(connection))
                before = self.database_dump()
                refused = claim(
                    self.database, "AWB-304", "drift-owner", 30,
                    "drift-claim-{0}".format(sequence),
                )
                self.assertEqual("SCHEMA_INVALID", refused["reasonCode"])
                self.assertEqual(before, self.database_dump())
        connection.executescript(
            "DROP INDEX one_active_orchestrator_per_work_item;"
            "CrEaTe UnIqUe /* formatting is not schema drift */ INDEX "
            "one_active_orchestrator_per_work_item "
            "ON orchestrator_leases ( work_item_id ) "
            "WHERE STATUS = 'ACTIVE' -- trailing comment\n;"
        )
        connection.commit()
        self.assertEqual("INSTALLED", schema_state(connection))
        connection.close()

    def test_schema_normalizer_preserves_quoted_contents_and_escapes(self):
        normalize = orchestrator_module._normalize_schema_sql
        expected = "CREATE TABLE Thing(value TEXT CHECK(value='A'' B'), \"Exact Name\" TEXT)"
        equivalent = (
            "create /* 'ignored comment literal' */ table Thing "
            "( value text check ( value = 'A'' B' ) , \"Exact Name\" text )"
        )
        self.assertEqual(normalize(expected), normalize(equivalent))
        for drifted in (
            "CREATE TABLE Thing(value TEXT CHECK(value='a'' b'), \"Exact Name\" TEXT)",
            "CREATE TABLE Thing(value TEXT CHECK(value='A''  B'), \"Exact Name\" TEXT)",
            "CREATE TABLE Thing(value TEXT CHECK(value='A'' B'), \"exact name\" TEXT)",
        ):
            with self.subTest(drifted=drifted):
                self.assertNotEqual(normalize(expected), normalize(drifted))

    def test_zero_history_legacy_dispatch_fence_combinations_and_writer_serialization(self):
        self.create("AWB-401")
        legacy = acquire_claim(self.database, "AWB-401", "AWB-401-T01",
                               "legacy", "PLANNER", self.agent_expiry())
        self.assertEqual(1, legacy["generation"])
        release_claim(self.database, "AWB-401", "legacy")

        self.create("AWB-402")
        self.create("AWB-403")
        lease_a = claim(self.database, "AWB-402", "owner-a", 900, "claim-402")
        lease_b = claim(self.database, "AWB-403", "owner-b", 900, "claim-403")
        for work_item, owner, lease, agent in (
            ("AWB-402", "owner-a", lease_a, "planner-a"),
            ("AWB-403", "owner-b", lease_b, "planner-b"),
        ):
            acquire_claim(self.database, work_item, work_item + "-T01", agent,
                          "PLANNER", self.agent_expiry(), orchestrator_id=owner,
                          orchestrator_generation=lease["lease"]["generation"])
        acquire_repository_lock(self.database, "AWB-402", "same-repo", "planner-a",
                                self.agent_expiry())
        with self.assertRaisesRegex(LiteError, "active writer"):
            acquire_repository_lock(self.database, "AWB-403", "same-repo", "planner-b",
                                    self.agent_expiry())
        release_repository_lock(self.database, "AWB-402", "same-repo", "planner-a")
        acquire_repository_lock(self.database, "AWB-403", "same-repo", "planner-b",
                                self.agent_expiry())

    def test_schema_missing_cli_json_queries_immutability_and_privacy(self):
        self.create("AWB-501")
        result = claim(self.database, "AWB-501", "private-safe", 900, "claim-501")
        self.assert_envelope(result, "CLAIM")
        detail = show(self.database, "AWB-501")
        self.assertEqual("OK", detail["status"])
        self.assertTrue(detail["events"])
        connection = open_database(self.database)
        try:
            payload = json.loads(connection.execute(
                "SELECT payload_json FROM orchestrator_events WHERE request_id='claim-501'"
            ).fetchone()[0])
            self.assertEqual({"request", "result"}, set(payload))
            self.assertNotIn("session", json.dumps(payload).lower())
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE orchestrator_events SET operation='X'")
        finally:
            connection.close()

        with mock.patch("agent_workboard.cli._project_database", return_value=self.database):
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli_main(["orchestrator", "show", "AWB-501", "--project", "."])
        self.assertEqual(0, code)
        self.assertEqual(ORCHESTRATOR_SCHEMA_VERSION,
                         json.loads(output.getvalue())["protocolVersion"])
        with mock.patch("agent_workboard.cli._project_database", return_value=self.database):
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli_main([
                    "orchestrator", "claim", "AWB-501", "--project", ".",
                    "--orchestrator", "bad id", "--ttl", "0",
                    "--request-id", "bad-request",
                ])
        invalid = json.loads(output.getvalue())
        self.assertEqual(2, code)
        self.assertEqual("INVALID_ARGUMENT", invalid["reasonCode"])
        self.assert_envelope(invalid, "CLAIM")

        absent = os.path.join(self.temporary.name, "old.db")
        initialize_database(absent)
        connection = sqlite3.connect(absent)
        connection.executescript(
            "DROP TRIGGER orchestrator_events_no_update;"
            "DROP TRIGGER orchestrator_events_no_delete;"
            "DROP TABLE orchestrator_events;DROP TABLE orchestrator_leases;"
            "DROP TABLE orchestrator_instances;"
            "DELETE FROM schema_meta WHERE key='orchestrator_schema_version';"
        )
        connection.commit()
        connection.close()
        self.assertEqual("ABSENT", schema_state(open_database(absent)))
        self.assertEqual("SCHEMA_NOT_INSTALLED", register(
            absent, "local-a", "register-old")["reasonCode"])

    def test_all_public_cli_subcommands_have_stable_envelopes(self):
        self.create("AWB-510")
        self.create("AWB-511")
        code, value = self.cli([
            "register", "--orchestrator", "cli-a", "--request-id", "cli-register",
        ])
        self.assertEqual(0, code)
        self.assert_envelope(value, "REGISTER")
        code, value = self.cli([
            "claim", "AWB-510", "--orchestrator", "cli-a", "--ttl", "30",
            "--request-id", "cli-claim",
        ])
        self.assertEqual(0, code)
        generation = value["lease"]["generation"]
        for arguments, operation in (
            (["renew", "AWB-510", "--orchestrator", "cli-a", "--generation",
              str(generation), "--ttl", "30", "--request-id", "cli-renew"], "RENEW"),
            (["list", "--status", "ACTIVE"], "LIST"),
            (["show", "AWB-510"], "SHOW"),
            (["release", "AWB-510", "--orchestrator", "cli-a", "--generation",
              str(generation), "--request-id", "cli-release"], "RELEASE"),
        ):
            code, value = self.cli(arguments)
            self.assertEqual(0, code)
            self.assert_envelope(value, operation)
        code, value = self.cli([
            "claim-next", "--orchestrator", "cli-b", "--ttl", "30",
            "--request-id", "cli-next",
        ])
        self.assertEqual(0, code)
        self.assert_envelope(value, "CLAIM_NEXT")
        recovered_work_item = value["lease"]["work_item_id"]
        expired_lease = dict(value["lease"])
        connection = open_database(self.database)
        connection.execute(
            "UPDATE orchestrator_leases SET expires_at=? WHERE lease_id=?",
            (self.now(-5), value["lease"]["lease_id"]),
        )
        connection.commit()
        connection.close()
        code, value = self.cli([
            "recover", recovered_work_item, "--orchestrator", "cli-c", "--ttl", "30",
            "--request-id", "cli-recover",
        ])
        self.assertEqual(2, code)
        self.assert_envelope(value, "RECOVER")
        self.assertEqual("EXPIRED_ACTIVITY_RECONCILIATION_REQUIRED",
                         value["reasonCode"])
        self.assertEqual("OK", self.reconcile_checked(
            recovered_work_item, "orchestrator-lease",
            expired_lease["lease_id"], "cli-b", expired_lease["generation"],
        )["status"])
        code, value = self.cli([
            "recover", recovered_work_item, "--orchestrator", "cli-c",
            "--ttl", "30", "--request-id", "cli-recover-after-check",
        ])
        self.assertEqual(0, code)
        self.assert_envelope(value, "RECOVER")


if __name__ == "__main__":
    unittest.main()
