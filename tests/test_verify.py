import argparse
import datetime
import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest import mock

from agent_workboard import cli
from agent_workboard import verify
from agent_workboard import lite
from agent_workboard import workflow
from agent_workboard.lite import LiteError


class VerifyPolicyTest(unittest.TestCase):
    def management(self, scope=None):
        return {"scope": scope or ["ordinary local implementation"]}

    def candidate(self):
        return {"kind": "ORDINARY", "candidateId": None,
                "candidateFingerprint": "a" * 64, "buildFingerprint": None,
                "sourceDescriptorDigest": "b" * 64, "source": "candidate"}

    def test_classifier_and_policy_floor_are_single_monotonic_authority(self):
        normal = verify.classify(self.management(), self.candidate(), ["README.md"])
        self.assertEqual("NORMAL", normal["riskClass"])
        default = verify.policy_decision(normal, {"profile": "AUTO"})
        self.assertFalse(default["overrideRequired"])
        stricter = verify.policy_decision(normal, {
            "revision": "FULL", "database": "DISPOSABLE_PLUS_LIVE_READ_ONLY",
        })
        self.assertFalse(stricter["overrideRequired"])
        downgraded = verify.policy_decision(normal, {"revision": "NONE"})
        self.assertTrue(downgraded["overrideRequired"])
        with self.assertRaisesRegex(LiteError, "VERIFY_POLICY_BELOW_HARD_FLOOR"):
            verify.policy_decision(normal, {"runs": {"core": 0}})

    def test_self_host_operation_enum_is_closed(self):
        self.assertEqual({
            "VERIFY_FINAL_RECORD",
            "SUBMIT_IMPLEMENTATION_WITH_RECEIPT",
            "FORMAL_IMPLEMENTATION_REVIEW",
            "PUBLICATION_POSTFLIGHT",
        }, set(workflow.SELF_HOST_BOOTSTRAP_OPERATIONS))
        for operation in workflow.SELF_HOST_BOOTSTRAP_OPERATIONS:
            self.assertEqual(
                operation, workflow.assert_self_host_bootstrap_operation(operation)
            )
        with self.assertRaisesRegex(ValueError, "SELF_HOST_BOOTSTRAP_REFUSED"):
            workflow.assert_self_host_bootstrap_operation("CANDIDATE_BUILD")

    def test_b8_on_b7_cli_dispatch_denies_non_allowlisted_mutation_preflight(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".awb"))
            database = os.path.join(root, ".awb", "workboard.db")
            lite.initialize_database(database)
            with open(os.path.join(root, ".awb", "config.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({
                    "configVersion": 1, "database": ".awb/workboard.db",
                    "projectId": "project-372e08efb9354d13bbc57fb2f3405e94",
                    "repositoryKey": "agent-workboard-ops",
                    "requiredPackageVersion": "0.3.1b7",
                    "requiredSourceCommit":
                    "c7380024f9b0efcd3167cebcd9915b2f1d85d13a",
                    "requiredSourceTree":
                    "102cc5ef820b9f7d42166012c67dfd8708e61419",
                    "requiredSourceTag": "v0.3.1b7",
                    "runtimeMode": "stable", "usagePolicy": "OFF",
                }, handle)
            denied = argparse.Namespace(
                project=root, command="candidate", candidate_command="build"
            )
            with self.assertRaisesRegex(LiteError, "SELF_HOST_BOOTSTRAP_REFUSED"):
                cli._guard_self_host_dispatch(denied)
            allowed = argparse.Namespace(
                project=root, command="verify", verify_command="run",
                phase="FINAL", candidate="candidate-1", work_item="AWB-027",
            )
            with mock.patch(
                    "agent_workboard.candidate.assert_self_host_operation",
                    return_value="CANDIDATE") as guarded:
                cli._guard_self_host_dispatch(allowed)
            guarded.assert_called_once()
            self.assertEqual("VERIFY_FINAL_RECORD", guarded.call_args[0][3])

    def test_init_bypasses_existing_project_identity_guard(self):
        with tempfile.TemporaryDirectory() as root:
            project = os.path.join(root, "fresh-uninitialized-project")
            args = argparse.Namespace(project=project, command="init")
            with mock.patch(
                    "agent_workboard.cli._project_identity",
                    side_effect=AssertionError("init read existing project identity"),
            ) as identity:
                cli._guard_self_host_dispatch(args)
            identity.assert_not_called()

    def test_auto_classifies_preview_and_cross_version_without_new_hard_class(self):
        preview = verify.classify(
            self.management(), self.candidate(), ["src/agent_workboard/candidate.py"]
        )
        cross = verify.classify(
            self.management(["disposable upgrade and rollback verification"]),
            self.candidate(), ["src/agent_workboard/project.py"],
        )
        self.assertEqual("PREVIEW", preview["riskClass"])
        self.assertEqual("CROSS_VERSION", cross["riskClass"])
        managed = dict(self.candidate(), kind="MANAGED_RELEASE",
                       candidateId="release-1", buildFingerprint="c" * 64)
        workspace_handoff = verify.classify(
            self.management(["发布 Preview 并升级本工作区"]), managed,
            ["src/agent_workboard/project.py", "CHANGELOG.md"],
        )
        self.assertEqual("PREVIEW", workspace_handoff["riskClass"])
        actual_migration = verify.classify(
            self.management(["release migration implementation"]), managed,
            ["src/agent_workboard/migration.py"],
        )
        self.assertEqual("CROSS_VERSION", actual_migration["riskClass"])
        self.assertEqual("PREVIEW", verify.policy_decision(
            preview, {"profile": "AUTO"})["effective"]["profile"])
        with self.assertRaisesRegex(LiteError, "VERIFY_POLICY_BELOW_HARD_FLOOR"):
            verify.policy_decision(preview, {"profile": "NORMAL"})

    def test_every_stricter_policy_dimension_changes_registered_work(self):
        classifier = verify.classify(self.management(), self.candidate(), [])
        baseline = verify.policy_decision(classifier)["effective"]
        baseline_plan = verify._registered_check_plan("REVISION", baseline)
        stricter = {
            "database": "DISPOSABLE_PLUS_LIVE_READ_ONLY",
            "revision": "FULL", "final": "ALWAYS_RUN",
            "profile": "PREVIEW", "runs": {"core": 2},
            "rebuild": "ALWAYS", "compatibility": "FULL_GRAPH",
            "identityEvidence": "EXACT_PATHS_AND_ASSET_HASHES",
            "previewCost": "EXTENDED",
        }
        for name, value in sorted(stricter.items()):
            requested = dict(baseline)
            requested["runs"] = dict(baseline["runs"])
            requested[name] = value
            plan = verify._registered_check_plan("REVISION", requested)
            self.assertNotEqual(
                [(entry["checkId"], entry["kind"], entry.get("command"))
                 for entry in baseline_plan],
                [(entry["checkId"], entry["kind"], entry.get("command"))
                 for entry in plan], name,
            )
            self.assertTrue(any(name in entry["dimensions"] for entry in plan), name)

    def test_source_identity_is_content_bound_and_rejects_links(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "candidate")
            os.makedirs(source)
            path = os.path.join(source, "value.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("one")
            first = verify.source_identity(root, "candidate")
            second = verify.source_identity(root, "candidate")
            self.assertEqual(first, second)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("two")
            self.assertNotEqual(first["candidateFingerprint"], verify.source_identity(
                root, "candidate")["candidateFingerprint"])
            os.symlink(path, os.path.join(source, "linked"))
            with self.assertRaisesRegex(LiteError, "linked file"):
                verify.source_identity(root, "candidate")

    def test_artifact_scan_is_verifier_owned(self):
        with tempfile.TemporaryDirectory() as root:
            wheel = os.path.join(root, "safe.whl")
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("package/value.py", "VALUE = 1\n")
            self.assertEqual(1, verify.scan_artifact(wheel)["memberCount"])
            unsafe = os.path.join(root, "unsafe.whl")
            with zipfile.ZipFile(unsafe, "w") as archive:
                archive.writestr("../escape", "value")
            with self.assertRaisesRegex(LiteError, "unsafe member"):
                verify.scan_artifact(unsafe)

    def _management(self, work_item_id):
        return {
            "contractVersion": "AWB-WORKITEM-MGMT-v1",
            "templateContractVersion": "AWB-MANAGEMENT-v1",
            "scope": ["ordinary local implementation"],
            "outOfScope": ["remote writes"],
            "authorization": {"allowed": ["local tests"],
                              "forbidden": ["remote writes"]},
            "safetyConstraints": ["do not weaken tests"],
            "tasks": [
                {"taskId": work_item_id + "-T01", "seq": 1,
                 "title": "plan", "ownerRole": "PLANNER", "required": True,
                 "acceptance": ["planned"],
                 "closureEvidenceRequired": ["plan"]},
                {"taskId": work_item_id + "-T02", "seq": 2,
                 "title": "implement", "ownerRole": "IMPLEMENTER",
                 "required": True, "acceptance": ["implemented"],
                 "closureEvidenceRequired": ["receipt"]},
                {"taskId": work_item_id + "-T03", "seq": 3,
                 "title": "review", "ownerRole": "REVIEWER", "required": True,
                 "acceptance": ["reviewed"],
                 "closureEvidenceRequired": ["review"]},
            ],
            "acceptance": [{"id": "AC-001", "criterion": "verified"}],
            "closure": [{"id": "CL-001", "criterion": "receipt"}],
        }

    def _final_receipt(self, root, request_id="verify-final-001"):
        database = os.path.join(root, "workboard.db")
        source = os.path.join(root, "candidate")
        os.makedirs(source)
        with open(os.path.join(source, "value.py"), "w", encoding="utf-8") as handle:
            handle.write("VALUE = 1\n")
        lite.initialize_database(database)
        lite.create_work_item(
            database, "AWB-VFY", "AWB", "verify fixture",
            management=self._management("AWB-VFY"), human_review="MANUAL",
        )
        checks = [{
            "checkId": "registered-unittest-final", "phase": "FINAL",
            "result": "PASS", "resultDigest": "a" * 64,
            "covers": ["HC-1", "HC-5", "VP-1", "VP-2"],
        }]
        with mock.patch.object(verify, "_run_registered_checks", return_value=checks), \
                mock.patch("agent_workboard.candidate._runtime_guard"):
            result = verify.run(
                database, root, "verify-repository", "AWB-VFY", "candidate",
                "implementer", "FINAL", request_id,
            )
        return database, result

    def test_final_receipt_is_candidate_bound_replay_safe_and_status_visible(self):
        with tempfile.TemporaryDirectory() as root:
            database, result = self._final_receipt(root)
            receipt = result["receipt"]
            self.assertEqual(verify.RECEIPT_PROTOCOL,
                             receipt["protocolVersion"])
            self.assertEqual(["AC-001"], [
                entry["acceptanceId"]
                for entry in receipt["core"]["acceptanceTrace"]
            ])
            self.assertIn("AC-001", receipt["core"]["checks"][0]["covers"])
            self.assertEqual(sorted(verify.DEFAULT_POLICY),
                             sorted(receipt["core"]["policyCoverage"]))
            connection = lite.open_database(database)
            try:
                current = verify.validate_current_receipt(
                    connection, root, "AWB-VFY", receipt["receiptId"], True,
                )
            finally:
                connection.close()
            self.assertEqual(receipt["coreFingerprint"],
                             current["coreFingerprint"])
            status = verify.status(database, root, "AWB-VFY", "candidate")
            self.assertEqual(receipt["receiptId"],
                             status["currentReceipt"]["receiptId"])
            with mock.patch.object(verify, "_run_registered_checks") as checks, \
                    mock.patch("agent_workboard.candidate._runtime_guard"):
                replay = verify.run(
                    database, root, "verify-repository", "AWB-VFY",
                    "candidate", "implementer", "FINAL", "verify-final-001",
                )
            checks.assert_not_called()
            self.assertEqual(result, replay)
            with open(os.path.join(root, "candidate", "value.py"), "w",
                      encoding="utf-8") as handle:
                handle.write("VALUE = 2\n")
            connection = lite.open_database(database)
            try:
                with self.assertRaisesRegex(LiteError, "VERIFY_CANDIDATE_DRIFT"):
                    verify.validate_current_receipt(
                        connection, root, "AWB-VFY", receipt["receiptId"], True,
                    )
            finally:
                connection.close()

    def test_management_classifier_and_acceptance_coverage_drift_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            database, result = self._final_receipt(root, "verify-final-drift-001")
            receipt = result["receipt"]
            amended = self._management("AWB-VFY")
            amended["scope"] = ["cross-version migration and rollback implementation"]
            lite.amend_management(
                database, "AWB-VFY", "human", amended, "risk scope changed",
                request_id="management-risk-drift-001",
            )
            connection = lite.open_database(database)
            try:
                before = connection.execute("SELECT count(*) FROM events").fetchone()[0]
                with self.assertRaisesRegex(LiteError, "VERIFY_CLASSIFIER_DRIFT"):
                    verify.validate_current_receipt(
                        connection, root, "AWB-VFY", receipt["receiptId"], True,
                    )
                self.assertEqual(before, connection.execute(
                    "SELECT count(*) FROM events").fetchone()[0])
            finally:
                connection.close()

        for suffix, acceptance in (
                ("changed", [{"id": "AC-001", "criterion": "changed criterion"}]),
                ("extra", [{"id": "AC-001", "criterion": "verified"},
                           {"id": "AC-002", "criterion": "additional criterion"}])):
            with tempfile.TemporaryDirectory() as root:
                database, result = self._final_receipt(
                    root, "verify-final-acceptance-" + suffix
                )
                amended = self._management("AWB-VFY")
                amended["acceptance"] = acceptance
                lite.amend_management(
                    database, "AWB-VFY", "human", amended,
                    "acceptance changed", request_id="acceptance-" + suffix,
                )
                connection = lite.open_database(database)
                try:
                    with self.assertRaisesRegex(
                            LiteError, "VERIFY_RECEIPT_.*(COVERAGE|DRIFT)"):
                        verify.validate_current_receipt(
                            connection, root, "AWB-VFY",
                            result["receipt"]["receiptId"], True,
                        )
                finally:
                    connection.close()

        with tempfile.TemporaryDirectory() as root:
            database, result = self._final_receipt(root, "verify-final-cover-001")
            connection = lite.open_database(database)
            try:
                row = connection.execute(
                    "SELECT event_id,payload_json FROM events WHERE event_type="
                    "'VERIFY_RECEIPT_RECORDED' ORDER BY event_id DESC LIMIT 1"
                ).fetchone()
                payload = json.loads(row["payload_json"])
                payload["receipt"]["core"]["checks"][0]["covers"].remove("AC-001")
                connection.execute("UPDATE events SET payload_json=? WHERE event_id=?", (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    row["event_id"],
                ))
                connection.commit()
                with self.assertRaisesRegex(
                        LiteError, "VERIFY_RECEIPT_ACCEPTANCE_COVERAGE_MISMATCH"):
                    verify.validate_current_receipt(
                        connection, root, "AWB-VFY",
                        result["receipt"]["receiptId"], True,
                    )
            finally:
                connection.close()

    def test_cli_run_binds_policy_and_receipt_inputs_to_verify_authority(self):
        args = argparse.Namespace(
            project=".", verify_command="run", work_item="AWB-VFY",
            source="candidate", candidate=None, agent="implementer", phase="FINAL",
            request_id="verify-final-002", orchestrator_id="orch",
            orchestrator_generation=1, human_override_request_id=None,
            addressed_finding=["F-001"], known_issue=["I-001"],
            database_policy=None, revision_policy="FULL", final_policy=None,
            profile="AUTO", runs=["core=2"], rebuild=None,
            compatibility=None, identity_evidence=None, preview_cost=None,
            ready_fingerprint=None, authorization_request_id=None,
        )
        expected = {"protocolVersion": verify.RECEIPT_PROTOCOL,
                    "status": "PASS"}
        config = {"repositoryKey": "repository"}
        with mock.patch.object(cli, "_project_identity",
                               return_value=("/project", config, "/project/db")), \
                mock.patch.object(cli.verify, "run", return_value=expected) as run, \
                mock.patch.object(cli, "_print"):
            self.assertEqual(0, cli._verify_command(args))
        run.assert_called_once_with(
            "/project/db", "/project", "repository", "AWB-VFY",
            "candidate", "implementer", "FINAL", "verify-final-002",
            "orch", 1, {"revision": "FULL", "profile": "AUTO",
                        "runs": {"core": 2}}, None, ["F-001"], ["I-001"],
            None,
        )

    def test_registered_runner_denies_live_database_aliases_and_child_writes(self):
        with tempfile.TemporaryDirectory() as root:
            awb = os.path.join(root, ".awb")
            os.makedirs(os.path.join(
                awb, "release-candidates", "AWB-VFY", "managed", "staging"
            ))
            os.makedirs(os.path.join(awb, "backup-current"))
            database = os.path.join(awb, "workboard.db")
            lite.initialize_database(database)
            source = os.path.join(root, "malicious-candidate")
            os.makedirs(os.path.join(source, "tests"))
            with open(os.path.join(source, "tests", "__init__.py"), "w",
                      encoding="utf-8") as handle:
                handle.write("")
            malicious = r'''
import json, os, sqlite3, subprocess, sys, tempfile, unittest

class DenyProbe(unittest.TestCase):
    def denied(self, call):
        with self.assertRaises((PermissionError, OSError)):
            call()

    def test_all_live_write_routes_are_denied_before_mutation(self):
        paths = json.loads(os.environ["AWB_VERIFY_DENY_PATHS"])
        database, wal, shm = paths[:3]
        self.denied(lambda: open(database, "wb"))
        relative = os.path.relpath(database, os.getcwd())
        self.denied(lambda: open(relative, "ab"))
        self.denied(lambda: open(wal, "wb"))
        self.denied(lambda: os.open(shm, os.O_CREAT | os.O_WRONLY))
        self.denied(lambda: sqlite3.connect(database))
        self.denied(lambda: sqlite3.connect("file:" + database + "?mode=rwc", uri=True))
        connection = sqlite3.connect(
            "file:" + database + "?mode=ro&immutable=1", uri=True
        )
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        connection.close()
        disposable = tempfile.mkdtemp()
        ordinary = os.path.join(disposable, "ordinary")
        open(ordinary, "w").close()
        self.denied(lambda: os.rename(ordinary, database))
        self.denied(lambda: os.replace(ordinary, database))
        self.denied(lambda: os.unlink(database))
        self.denied(lambda: os.truncate(database, 0))
        self.denied(lambda: os.link(database, os.path.join(disposable, "hard")))
        self.denied(lambda: os.symlink(database, os.path.join(disposable, "sym")))
        child = subprocess.run([
            sys.executable, "-c", "open(" + repr(database) + ", 'wb').close()"
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(0, child.returncode)
        for protected in paths[3:]:
            self.denied(lambda value=protected: open(
                os.path.join(value, "intruder"), "wb"
            ))
'''
            with open(os.path.join(source, "tests", "test_verify.py"), "w",
                      encoding="utf-8") as handle:
                handle.write(malicious)
            before = verify._path_snapshot(verify._live_deny_paths(root, database))
            checks = verify._run_registered_checks(root, database, source, "REVISION", {
                "runs": {"core": 1},
                "database": "DISPOSABLE_PLUS_LIVE_READ_ONLY",
                "identityEvidence": "EXACT_CHANGED_PATHS",
            })
            after = verify._path_snapshot(verify._live_deny_paths(root, database))
            self.assertEqual(
                ["registered-unittest-revision", "registered-live-read-only",
                 "registered-identity-evidence"],
                [check["checkId"] for check in checks],
            )
            self.assertTrue(all(check["result"] == "PASS" for check in checks))
            self.assertEqual(before, after)

    def test_cli_post_publication_uses_stdin_managed_runtime_and_current_receipt(self):
        args = argparse.Namespace(
            project=".", verify_command="run", work_item="AWB-REL",
            source="managed/source", candidate="candidate-1", agent="operator",
            phase="POST_PUBLICATION", request_id="post-001",
            ready_fingerprint="r" * 64,
            authorization_request_id="authorize-001",
        )
        publication = {"protocolVersion": "AWB-MUTATION-RECEIPT-v1",
                       "status": "OK"}
        receipt = {"protocolVersion": verify.RECEIPT_PROTOCOL,
                   "receiptId": "receipt-1"}
        connection = mock.Mock()
        with mock.patch.object(cli, "_project_identity", return_value=(
                "/project", {"repositoryKey": "repository"}, "/project/db")), \
                mock.patch.object(cli.candidate, "publication_postflight",
                                  return_value=publication) as postflight, \
                mock.patch.object(cli.lite, "open_database",
                                  return_value=connection), \
                mock.patch.object(cli.verify, "validate_current_receipt",
                                  return_value=receipt), \
                mock.patch.object(cli, "_print") as printed, \
                mock.patch.object(cli.sys, "stdin",
                                  new=io.StringIO('{"remote":"facts"}')):
            self.assertEqual(0, cli._verify_command(args))
        postflight.assert_called_once_with(
            "/project/db", "/project", "AWB-REL", "operator", "r" * 64,
            "authorize-001", {"remote": "facts"}, "post-001",
        )
        printed_value = printed.call_args[0][0]
        self.assertEqual(("PASS", receipt, publication), (
            printed_value["status"], printed_value["receipt"],
            printed_value["publicationReceipt"],
        ))

    def test_human_policy_downgrade_is_floor_bound_one_shot_and_replay_safe(self):
        with tempfile.TemporaryDirectory() as root:
            database = os.path.join(root, "workboard.db")
            source = os.path.join(root, "candidate")
            os.makedirs(source)
            with open(os.path.join(source, "value.py"), "w",
                      encoding="utf-8") as handle:
                handle.write("VALUE = 1\n")
            lite.initialize_database(database)
            lite.create_work_item(
                database, "AWB-VFY", "AWB", "override fixture",
                management=self._management("AWB-VFY"), human_review="MANUAL",
            )
            checks = [{
                "checkId": "registered-unittest-revision", "phase": "REVISION",
                "result": "PASS", "resultDigest": "a" * 64,
                "covers": ["HC-1", "HC-5", "VP-1", "VP-2"],
            }]
            with mock.patch.object(verify, "_run_registered_checks",
                                   return_value=checks), mock.patch(
                                       "agent_workboard.candidate._runtime_guard"):
                observation = verify.run(
                    database, root, "repository", "AWB-VFY", "candidate",
                    "implementer", "REVISION", "verify-observation-001",
                )["observation"]
            expires = (datetime.datetime.now(datetime.timezone.utc) +
                       datetime.timedelta(hours=1)).replace(
                           microsecond=0).isoformat()
            requested = {"revision": "NONE"}
            grant = verify.grant_override(
                database, root, "AWB-VFY", "human",
                observation["candidate"]["candidateFingerprint"], "FINAL",
                "reduce revision cost", expires, "override-001", requested,
            )
            self.assertEqual("NONE", grant["effectivePolicy"]["revision"])
            self.assertEqual(grant, verify.grant_override(
                database, root, "AWB-VFY", "human",
                observation["candidate"]["candidateFingerprint"], "FINAL",
                "reduce revision cost", expires, "override-001", requested,
            ))
            final_checks = [dict(checks[0], checkId="registered-unittest-final",
                                 phase="FINAL")]
            with mock.patch.object(verify, "_run_registered_checks",
                                   return_value=final_checks), mock.patch(
                                       "agent_workboard.candidate._runtime_guard"):
                accepted = verify.run(
                    database, root, "repository", "AWB-VFY", "candidate",
                    "implementer", "FINAL", "verify-final-downgrade-001",
                    requested_policy=requested,
                    override_request_id="override-001",
                )
                replay = verify.run(
                    database, root, "repository", "AWB-VFY", "candidate",
                    "implementer", "FINAL", "verify-final-downgrade-001",
                    requested_policy=requested,
                    override_request_id="override-001",
                )
                with self.assertRaisesRegex(LiteError,
                                             "VERIFY_OVERRIDE_ALREADY_CONSUMED"):
                    verify.run(
                        database, root, "repository", "AWB-VFY", "candidate",
                        "implementer", "FINAL", "verify-final-downgrade-002",
                        requested_policy=requested,
                        override_request_id="override-001",
                    )
            self.assertEqual(accepted, replay)
            with self.assertRaisesRegex(LiteError,
                                         "VERIFY_POLICY_BELOW_HARD_FLOOR"):
                verify.grant_override(
                    database, root, "AWB-VFY", "human",
                    observation["candidate"]["candidateFingerprint"], "FINAL",
                    "below floor", expires, "override-below-floor",
                    {"runs": {"core": 0}},
                )

    def test_verifier_is_single_policy_authority_and_duplicate_gates_are_deleted(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertFalse(os.path.exists(os.path.join(root, "tools", "release_gate.py")))
        self.assertFalse(os.path.exists(os.path.join(root, "tests", "test_release_gate.py")))
        python_files = []
        package = os.path.join(root, "src", "agent_workboard")
        for directory, unused_names, files in os.walk(package):
            for name in files:
                if name.endswith(".py"):
                    python_files.append(os.path.join(directory, name))
        definitions = {"POLICY_ORDER =": [], "DEFAULT_POLICY =": [],
                       "def classify(": []}
        for path in python_files:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for marker in definitions:
                if marker in source:
                    definitions[marker].append(os.path.relpath(path, root))
        self.assertEqual({key: ["src/agent_workboard/verify.py"]
                          for key in definitions}, definitions)
        pairs = (
            (".codex/skills/awb-orchestrator/SKILL.md",
             "src/agent_workboard/resources/codex/skills/awb-orchestrator/SKILL.md"),
            (".codex/agents/implementer.toml",
             "src/agent_workboard/resources/codex/agents/implementer.toml"),
            (".codex/agents/reviewer.toml",
             "src/agent_workboard/resources/codex/agents/reviewer.toml"),
        )
        for first, second in pairs:
            with open(os.path.join(root, first), "rb") as left, \
                    open(os.path.join(root, second), "rb") as right:
                self.assertEqual(left.read(), right.read())
        with open(os.path.join(root, "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(verify.RECEIPT_PROTOCOL,
                         manifest["verificationProtocol"])
        for relative in (
                "src/agent_workboard/cli.py", "src/agent_workboard/lite.py",
                "src/agent_workboard/resources/spec/mvp_lite_v1_1/workflow.yaml"):
            with open(os.path.join(root, relative), encoding="utf-8") as handle:
                text = handle.read()
            for removed in ("--quality-file", "--local-tests-passed",
                            "qualityBaseline", "implementation_quality_baseline"):
                self.assertNotIn(removed, text)


if __name__ == "__main__":
    unittest.main()
