import ast
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile


TEMPLATES = (
    "README.md", "awb-template.md", "fe-template.md",
    "remediation-plan-template.md", "test-issue-template.md",
    "test-issue-trigger-rules.md", "usage-observation-template.md", "wa-template.md",
)
RUNBOOK = os.path.join(".codex", "skills", "awb-orchestrator", "references",
                       "upgrade-and-rollback.md")
PACKAGE_RUNBOOK = os.path.join("src", "agent_workboard", "resources", "codex", "skills",
                               "awb-orchestrator", "references", "upgrade-and-rollback.md")
RUNBOOK_SECTIONS = (
    "APPLIES", "DOES_NOT_APPLY", "AUTHORITY", "PREFLIGHT_FIRST", "RESULT_READY",
    "RESULT_NO_OP", "RESULT_REFUSED", "MANDATORY_STOP", "EVIDENCE", "POST_UPGRADE",
    "ROLLBACK", "ROLLBACK_VERIFY", "RESULT_BLOCKED", "NEXT_STEP_ARGUMENTS",
)


class ReleaseBuildTest(unittest.TestCase):
    def _wheel_from_build(self, repository, destination):
        build = os.path.join(repository, "build", "lib")
        files = {}
        for base, directories, names in os.walk(build):
            directories.sort()
            for name in sorted(names):
                path = os.path.join(base, name)
                with open(path, "rb") as handle:
                    files[os.path.relpath(path, build).replace(os.sep, "/")] = handle.read()
        prefix = "agent_workboard-0.3.1b2.dist-info"
        files[prefix + "/METADATA"] = b"Metadata-Version: 2.1\nName: agent-workboard\nVersion: 0.3.1b2\n\n"
        files[prefix + "/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: awb-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        files[prefix + "/entry_points.txt"] = b"[console_scripts]\nawb = agent_workboard.cli:main\n"
        records = []
        for name, raw in sorted(files.items()):
            digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
            records.append("{0},sha256={1},{2}".format(name, digest, len(raw)))
        record_name = prefix + "/RECORD"
        files[record_name] = ("\n".join(records + [record_name + ",,"]) + "\n").encode("utf-8")
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, raw in sorted(files.items()):
                info = zipfile.ZipInfo(name, (2000, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, raw)

    def _assert_runbook_semantics(self, raw):
        text = raw.decode("utf-8")
        self.assertIn("AWB-UPGRADE-RUNBOOK-v1", text)
        self.assertIn("AWB-UPGRADE-v1", text)
        self.assertIn("exactly one non-empty `nextStep`", text)
        self.assertIn('`{"action":"NONE","arguments":{}}`', text)
        self.assertIn("exact `rollbackManifest`", text)
        self.assertIn("awb upgrade --check --project", text)
        self.assertIn("awb upgrade --project <project> --rollback", text)
        for identity in ("`0.3.1b1/v0.3.1b1`", "`0.3.1b2/v0.3.1b2`"):
            self.assertIn(identity, text)
        self.assertIn("database pre/post SHA-256", text)
        self.assertIn("usageSchemaVersion=AWB-USAGE-v1", text)
        self.assertIn("orchestratorSchemaVersion=AWB-ORCHESTRATOR-v1", text)
        self.assertIn("gatePolicySchemaVersion=AWB-AUTO-GATE-v1", text)
        for section in RUNBOOK_SECTIONS:
            self.assertIn("## " + section, text)

    def _release_repository(self, root):
        source = os.path.dirname(os.path.dirname(__file__))
        repository = os.path.join(root, "source")
        shutil.copytree(source, repository, ignore=shutil.ignore_patterns(
            ".git", ".awb", "build", "dist", "*.egg-info", "__pycache__", "*.pyc"))
        subprocess.check_call(["git", "init"], cwd=repository, stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "config", "user.name", "release-test"], cwd=repository)
        subprocess.check_call(["git", "config", "user.email", "release-test@example.invalid"], cwd=repository)
        subprocess.check_call(["git", "add", "--", ".codex", ".gitignore", "CHANGELOG.md",
                               "CONTRIBUTING.md", "LICENSE", "README.md", "docs", "manifest.json",
                               "pyproject.toml", "setup.py", "src", "tests", "tools"], cwd=repository)
        environment = dict(os.environ)
        environment.update({"GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"})
        subprocess.check_call(["git", "commit", "-m", "release test"], cwd=repository,
                              env=environment, stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "tag", "-a", "v0.3.1b2", "-m", "preview"], cwd=repository)
        return repository

    def _identity(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return ast.literal_eval(handle.read().split("=", 1)[1].strip())

    def test_public_and_spec_manifests_match_candidate_bytes(self):
        repository = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(repository, "manifest.json"), "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual("AWB-018-public-v1", manifest["manifestVersion"])
        expected_paths = set(subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=repository,
        ).decode("utf-8").splitlines()) - {"manifest.json"}
        actual_paths = set(entry["path"] for entry in manifest["files"])
        self.assertEqual(expected_paths, actual_paths)
        for entry in manifest["files"]:
            with open(os.path.join(repository, entry["path"]), "rb") as handle:
                self.assertEqual(entry["sha256"], hashlib.sha256(handle.read()).hexdigest())

        spec_root = os.path.join(repository, "src", "agent_workboard", "resources", "spec",
                                 "mvp_lite_v1_1")
        with open(os.path.join(spec_root, "manifest.json"), "r", encoding="utf-8") as handle:
            spec = json.load(handle)
        self.assertEqual("MVP-LITE-v1 + AWB-USAGE-v1 + AWB-ORCHESTRATOR-v1 + AWB-AUTO-GATE-v1",
                         spec["databaseSchemaVersion"])
        self.assertEqual(52, spec["composition"]["acceptanceCaseCount"])
        for entry in spec["artifacts"]:
            with open(os.path.join(spec_root, entry["path"]), "rb") as handle:
                self.assertEqual(entry["sha256"], hashlib.sha256(handle.read()).hexdigest())

        with open(os.path.join(spec_root, "workflow.yaml"), "r", encoding="utf-8") as handle:
            workflow = handle.read()
        plan = workflow.split("  agent_approve_plan:\n", 1)[1].split(
            "  agent_reject_plan:\n", 1)[0]
        self.assertIn(
            "to: {AUTO_ON_PASS: PLAN_REVIEW_APPROVED, MANUAL: PLAN_REVIEW_PENDING}", plan)
        for prerequisite in (
                "mode_STANDARD", "queue_CLAIMED", "active_independent_reviewer_claim",
                "management_envelope_present", "latest_independent_plan_review_PASS",
                "open_plan_findings_zero", "held_reason_absent", "blocked_reason_absent"):
            self.assertIn(prerequisite, plan)
        self.assertIn("stage: PLAN", plan)
        final = workflow.split("  agent_approve_final:\n", 1)[1].split(
            "  agent_reject_final:\n", 1)[0]
        self.assertIn(
            "to: {AUTO_ON_PASS: FINAL_ACCEPTANCE_APPROVED, MANUAL: IMPLEMENTATION_COMPLETED}",
            final)
        for prerequisite in (
                "mode_STANDARD", "queue_CLAIMED", "active_independent_reviewer_claim",
                "management_envelope_present", "latest_independent_final_review_PASS",
                "open_implementation_findings_zero", "held_reason_absent",
                "blocked_reason_absent", "required_tasks_completed",
                "implementation_quality_baseline_PASS"):
            self.assertIn(prerequisite, final)
        self.assertIn("stage: FINAL", final)

    def test_tagged_build_and_no_git_sdist_preserve_identity_and_templates(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._release_repository(temporary)
            environment = dict(os.environ)
            environment["SOURCE_DATE_EPOCH"] = "946684800"
            subprocess.check_call([sys.executable, "setup.py", "build_py"], cwd=repository, env=environment,
                                  stdout=subprocess.DEVNULL)
            direct_identity = self._identity(os.path.join(repository, "build", "lib", "agent_workboard", "_build.py"))
            self.assertEqual("0.3.1b2", direct_identity["packageVersion"])
            self.assertEqual("v0.3.1b2", direct_identity["sourceTag"])
            for name in TEMPLATES:
                with open(os.path.join(repository, "docs", "work-item-templates", name), "rb") as source:
                    expected = source.read()
                with open(os.path.join(repository, "build", "lib", "agent_workboard", "resources",
                                       "work_item_templates", name), "rb") as packaged:
                    self.assertEqual(expected, packaged.read())
            with open(os.path.join(repository, RUNBOOK), "rb") as handle:
                root_runbook = handle.read()
            with open(os.path.join(repository, PACKAGE_RUNBOOK), "rb") as handle:
                self.assertEqual(root_runbook, handle.read())
            packaged_runbook = os.path.join(repository, "build", "lib", "agent_workboard", "resources",
                                            "codex", "skills", "awb-orchestrator", "references",
                                            "upgrade-and-rollback.md")
            with open(packaged_runbook, "rb") as handle:
                self.assertEqual(root_runbook, handle.read())
            self._assert_runbook_semantics(root_runbook)
            root_skill = os.path.join(repository, ".codex", "skills", "awb-orchestrator", "SKILL.md")
            package_skill = os.path.join(repository, "src", "agent_workboard", "resources", "codex",
                                         "skills", "awb-orchestrator", "SKILL.md")
            with open(root_skill, "rb") as first, open(package_skill, "rb") as second:
                self.assertEqual(first.read(), second.read())
            with open(root_skill, "r", encoding="utf-8") as handle:
                route = handle.read()
            for token in ("references/upgrade-and-rollback.md", "PREFLIGHT_FIRST", "REFUSED",
                          "BLOCKED", "nextStep", "--orchestrator-generation",
                          "does not\nspawn", "AWB-CREATION-RISK-v1", "AUTO_ON_PASS",
                          "AUTO_GATE_APPROVED", "300-second", "caffeinate -di",
                          "last active WorkItem", "POWER_INHIBIT_UNAVAILABLE",
                          "POWER_INHIBIT_CLEANUP_FAILED", "`pkill`", "`pmset`",
                          "not a product\nrunner"):
                self.assertIn(token, route)

            wheel = os.path.join(repository, "dist", "agent_workboard-0.3.1b2-py3-none-any.whl")
            os.makedirs(os.path.dirname(wheel), exist_ok=True)
            self._wheel_from_build(repository, wheel)
            with zipfile.ZipFile(wheel) as archive:
                wheel_runbook = archive.read(
                    "agent_workboard/resources/codex/skills/awb-orchestrator/references/upgrade-and-rollback.md")
            self.assertEqual(root_runbook, wheel_runbook)

            hashes = []
            archives = []
            for sequence in ("one", "two"):
                checkout = os.path.join(temporary, sequence)
                subprocess.check_call(["git", "clone", "--quiet", repository, checkout])
                subprocess.check_call(["git", "checkout", "--quiet", "v0.3.1b2"], cwd=checkout)
                subprocess.check_call([sys.executable, "setup.py", "sdist"], cwd=checkout, env=environment,
                                      stdout=subprocess.DEVNULL)
                archive = os.path.join(checkout, "dist", "agent-workboard-0.3.1b2.tar.gz")
                with open(archive, "rb") as handle:
                    hashes.append(hashlib.sha256(handle.read()).hexdigest())
                archives.append(archive)
            self.assertEqual(hashes[0], hashes[1])

            extracted = os.path.join(temporary, "extracted")
            with tarfile.open(archives[0], "r:gz") as archive:
                archive.extractall(extracted)
            source_tree = os.path.join(extracted, "agent-workboard-0.3.1b2")
            self.assertFalse(os.path.exists(os.path.join(source_tree, ".git")))
            with open(os.path.join(source_tree, ".awb-release-identity.json"), "r", encoding="utf-8") as handle:
                envelope = json.load(handle)
            self.assertEqual(direct_identity, envelope)
            with open(os.path.join(source_tree, RUNBOOK), "rb") as handle:
                self.assertEqual(root_runbook, handle.read())
            with open(os.path.join(source_tree, PACKAGE_RUNBOOK), "rb") as handle:
                self.assertEqual(root_runbook, handle.read())
            subprocess.check_call([sys.executable, "setup.py", "build_py"], cwd=source_tree, env=environment,
                                  stdout=subprocess.DEVNULL)
            self.assertEqual(direct_identity, self._identity(
                os.path.join(source_tree, "build", "lib", "agent_workboard", "_build.py")))
            with open(os.path.join(source_tree, "build", "lib", "agent_workboard", "resources", "codex",
                                   "skills", "awb-orchestrator", "references",
                                   "upgrade-and-rollback.md"), "rb") as handle:
                self.assertEqual(root_runbook, handle.read())
            no_git_wheel = os.path.join(source_tree, "dist", "agent_workboard-0.3.1b2-py3-none-any.whl")
            self._wheel_from_build(source_tree, no_git_wheel)
            with zipfile.ZipFile(no_git_wheel) as archive:
                self.assertEqual(root_runbook, archive.read(
                    "agent_workboard/resources/codex/skills/awb-orchestrator/references/upgrade-and-rollback.md"))

            envelope["sourceTag"] = "v9.9.9"
            with open(os.path.join(source_tree, ".awb-release-identity.json"), "w", encoding="utf-8") as handle:
                json.dump(envelope, handle)
            self.assertNotEqual(0, subprocess.call([sys.executable, "setup.py", "build_py"], cwd=source_tree,
                                                   env=environment, stdout=subprocess.DEVNULL,
                                                   stderr=subprocess.DEVNULL))


if __name__ == "__main__":
    unittest.main()
