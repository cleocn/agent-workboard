import datetime
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import tarfile
import unittest
import zipfile
from unittest import mock

from agent_workboard import candidate
from agent_workboard import verify
from agent_workboard.lite import (
    LiteError, acquire_claim, acquire_repository_lock, create_work_item,
    initialize_database, record_agent_review, set_task_status, transition,
)


class CommitThenRaiseConnection:
    """SQLite proxy that reports one successful commit as an exception."""

    def __init__(self, connection, state):
        self.connection = connection
        self.state = state

    def commit(self):
        self.connection.commit()
        if not self.state["raised"]:
            self.state["raised"] = True
            if self.state.get("after_commit"):
                self.state["after_commit"]()
            raise RuntimeError("simulated unknown commit outcome")

    def __getattr__(self, name):
        return getattr(self.connection, name)


class ManagedCandidateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = self.temporary.name
        os.makedirs(os.path.join(self.root, ".awb"))
        self.database = os.path.join(self.root, ".awb", "workboard.db")
        initialize_database(self.database)
        self.work_item = "AWB-CAND"
        management = {
            "contractVersion": "AWB-WORKITEM-MGMT-v1",
            "templateContractVersion": "AWB-MANAGEMENT-v1",
            "scope": ["release candidate"], "outOfScope": ["remote"],
            "authorization": {"allowed": ["local"], "forbidden": ["remote"]},
            "safetyConstraints": ["no delete"],
            "tasks": [
                {"taskId": self.work_item + "-T01", "seq": 1, "title": "plan",
                 "ownerRole": "PLANNER", "required": True,
                 "acceptance": ["plan"], "closureEvidenceRequired": ["plan"]},
                {"taskId": self.work_item + "-T02", "seq": 2, "title": "implementation",
                 "ownerRole": "IMPLEMENTER", "required": True,
                 "acceptance": ["implementation"], "closureEvidenceRequired": ["tests"]},
                {"taskId": self.work_item + "-T03", "seq": 3, "title": "review",
                 "ownerRole": "REVIEWER", "required": True,
                 "acceptance": ["review"], "closureEvidenceRequired": ["review"]},
            ],
            "acceptance": [{"id": "AC-001", "criterion": "passes"}],
            "closure": [{"id": "CL-001", "criterion": "evidence"}],
        }
        create_work_item(self.database, self.work_item, "AWB", "candidate",
                         management=management, human_review="AUTO_ON_PASS")
        expires = (datetime.datetime.now(datetime.timezone.utc) +
                   datetime.timedelta(hours=1)).replace(microsecond=0).isoformat()
        acquire_claim(self.database, self.work_item, self.work_item + "-T01",
                      "planner", "PLANNER", expires)
        set_task_status(self.database, self.work_item, self.work_item + "-T01",
                        "planner", "IN_PROGRESS")
        transition(self.database, self.work_item, "submit_plan", "planner")
        acquire_claim(self.database, self.work_item, self.work_item + "-T03",
                      "plan-reviewer", "REVIEWER", expires)
        record_agent_review(self.database, self.work_item, "PLAN", "plan-reviewer",
                            "APPROVED", "pass")
        acquire_claim(self.database, self.work_item, self.work_item + "-T02",
                      "implementer", "IMPLEMENTER", expires)
        transition(self.database, self.work_item, "start_implementation", "implementer")
        set_task_status(self.database, self.work_item, self.work_item + "-T02",
                        "implementer", "IN_PROGRESS")
        acquire_repository_lock(self.database, self.work_item, "repo", "implementer", expires)
        self.source = os.path.join(self.root, "base-source")
        os.makedirs(self.source)
        subprocess.check_call(["git", "init"], cwd=self.source, stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "config", "user.name", "test"], cwd=self.source)
        subprocess.check_call(["git", "config", "user.email", "test@example.invalid"], cwd=self.source)
        with open(os.path.join(self.source, "release.txt"), "w", encoding="utf-8") as handle:
            handle.write("base\n")
        subprocess.check_call(["git", "add", "release.txt"], cwd=self.source)
        subprocess.check_call(["git", "commit", "-m", "base"], cwd=self.source,
                              stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "tag", "-a", "v-base", "-m", "base"], cwd=self.source)
        self.base = {
            "packageVersion": "0.3.1b6",
            "sourceCommit": self.git("rev-parse", "HEAD"),
            "sourceTree": self.git("rev-parse", "HEAD^{tree}"),
            "sourceTag": "v-base", "wheelSha256": "a" * 64,
        }
        self.target = {
            "version": "0.3.1b7", "tag": "v-next", "releaseBranch": "release/next",
            "title": "Preview", "assetNames": ["package.whl", "package.tar.gz", "SHA256SUMS"],
        }
        self.base_file = self.json_file("base.json", self.base)
        self.target_file = self.json_file("target.json", self.target)

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args, **kwargs):
        repository = kwargs.get("repository", self.source)
        return subprocess.check_output(["git"] + list(args), cwd=repository).decode("ascii").strip()

    def json_file(self, name, value):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return name

    def commit_then_raise(self, after_commit=None):
        original = candidate.lite.open_database
        state = {"raised": False, "after_commit": after_commit}

        def open_database(path):
            return CommitThenRaiseConnection(original(path), state)

        return mock.patch.object(candidate.lite, "open_database",
                                 side_effect=open_database)

    def prepare(self, request="prepare-1"):
        return candidate.prepare(
            self.database, self.root, "repo", self.work_item, "candidate-1",
            self.source, self.base_file, self.target_file, "implementer", request,
        )

    def test_prepare_replay_is_zero_write_and_wrong_owner_is_refused(self):
        first = self.prepare()
        self.assertEqual("OK", first["status"])
        active = os.path.join(self.root, ".awb", "release-candidates", self.work_item,
                              "managed", "active", "candidate-1")
        before = candidate._tree_fingerprint(active)
        replay = self.prepare()
        self.assertEqual(first["eventId"], replay["eventId"])
        self.assertEqual(before, candidate._tree_fingerprint(active))
        with self.assertRaises(LiteError):
            candidate.prepare(
                self.database, self.root, "repo", self.work_item, "candidate-2",
                self.source, self.base_file, self.target_file, "other", "prepare-2",
            )

    def test_prepare_rejects_traversal_and_existing_destination(self):
        with self.assertRaises(LiteError):
            candidate.prepare(
                self.database, self.root, "repo", self.work_item, "../escape",
                self.source, self.base_file, self.target_file, "implementer", "prepare-x",
            )
        self.prepare()
        with self.assertRaises(LiteError):
            candidate.prepare(
                self.database, self.root, "repo", self.work_item, "candidate-1",
                self.source, self.base_file, self.target_file, "implementer", "prepare-2",
            )

    def test_prepare_event_fault_compensates_and_concurrency_has_one_winner(self):
        def fault(connection, event_type, request_id):
            raise RuntimeError("candidate event fault")

        before = list(candidate.lite.timeline(self.database, self.work_item))
        with mock.patch.object(candidate, "_candidate_materialized", side_effect=fault):
            with self.assertRaisesRegex(RuntimeError, "candidate event fault"):
                self.prepare("prepare-fault")
        self.assertEqual(before, candidate.lite.timeline(self.database, self.work_item))
        fault_staging = os.path.join(
            self.root, ".awb", "release-candidates", self.work_item,
            "managed", "staging", "candidate-1", "prepare-fault",
        )
        self.assertTrue(os.path.isdir(fault_staging))

        results = []
        lock = threading.Lock()

        def contender(request_id):
            try:
                value = self.prepare(request_id)
            except Exception as exc:
                value = exc
            with lock:
                results.append(value)

        threads = [threading.Thread(target=contender, args=(request_id,))
                   for request_id in ("prepare-a", "prepare-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, sum(isinstance(value, dict) for value in results))
        self.assertEqual(1, sum(isinstance(value, Exception) for value in results))
        connection = candidate.lite.open_database(self.database)
        try:
            prepared = connection.execute(
                "SELECT count(*) FROM events WHERE work_item_id=? "
                "AND event_type='CANDIDATE_PREPARED'", (self.work_item,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(1, prepared)

    def _prepare_committed_candidate(self, allow_name):
        self.prepare()
        managed_source = os.path.join(
            self.root, ".awb", "release-candidates", self.work_item,
            "managed", "active", "candidate-1", "source",
        )
        subprocess.check_call(["git", "config", "user.name", "test"], cwd=managed_source)
        subprocess.check_call(["git", "config", "user.email", "test@example.invalid"], cwd=managed_source)
        with open(os.path.join(managed_source, "release.txt"), "w", encoding="utf-8") as handle:
            handle.write("candidate\n")
        subprocess.check_call(["git", "add", "release.txt"], cwd=managed_source)
        subprocess.check_call(["git", "commit", "-m", "candidate"], cwd=managed_source,
                              stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "tag", "-a", "v-next", "-m", "next"], cwd=managed_source)
        return managed_source, self.json_file(allow_name, {"paths": ["release.txt"]})

    def test_freeze_concurrent_loser_never_compensates_committed_winner(self):
        unused_source, allow = self._prepare_committed_candidate("allow-race.json")
        results = []
        result_lock = threading.Lock()

        def contender(request_id):
            try:
                value = candidate.freeze(
                    self.database, self.root, "repo", self.work_item, "candidate-1",
                    allow, "implementer", request_id,
                )
            except Exception as exc:
                value = exc
            with result_lock:
                results.append((request_id, value))

        threads = [threading.Thread(target=contender, args=(request_id,))
                   for request_id in ("freeze-a", "freeze-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [(request_id, value) for request_id, value in results
                   if isinstance(value, dict)]
        losers = [value for unused_request, value in results
                  if isinstance(value, Exception)]
        self.assertEqual((1, 1), (len(winners), len(losers)))
        manifest_path = candidate._candidate_manifest(
            self.root, self.work_item, "candidate-1"
        )[2]
        manifest = candidate._load_json(manifest_path)
        self.assertEqual(("FROZEN", winners[0][0]),
                         (manifest["lifecycle"], manifest["freezeRequestId"]))
        connection = candidate.lite.open_database(self.database)
        try:
            head_row, head_payload = candidate._candidate_head(
                connection, self.work_item, "candidate-1"
            )
        finally:
            connection.close()
        self.assertEqual("CANDIDATE_FROZEN", head_row["event_type"])
        self.assertEqual(manifest["candidateFingerprint"],
                         head_payload["candidateFingerprint"])

    def test_freeze_event_fault_compensates_only_owned_prepared_bytes(self):
        unused_source, allow = self._prepare_committed_candidate("allow-fault.json")
        def fault(connection, event_type, request_id):
            raise RuntimeError("freeze event fault")

        with mock.patch.object(candidate, "_candidate_materialized", side_effect=fault):
            with self.assertRaisesRegex(RuntimeError, "freeze event fault"):
                candidate.freeze(
                    self.database, self.root, "repo", self.work_item, "candidate-1",
                    allow, "implementer", "freeze-fault",
                )
        manifest = candidate._load_json(candidate._candidate_manifest(
            self.root, self.work_item, "candidate-1"
        )[2])
        self.assertEqual("PREPARED", manifest["lifecycle"])
        connection = candidate.lite.open_database(self.database)
        try:
            replay = connection.execute(
                "SELECT event_id FROM events WHERE request_id='freeze-fault'"
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNone(replay)

    def test_unknown_commit_ambiguity_preserves_recoverable_frozen_bytes(self):
        unused_source, allow = self._prepare_committed_candidate(
            "allow-ambiguous.json"
        )

        def remove_persisted_receipt():
            connection = sqlite3.connect(self.database)
            try:
                raw = connection.execute(
                    "SELECT payload_json FROM events WHERE request_id=?",
                    ("freeze-ambiguous",),
                ).fetchone()[0]
                payload = json.loads(raw)
                payload.pop("resultReceipt")
                payload.pop("resultRowVersion")
                connection.execute(
                    "UPDATE events SET payload_json=? WHERE request_id=?",
                    (json.dumps(payload), "freeze-ambiguous"),
                )
                connection.commit()
            finally:
                connection.close()

        with self.commit_then_raise(remove_persisted_receipt):
            with self.assertRaisesRegex(LiteError, "COMMIT_OUTCOME_AMBIGUOUS"):
                candidate.freeze(
                    self.database, self.root, "repo", self.work_item,
                    "candidate-1", allow, "implementer", "freeze-ambiguous",
                )
        manifest = candidate._load_json(candidate._candidate_manifest(
            self.root, self.work_item, "candidate-1"
        )[2])
        self.assertEqual("FROZEN", manifest["lifecycle"])
        connection = candidate.lite.open_database(self.database)
        try:
            head_row, unused_payload = candidate._candidate_head(
                connection, self.work_item, "candidate-1"
            )
        finally:
            connection.close()
        self.assertEqual("CANDIDATE_FROZEN", head_row["event_type"])

    def test_tree_fingerprint_rejects_symlinks_and_directory_race(self):
        tree = os.path.join(self.root, "unsafe-tree")
        external = os.path.join(self.root, "external")
        os.makedirs(os.path.join(tree, "nested"))
        os.makedirs(external)
        with open(os.path.join(external, "secret"), "w", encoding="utf-8") as handle:
            handle.write("outside\n")
        os.symlink(os.path.join(external, "secret"), os.path.join(tree, "file-link"))
        with self.assertRaisesRegex(LiteError, "symlink"):
            candidate._tree_fingerprint(tree)
        os.rename(os.path.join(tree, "file-link"), os.path.join(tree, "saved-file-link"))
        os.symlink(external, os.path.join(tree, "directory-link"))
        with self.assertRaisesRegex(LiteError, "symlink"):
            candidate._tree_fingerprint(tree)

        safe_tree = os.path.join(self.root, "race-tree")
        child = os.path.join(safe_tree, "child")
        os.makedirs(child)
        with open(os.path.join(child, "value"), "w", encoding="utf-8") as handle:
            handle.write("value\n")
        child_inode = os.stat(child).st_ino
        original_fstat = candidate.os.fstat
        child_checks = [0]

        def racing_fstat(descriptor):
            value = original_fstat(descriptor)
            if value.st_ino == child_inode:
                child_checks[0] += 1
                if child_checks[0] == 2:
                    fields = list(value)
                    fields[stat.ST_INO] += 1
                    return os.stat_result(fields)
            return value

        with mock.patch.object(candidate.os, "fstat", side_effect=racing_fstat):
            with self.assertRaisesRegex(LiteError, "changed during scan"):
                candidate._tree_fingerprint(safe_tree)

    def test_prepare_rejects_symlinked_managed_component(self):
        release_root = os.path.join(self.root, ".awb", "release-candidates")
        external = os.path.join(self.root, "managed-external")
        os.makedirs(release_root)
        os.makedirs(external)
        os.symlink(external, os.path.join(release_root, self.work_item))
        with self.assertRaisesRegex(LiteError, "contains a symlink"):
            self.prepare("prepare-symlinked-managed")

    def test_freeze_rejects_tracked_file_and_external_directory_symlinks(self):
        self.prepare()
        managed_source = os.path.join(
            self.root, ".awb", "release-candidates", self.work_item,
            "managed", "active", "candidate-1", "source",
        )
        subprocess.check_call(["git", "config", "user.name", "test"], cwd=managed_source)
        subprocess.check_call(["git", "config", "user.email", "test@example.invalid"], cwd=managed_source)
        with open(os.path.join(managed_source, "release.txt"), "w", encoding="utf-8") as handle:
            handle.write("candidate\n")
        external = os.path.join(self.root, "tracked-link-target")
        os.makedirs(external)
        with open(os.path.join(external, "value"), "w", encoding="utf-8") as handle:
            handle.write("outside\n")
        os.symlink(os.path.join(external, "value"),
                   os.path.join(managed_source, "tracked-file-link"))
        os.symlink(external, os.path.join(managed_source, "tracked-directory-link"))
        subprocess.check_call(
            ["git", "add", "release.txt", "tracked-file-link", "tracked-directory-link"],
            cwd=managed_source,
        )
        subprocess.check_call(["git", "commit", "-m", "candidate"], cwd=managed_source,
                              stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "tag", "-a", "v-next", "-m", "next"], cwd=managed_source)
        allow = self.json_file("allow-symlinks.json", {"paths": [
            "release.txt", "tracked-directory-link", "tracked-file-link",
        ]})
        with self.assertRaisesRegex(LiteError, "symlink"):
            candidate.freeze(
                self.database, self.root, "repo", self.work_item, "candidate-1",
                allow, "implementer", "freeze-symlinks",
            )
        manifest = candidate._load_json(candidate._candidate_manifest(
            self.root, self.work_item, "candidate-1"
        )[2])
        self.assertEqual("PREPARED", manifest["lifecycle"])
        connection = candidate.lite.open_database(self.database)
        try:
            event = connection.execute(
                "SELECT event_id FROM events WHERE request_id='freeze-symlinks'"
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNone(event)

    def test_freeze_and_quarantine_commit_then_raise_preserve_bytes(self):
        self.prepare()
        managed_source = os.path.join(
            self.root, ".awb", "release-candidates", self.work_item,
            "managed", "active", "candidate-1", "source",
        )
        subprocess.check_call(["git", "config", "user.name", "test"], cwd=managed_source)
        subprocess.check_call(["git", "config", "user.email", "test@example.invalid"], cwd=managed_source)
        with open(os.path.join(managed_source, "release.txt"), "w", encoding="utf-8") as handle:
            handle.write("candidate\n")
        subprocess.check_call(["git", "add", "release.txt"], cwd=managed_source)
        subprocess.check_call(["git", "commit", "-m", "candidate"], cwd=managed_source,
                              stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "tag", "-a", "v-next", "-m", "next"], cwd=managed_source)
        allow = self.json_file("allow.json", {"paths": ["release.txt"]})
        with self.commit_then_raise():
            frozen = candidate.freeze(
                self.database, self.root, "repo", self.work_item, "candidate-1",
                allow, "implementer", "freeze-1",
            )
        self.assertEqual("OK", frozen["status"])
        frozen_replay = candidate.freeze(
            self.database, self.root, "repo", self.work_item, "candidate-1",
            allow, "implementer", "freeze-1",
        )
        self.assertEqual(frozen["eventId"], frozen_replay["eventId"])
        frozen_timeline = candidate.lite.timeline(self.database, self.work_item)
        with self.assertRaises(LiteError):
            candidate.freeze(
                self.database, self.root, "repo", self.work_item, "candidate-1",
                allow, "other-owner", "freeze-1",
            )
        self.assertEqual(frozen_timeline,
                         candidate.lite.timeline(self.database, self.work_item))
        active = os.path.dirname(managed_source)
        before = candidate._tree_fingerprint(active)
        with self.commit_then_raise():
            moved = candidate.quarantine(
                self.database, self.root, "repo", self.work_item, "candidate-1",
                "implementer", "quarantine-1",
            )
        self.assertEqual("OK", moved["status"])
        moved_replay = candidate.quarantine(
            self.database, self.root, "repo", self.work_item, "candidate-1",
            "implementer", "quarantine-1",
        )
        self.assertEqual(moved["eventId"], moved_replay["eventId"])
        moved_timeline = candidate.lite.timeline(self.database, self.work_item)
        with self.assertRaisesRegex(LiteError, "different content"):
            candidate.quarantine(
                self.database, self.root, "repo", self.work_item, "candidate-1",
                "implementer", "quarantine-1", restore=True,
                source_request_id="quarantine-1",
            )
        self.assertEqual(moved_timeline,
                         candidate.lite.timeline(self.database, self.work_item))
        quarantine_journal = os.path.join(
            self.root, ".awb", "release-candidates", self.work_item,
            "managed", "journal", "candidate-1", "quarantine-1.json",
        )
        self.assertEqual("COMMITTED",
                         candidate._load_json(quarantine_journal)["status"])
        quarantined = os.path.join(self.root, ".awb", "release-candidates", self.work_item,
                                   "managed", "quarantine", "candidate-1", "quarantine-1")
        self.assertEqual(before, candidate._tree_fingerprint(quarantined))
        restored = candidate.quarantine(
            self.database, self.root, "repo", self.work_item, "candidate-1",
            "implementer", "restore-1", restore=True,
            source_request_id="quarantine-1",
        )
        self.assertEqual("OK", restored["status"])
        restored_replay = candidate.quarantine(
            self.database, self.root, "repo", self.work_item, "candidate-1",
            "implementer", "restore-1", restore=True,
            source_request_id="quarantine-1",
        )
        self.assertEqual(restored["eventId"], restored_replay["eventId"])
        self.assertEqual(before, candidate._tree_fingerprint(active))

    def test_finalize_commit_then_raise_preserves_committed_manifest(self):
        self._freeze_for_build()
        with self.commit_then_raise():
            finalized = candidate.finalize(
                self.database, self.root, "repo", self.work_item, "candidate-1",
                "implementer", "finalize-1",
            )
        self.assertEqual(finalized, candidate.finalize(
            self.database, self.root, "repo", self.work_item, "candidate-1",
            "implementer", "finalize-1",
        ))
        finalized_timeline = candidate.lite.timeline(self.database, self.work_item)
        with self.assertRaisesRegex(LiteError, "different content"):
            candidate.finalize(
                self.database, self.root, "repo", self.work_item, "candidate-1",
                "other-owner", "finalize-1",
            )
        self.assertEqual(finalized_timeline,
                         candidate.lite.timeline(self.database, self.work_item))
        final_manifest = candidate._load_json(candidate._candidate_manifest(
            self.root, self.work_item, "candidate-1"
        )[2])
        self.assertEqual("FINALIZED", final_manifest["lifecycle"])
        connection = candidate.lite.open_database(self.database)
        try:
            head_row, unused_payload = candidate._candidate_head(
                connection, self.work_item, "candidate-1"
            )
        finally:
            connection.close()
        self.assertEqual("CANDIDATE_FINALIZED", head_row["event_type"])

    def test_toolchain_drift_refuses_before_build_files_or_event(self):
        self.prepare()
        toolchain = {
            "protocolVersion": "AWB-PINNED-PYTHON-BUILD-v1", "toolchainId": "bad",
            "python": {"canonicalExecutable": os.path.realpath(os.sys.executable),
                       "executableSha256": "0" * 64, "implementation": "CPython",
                       "version": "0"},
            "packages": {"setuptools": "0", "wheel": "0"},
            "backend": "SETUPTOOLS_BDIST_WHEEL_SDIST",
            "environment": {"TZ": "UTC", "LC_ALL": "C", "SOURCE_DATE_EPOCH": "COMMIT_EPOCH"},
        }
        path = self.json_file("toolchain.json", toolchain)
        with self.assertRaisesRegex(LiteError, "TOOLCHAIN_NOT_READY"):
            candidate.build(self.database, self.root, "repo", self.work_item,
                            "candidate-1", path, "implementer", "build-1")
        staging = os.path.join(self.root, ".awb", "release-candidates", self.work_item,
                               "managed", "staging", "candidate-1", "build-1")
        self.assertFalse(os.path.exists(staging))

    def test_publication_retry_budget_allows_only_fresh_ordinary_r2_or_r3(self):
        review = lambda number, mode="ORDINARY", result="PASS": {
            "round": number, "reviewerMode": mode, "result": result,
        }
        self.assertEqual(2, candidate.retry_review_route([review(1)])["expectedNextReviewRound"])
        self.assertEqual(3, candidate.retry_review_route([review(1), review(2)])["expectedNextReviewRound"])
        self.assertIsNone(candidate.retry_review_route([review(1), review(2), review(3)]))
        self.assertIsNone(candidate.retry_review_route(
            [review(1), review(2), review(3, result="REVISE"), review(4, "CONVERGENCE")]
        ))
        self.assertIsNone(candidate.retry_review_route(
            [review(1), review(2), review(3), review(4, "CONVERGENCE"), review(5)]
        ))

    def _freeze_for_build(self):
        self.prepare()
        managed_source = os.path.join(
            self.root, ".awb", "release-candidates", self.work_item,
            "managed", "active", "candidate-1", "source",
        )
        subprocess.check_call(["git", "config", "user.name", "test"], cwd=managed_source)
        subprocess.check_call(["git", "config", "user.email", "test@example.invalid"], cwd=managed_source)
        with open(os.path.join(managed_source, "release.txt"), "w", encoding="utf-8") as handle:
            handle.write("candidate\n")
        subprocess.check_call(["git", "add", "release.txt"], cwd=managed_source)
        subprocess.check_call(["git", "commit", "-m", "candidate"], cwd=managed_source,
                              stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "tag", "-a", "v-next", "-m", "next"], cwd=managed_source)
        allow = self.json_file("allow-build.json", {"paths": ["release.txt"]})
        return candidate.freeze(
            self.database, self.root, "repo", self.work_item, "candidate-1",
            allow, "implementer", "freeze-build",
        )

    def _build_fixture(self):
        frozen = self._freeze_for_build()
        executable = os.path.realpath(os.sys.executable)
        toolchain = {
            "protocolVersion": "AWB-PINNED-PYTHON-BUILD-v1", "toolchainId": "fixture",
            "python": {"canonicalExecutable": executable,
                       "executableSha256": candidate._file_sha(executable),
                       "implementation": "CPython", "version": "fixture-python"},
            "packages": {"setuptools": "fixture-setuptools", "wheel": "fixture-wheel"},
            "backend": "SETUPTOOLS_BDIST_WHEEL_SDIST",
            "environment": {"TZ": "UTC", "LC_ALL": "C", "SOURCE_DATE_EPOCH": "COMMIT_EPOCH"},
        }
        toolchain_file = self.json_file("toolchain-good.json", toolchain)
        original_check_output = candidate.subprocess.check_output
        original_check_call = candidate.subprocess.check_call

        def check_output(command, **kwargs):
            if command[:2] == [executable, "-c"]:
                return json.dumps(["CPython", "fixture-python", "fixture-setuptools",
                                   "fixture-wheel"]).encode("utf-8") + b"\n"
            return original_check_output(command, **kwargs)

        def check_call(command, **kwargs):
            if command and command[0] == executable and "bdist_wheel" in command:
                indexes = [index for index, value in enumerate(command)
                           if value == "--dist-dir"]
                self.assertEqual(2, len(indexes))
                assets = command[indexes[0] + 1]
                self.assertEqual([assets, assets],
                                 [command[index + 1] for index in indexes])
                self.assertEqual([
                    executable, "setup.py", "bdist_wheel", "--dist-dir", assets,
                    "sdist", "--dist-dir", assets,
                ], command)
                self.assertEqual(os.path.realpath(assets), assets)
                if not os.path.isabs(assets):
                    assets = os.path.join(kwargs["cwd"], assets)
                os.makedirs(assets, exist_ok=True)
                identity = {
                    "packageVersion": "0.3.1b7",
                    "sourceCommit": frozen_identity["headCommit"],
                    "sourceTree": frozen_identity["headTree"],
                    "sourceTag": "v-next",
                }
                wheel_path = os.path.join(assets, "package.whl")
                with zipfile.ZipFile(wheel_path, "w") as archive:
                    archive.writestr("package/module.py", "value = 1\n")
                    archive.writestr("agent_workboard/_build.py",
                                     "BUILD_IDENTITY = " + repr(identity) + "\n")
                sdist_path = os.path.join(assets, "package.tar.gz")
                with tarfile.open(sdist_path, "w:gz") as archive:
                    raw = b"value = 1\n"
                    info = tarfile.TarInfo("package/module.py")
                    info.size = len(raw)
                    archive.addfile(info, io.BytesIO(raw))
                    identity_raw = json.dumps(identity).encode("utf-8")
                    identity_info = tarfile.TarInfo(
                        "package/.awb-release-identity.json"
                    )
                    identity_info.size = len(identity_raw)
                    archive.addfile(identity_info, io.BytesIO(identity_raw))
                return 0
            return original_check_call(command, **kwargs)

        frozen_identity = candidate._load_json(candidate._candidate_manifest(
            self.root, self.work_item, "candidate-1"
        )[2])["frozen"]
        return toolchain, toolchain_file, check_output, check_call

    def test_build_event_fault_preserves_drifted_target_for_recovery(self):
        unused_toolchain, toolchain_file, check_output, check_call = self._build_fixture()
        managed = os.path.join(
            self.root, ".awb", "release-candidates", self.work_item, "managed"
        )
        target = os.path.join(managed, "active", "candidate-1", "builds", "build-drift")
        staging = os.path.join(managed, "staging", "candidate-1", "build-drift")
        journal_path = os.path.join(managed, "journal", "candidate-1", "build-drift.json")
        def fault(connection, event_type, request_id):
            with open(os.path.join(target, "intruder.txt"), "w",
                      encoding="utf-8") as handle:
                handle.write("foreign\n")
            raise RuntimeError("build event fault after target drift")

        before = candidate.lite.timeline(self.database, self.work_item)
        with mock.patch.object(candidate.subprocess, "check_output", side_effect=check_output), \
                mock.patch.object(candidate.subprocess, "check_call", side_effect=check_call), \
                mock.patch.object(candidate, "_candidate_materialized", side_effect=fault):
            with self.assertRaisesRegex(LiteError, "TARGET_TREE_DRIFT"):
                candidate.build(
                    self.database, self.root, "repo", self.work_item, "candidate-1",
                    toolchain_file, "implementer", "build-drift",
                )
        journal = candidate._load_json(journal_path, candidate.CANDIDATE_PROTOCOL)
        manifest = candidate._load_json(candidate._candidate_manifest(
            self.root, self.work_item, "candidate-1"
        )[2])
        self.assertEqual("RECOVERY_REQUIRED", journal["status"])
        self.assertEqual("FROZEN", manifest["lifecycle"])
        self.assertTrue(os.path.isfile(os.path.join(target, "intruder.txt")))
        self.assertFalse(os.path.lexists(staging))
        self.assertNotEqual(journal["afterTreeSha256"],
                            candidate._tree_fingerprint(target))
        self.assertEqual(before, candidate.lite.timeline(self.database, self.work_item))
        with mock.patch.object(candidate.subprocess, "check_output", side_effect=check_output):
            with self.assertRaisesRegex(LiteError, "destination already exists"):
                candidate.build(
                    self.database, self.root, "repo", self.work_item, "candidate-1",
                    toolchain_file, "implementer", "build-drift",
                )
        self.assertTrue(os.path.isfile(os.path.join(target, "intruder.txt")))

    def test_build_rejects_artifact_outside_canonical_managed_assets(self):
        unused_toolchain, toolchain_file, check_output, check_call = self._build_fixture()

        def out_of_bounds(command, **kwargs):
            result = check_call(command, **kwargs)
            if command and command[0] == os.path.realpath(os.sys.executable) and \
                    "bdist_wheel" in command:
                directory = os.path.join(kwargs["cwd"], "dist")
                os.makedirs(directory)
                with open(os.path.join(directory, "unexpected.whl"), "wb") as handle:
                    handle.write(b"unexpected")
            return result

        with mock.patch.object(candidate.subprocess, "check_output",
                               side_effect=check_output), mock.patch.object(
                                   candidate.subprocess, "check_call",
                                   side_effect=out_of_bounds):
            with self.assertRaisesRegex(
                    LiteError, "artifacts outside managed assets"):
                candidate.build(
                    self.database, self.root, "repo", self.work_item, "candidate-1",
                    toolchain_file, "implementer", "build-out-of-bounds",
                )
        manifest = candidate._load_json(candidate._candidate_manifest(
            self.root, self.work_item, "candidate-1"
        )[2])
        self.assertEqual("FROZEN", manifest["lifecycle"])
        journal_path = os.path.join(
            self.root, ".awb", "release-candidates", self.work_item, "managed",
            "journal", "candidate-1", "build-out-of-bounds.json",
        )
        self.assertEqual("RECOVERY_REQUIRED",
                         candidate._load_json(journal_path)["status"])

    def test_build_compensation_race_never_overwrites_staging(self):
        unused_toolchain, toolchain_file, check_output, check_call = self._build_fixture()
        managed = os.path.join(
            self.root, ".awb", "release-candidates", self.work_item, "managed"
        )
        target = os.path.join(managed, "active", "candidate-1", "builds", "build-race")
        staging = os.path.join(managed, "staging", "candidate-1", "build-race")
        journal_path = os.path.join(managed, "journal", "candidate-1", "build-race.json")
        exclusive_rename = candidate._rename_exclusive

        def fault(connection, event_type, request_id):
            raise RuntimeError("build event fault before compensation race")

        def race(source, destination):
            os.makedirs(destination)
            with open(os.path.join(destination, "competitor.txt"), "w",
                      encoding="utf-8") as handle:
                handle.write("foreign\n")
            return exclusive_rename(source, destination)

        before = candidate.lite.timeline(self.database, self.work_item)
        with mock.patch.object(candidate.subprocess, "check_output", side_effect=check_output), \
                mock.patch.object(candidate.subprocess, "check_call", side_effect=check_call), \
                mock.patch.object(candidate, "_candidate_materialized", side_effect=fault), \
                mock.patch.object(candidate, "_rename_exclusive", side_effect=race):
            with self.assertRaisesRegex(LiteError, "STAGING_DESTINATION_RACE"):
                candidate.build(
                    self.database, self.root, "repo", self.work_item, "candidate-1",
                    toolchain_file, "implementer", "build-race",
                )
        journal = candidate._load_json(journal_path, candidate.CANDIDATE_PROTOCOL)
        manifest = candidate._load_json(candidate._candidate_manifest(
            self.root, self.work_item, "candidate-1"
        )[2])
        self.assertEqual("RECOVERY_REQUIRED", journal["status"])
        self.assertEqual("FROZEN", manifest["lifecycle"])
        self.assertEqual(journal["afterTreeSha256"],
                         candidate._tree_fingerprint(target))
        with open(os.path.join(staging, "competitor.txt"),
                  encoding="utf-8") as handle:
            self.assertEqual("foreign\n", handle.read())
        self.assertEqual(before, candidate.lite.timeline(self.database, self.work_item))

    def test_build_and_postflight_commit_then_raise_delay_final(self):
        toolchain, toolchain_file, check_output, check_call = self._build_fixture()
        with mock.patch.object(candidate.subprocess, "check_output", side_effect=check_output), \
                mock.patch.object(candidate.subprocess, "check_call", side_effect=check_call), \
                self.commit_then_raise():
            built = candidate.build(
                self.database, self.root, "repo", self.work_item, "candidate-1",
                toolchain_file, "implementer", "build-good",
            )
        self.assertEqual(3, len(built["artifacts"]))
        build_root = os.path.join(
            self.root, ".awb", "release-candidates", self.work_item,
            "managed", "active", "candidate-1", "builds", "build-good",
        )
        self.assertFalse(os.path.lexists(os.path.join(build_root, "source", "dist")))
        self.assertEqual(
            ["SHA256SUMS", "package.tar.gz", "package.whl"],
            sorted(os.listdir(os.path.join(build_root, "assets"))),
        )
        with mock.patch.object(candidate.subprocess, "check_output", side_effect=check_output):
            self.assertEqual(built, candidate.build(
                self.database, self.root, "repo", self.work_item, "candidate-1",
                toolchain_file, "implementer", "build-good",
            ))
        build_journal = os.path.join(
            self.root, ".awb", "release-candidates", self.work_item,
            "managed", "journal", "candidate-1", "build-good.json",
        )
        self.assertEqual("COMMITTED", candidate._load_json(build_journal)["status"])
        changed_toolchain = dict(toolchain)
        changed_toolchain["toolchainId"] = "different-fixture"
        changed_toolchain_file = self.json_file(
            "toolchain-changed.json", changed_toolchain
        )
        built_timeline = candidate.lite.timeline(self.database, self.work_item)
        with self.assertRaisesRegex(LiteError, "different content"):
            candidate.build(
                self.database, self.root, "repo", self.work_item, "candidate-1",
                changed_toolchain_file, "implementer", "build-good",
            )
        self.assertEqual(built_timeline,
                         candidate.lite.timeline(self.database, self.work_item))
        managed_source = os.path.relpath(os.path.join(
            self.root, ".awb", "release-candidates", self.work_item,
            "managed", "active", "candidate-1", "source",
        ), self.root)
        def passing_checks(unused_root, unused_database, unused_source, phase, policy):
            return [{
                "checkId": entry["checkId"], "phase": phase,
                "result": "PASS", "resultDigest": "a" * 64,
                "covers": ["HC-1", "HC-5", "VP-1", "VP-2"],
            } for entry in verify._registered_check_plan(phase, policy)]
        with mock.patch.object(verify, "_run_registered_checks",
                               side_effect=passing_checks):
            verified = verify.run(
                self.database, self.root, "repo", self.work_item,
                managed_source, "implementer", "FINAL", "verify-release",
                candidate_id="candidate-1",
            )["receipt"]
        from agent_workboard.lite import open_database, get_work_item
        transition(
            self.database, self.work_item, "submit_implementation", "implementer",
            verify_receipt=verified["receiptId"], project_root=self.root,
            candidate_id="candidate-1",
            candidate_fingerprint=built["candidateFingerprint"],
        )
        connection = open_database(self.database)
        try:
            reviewed = candidate.release_submission_candidate(connection, self.work_item)
        finally:
            connection.close()
        expires = (datetime.datetime.now(datetime.timezone.utc) +
                   datetime.timedelta(hours=1)).replace(microsecond=0).isoformat()
        acquire_claim(self.database, self.work_item, self.work_item + "-T03",
                      "implementation-reviewer", "REVIEWER", expires)
        review = {"result": "PASS", "reviewerMode": "ORDINARY", "summary": "pass",
                  "findings": [], "resolvedFindingIds": [],
                  "nonBlockingSuggestions": [], "reviewedCandidate": reviewed,
                  "reviewedReceipt": {
                      "receiptId": verified["receiptId"],
                      "coreFingerprint": verified["coreFingerprint"],
                      "receiptFingerprint": verified["projection"]["receiptFingerprint"],
                      "candidate": verified["candidate"],
                  }}
        record_agent_review(self.database, self.work_item, "FINAL",
                            "implementation-reviewer", "APPROVED", review,
                            request_id="release-review")
        waiting = get_work_item(self.database, self.work_item)
        self.assertEqual("IMPLEMENTATION_COMPLETED", waiting["state"])
        self.assertEqual("WAITING_HUMAN", waiting["queue_state"])
        connection = open_database(self.database)
        try:
            events = candidate._events(connection, self.work_item)
        finally:
            connection.close()
        self.assertFalse(any(row["event_type"] == "AUTO_GATE_APPROVED" and
                             payload.get("stage") == "FINAL"
                             for row, payload in events))
        publication = candidate.publication_status(self.database, self.root, self.work_item)
        self.assertEqual("WAITING_HUMAN", publication["status"])
        connection = open_database(self.database)
        try:
            unused_ready_row, ready_payload = candidate._latest_event(
                connection, self.work_item, "PUBLICATION_READY"
            )
        finally:
            connection.close()
        authorization = {
            "protocolVersion": "AWB-PUBLICATION-AUTHORIZATION-v1", "repository": "repo",
            "version": "0.3.1b7", "tag": "v-next", "releaseBranch": "release/next",
            "title": "Preview", "candidateId": "candidate-1",
            "candidateFingerprint": built["candidateFingerprint"],
            "assets": built["artifacts"],
            "allowedActions": ["CREATE_IMMUTABLE_PRERELEASE",
                               "PUSH_EXACT_NON_FORCE_REFS",
                               "UPLOAD_EXACT_THREE_ASSETS"],
            "creationDecisionActor": "human",
            "prepublicationAttestationSha256": "a" * 64,
            "formalReview": {
                "reviewId": ready_payload["reviewId"],
                "reviewEventId": ready_payload["reviewEventId"],
                "reviewRequestId": ready_payload["reviewRequestId"],
            },
            "readyFingerprint": ready_payload["readyFingerprint"],
            "buildFingerprint": built["buildFingerprint"],
        }
        authorization_file = self.json_file("authorization.json", authorization)
        authorized = candidate.publication_authorize(
            self.database, self.root, self.work_item, "human", "candidate-1",
            built["candidateFingerprint"], authorization_file, "authorize-1",
        )
        ready = candidate.publication_status(self.database, self.root, self.work_item)
        self.assertEqual("REMOTE", ready["nextStep"]["riskClass"])
        from agent_workboard.lite import workflow_advance, workflow_status, timeline
        workflow = workflow_status(
            self.database, self.work_item, "operator", "REVIEWER", "repo",
            project_root=self.root,
        )
        self.assertEqual(("REFUSED", "REMOTE",
                          "EXECUTE_EXACT_AUTHORIZED_PUBLICATION"), (
            workflow["status"], workflow["nextStep"]["riskClass"],
            workflow["nextStep"]["action"],
        ))
        before_timeline = timeline(self.database, self.work_item)
        refused = workflow_advance(
            self.database, self.root, self.work_item, "operator", "REVIEWER",
            "repo", workflow["nextStep"]["fingerprint"], workflow["rowVersion"],
            "remote-must-not-advance",
        )
        self.assertEqual("REFUSED", refused["status"])
        self.assertEqual(before_timeline, timeline(self.database, self.work_item))
        retry_database = os.path.join(self.root, "retry.db")
        source_connection = sqlite3.connect(self.database)
        target_connection = sqlite3.connect(retry_database)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        retry_file = self.json_file("retry.json", {
            "protocolVersion": "AWB-PUBLICATION-RETRY-v1",
            "candidateId": "candidate-1",
            "candidateFingerprint": built["candidateFingerprint"],
            "readyFingerprint": ready["nextStep"]["arguments"]["readyFingerprint"],
            "authorizationFingerprint": authorized["authorizationFingerprint"],
            "reason": "remote action did not start",
            "remoteActionStarted": False, "partialState": "NONE",
        })
        retry = candidate.publication_retry(
            retry_database, self.root, self.work_item, "human",
            ready["nextStep"]["arguments"]["readyFingerprint"],
            retry_file, "retry-1",
        )
        self.assertEqual(("OK", 2), (
            retry["status"], retry["expectedNextReviewRound"],
        ))
        retry_replay = candidate.publication_retry(
            retry_database, self.root, self.work_item, "human",
            ready["nextStep"]["arguments"]["readyFingerprint"],
            retry_file, "retry-1",
        )
        self.assertEqual(retry, retry_replay)
        retry_connection = candidate.lite.open_database(retry_database)
        try:
            self.assertEqual(1, len(candidate.lite._review_history(
                retry_connection, self.work_item, "FINAL"
            )))
            self.assertEqual(
                ["NOT_STARTED", "NOT_STARTED"],
                [row[0] for row in retry_connection.execute(
                    "SELECT status FROM tasks WHERE work_item_id=? AND "
                    "owner_role IN ('IMPLEMENTER','REVIEWER') ORDER BY seq",
                    (self.work_item,),
                )],
            )
        finally:
            retry_connection.close()
        unused_root, unused_managed, manifest_path = candidate._candidate_manifest(
            self.root, self.work_item, "candidate-1"
        )
        frozen_identity = candidate._load_json(manifest_path)["frozen"]
        evidence_core = {
            "candidateFingerprint": built["candidateFingerprint"],
            "readyFingerprint": ready_payload["readyFingerprint"],
            "authorizationFingerprint": authorized["authorizationFingerprint"],
        }
        postflight = {
            "protocolVersion": "AWB-PUBLICATION-POSTFLIGHT-v1", "repository": "repo",
            "version": "0.3.1b7", "tag": "v-next",
            "refs": {"branchOid": frozen_identity["headCommit"],
                     "tagObject": frozen_identity["tagObject"],
                     "tagPeel": frozen_identity["tagPeel"]},
            "release": {"immutable": True, "draft": False, "prerelease": True,
                        "version": "0.3.1b7", "tag": "v-next", "title": "Preview"},
            "assets": built["artifacts"], "partialState": "NONE",
            "evidenceCore": evidence_core,
            "evidenceCoreSha256": candidate._sha(candidate._json(evidence_core)),
            "formalReview": authorization["formalReview"],
            "ready": {
                "readyFingerprint": ready_payload["readyFingerprint"],
                "candidateFingerprint": built["candidateFingerprint"],
            },
            "authorization": {
                "requestId": "authorize-1",
                "authorizationFingerprint": authorized["authorizationFingerprint"],
            },
            "workspace": {"status": "PASS"},
            "repair": {"status": "PASS"},
            "remoteStatus": "EXACT_ALREADY_PUBLISHED",
        }
        postflight_file = self.json_file("postflight.json", postflight)
        exact_status = candidate.publication_status(
            self.database, self.root, self.work_item, postflight_file
        )
        self.assertEqual("RUN_POST_PUBLICATION_VERIFY",
                         exact_status["nextStep"]["action"])

        with tempfile.TemporaryDirectory() as manual_parent:
            manual_root = os.path.join(manual_parent, "project")
            shutil.copytree(self.root, manual_root, symlinks=True)
            manual_database = os.path.join(manual_root, ".awb", "workboard.db")
            manual_connection = sqlite3.connect(manual_database)
            try:
                manual_connection.execute(
                    "UPDATE work_items SET human_gate_policy='MANUAL' WHERE work_item_id=?",
                    (self.work_item,),
                )
                manual_connection.commit()
            finally:
                manual_connection.close()
            manual_accepted = candidate.publication_postflight(
                manual_database, manual_root, self.work_item, "manual-operator",
                ready["nextStep"]["arguments"]["readyFingerprint"], "authorize-1",
                postflight, "manual-postflight",
            )
            self.assertEqual("WAITING_HUMAN",
                             get_work_item(manual_database, self.work_item)["queue_state"])
            self.assertEqual(manual_accepted, candidate.publication_postflight(
                manual_database, manual_root, self.work_item, "manual-operator",
                ready["nextStep"]["arguments"]["readyFingerprint"], "authorize-1",
                postflight, "manual-postflight",
            ))

        with tempfile.TemporaryDirectory() as fault_parent:
            fault_root = os.path.join(fault_parent, "project")
            shutil.copytree(self.root, fault_root, symlinks=True)
            fault_database = os.path.join(fault_root, ".awb", "workboard.db")
            original_materialized = candidate._candidate_materialized

            def postflight_fault(connection, event_type, request_id):
                if event_type == "PUBLICATION_POSTFLIGHT":
                    raise RuntimeError("postflight event fault")
                return original_materialized(connection, event_type, request_id)

            with mock.patch("agent_workboard.candidate._candidate_materialized",
                            side_effect=postflight_fault):
                with self.assertRaisesRegex(RuntimeError, "postflight event fault"):
                    candidate.publication_postflight(
                        fault_database, fault_root, self.work_item, "fault-operator",
                        ready["nextStep"]["arguments"]["readyFingerprint"], "authorize-1",
                        postflight, "fault-postflight",
                    )
            fault_manifest = candidate._load_json(candidate._candidate_manifest(
                fault_root, self.work_item, "candidate-1"
            )[2])
            self.assertEqual("BUILT", fault_manifest["lifecycle"])
            fault_accepted = candidate.publication_postflight(
                fault_database, fault_root, self.work_item, "fault-operator",
                ready["nextStep"]["arguments"]["readyFingerprint"], "authorize-1",
                postflight, "fault-postflight",
            )
            self.assertEqual(fault_accepted, candidate.publication_postflight(
                fault_database, fault_root, self.work_item, "fault-operator",
                ready["nextStep"]["arguments"]["readyFingerprint"], "authorize-1",
                postflight, "fault-postflight",
            ))

        with self.commit_then_raise():
            accepted = candidate.publication_postflight(
                self.database, self.root, self.work_item, "operator",
                ready["nextStep"]["arguments"]["readyFingerprint"], "authorize-1",
                postflight, "postflight-1",
            )
        self.assertEqual("OK", accepted["status"])
        accepted_replay = candidate.publication_postflight(
            self.database, self.root, self.work_item, "operator",
            ready["nextStep"]["arguments"]["readyFingerprint"], "authorize-1",
            postflight, "postflight-1",
        )
        self.assertEqual(accepted, accepted_replay)
        postflight_timeline = timeline(self.database, self.work_item)
        connection = open_database(self.database)
        try:
            extension = connection.execute(
                "SELECT payload_json FROM events WHERE work_item_id=? AND "
                "event_type='VERIFY_RECEIPT_EXTENDED' ORDER BY event_id DESC LIMIT 1",
                (self.work_item,),
            ).fetchone()
        finally:
            connection.close()
        extended = json.loads(extension[0])["receipt"]
        self.assertEqual((verified["receiptId"], verified["coreFingerprint"]),
                         (extended["receiptId"], extended["coreFingerprint"]))
        self.assertNotEqual(verified["projection"]["receiptFingerprint"],
                            extended["projection"]["receiptFingerprint"])
        with self.assertRaisesRegex(LiteError, "different content"):
            candidate.publication_postflight(
                self.database, self.root, self.work_item, "other-operator",
                ready["nextStep"]["arguments"]["readyFingerprint"], "authorize-1",
                postflight, "postflight-1",
            )
        changed_postflight = dict(postflight)
        changed_postflight["release"] = dict(postflight["release"])
        changed_postflight["release"]["title"] = "Different Preview"
        with self.assertRaisesRegex(LiteError, "different content"):
            candidate.publication_postflight(
                self.database, self.root, self.work_item, "operator",
                ready["nextStep"]["arguments"]["readyFingerprint"], "authorize-1",
                changed_postflight, "postflight-1",
            )
        self.assertEqual(postflight_timeline, timeline(self.database, self.work_item))
        self.assertEqual("FINAL_ACCEPTANCE_APPROVED",
                         get_work_item(self.database, self.work_item)["state"])


if __name__ == "__main__":
    unittest.main()
