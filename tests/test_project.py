import json
import hashlib
import io
import os
import pkgutil
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_workboard.lite import (LiteError, acquire_claim, acquire_repository_lock,
                                  create_work_item, initialize_database)
from agent_workboard.cli import _project_database, main as cli_main
from agent_workboard.project import (bootstrap, codex_check, codex_install, doctor,
                                     init_project, transfer_export, transfer_import,
                                     upgrade_project)
import agent_workboard.project as project_module


class ProjectLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.temporary.name, "project")
        os.mkdir(self.root)
        self.wheel = os.path.join(self.temporary.name, "candidate.whl")
        with open(self.wheel, "wb") as handle:
            handle.write(b"test wheel binding")
        self.environment = mock.patch.dict(os.environ, {"AWB_WHEEL": self.wheel})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def bind_requirements(self):
        with open(self.wheel, "rb") as wheel:
            digest = hashlib.sha256(wheel.read()).hexdigest()
        with open(os.path.join(self.root, ".awb", "requirements-awb.txt"), "w", encoding="utf-8") as handle:
            handle.write("--require-hashes\nfile://" + self.wheel + "#egg=agent-workboard --hash=sha256:" +
                         digest + "\n")

    def target_wheel(self):
        identity = {"packageVersion": "0.2.1", "sourceCommit": "3" * 40,
                    "sourceTree": "4" * 40, "sourceTag": "v0.2.1"}
        path = os.path.join(self.temporary.name, "agent_workboard-0.2.1-py3-none-any.whl")
        self.fake_wheel(path, identity, include_codex=True)
        return path, identity

    def tree_snapshot(self, root=None):
        root = root or self.root
        snapshot = {}
        for base, directories, files in os.walk(root, followlinks=False):
            directories.sort()
            files.sort()
            for name in directories:
                path = os.path.join(base, name)
                relative = os.path.relpath(path, root).replace(os.sep, "/") + "/"
                snapshot[relative] = "SYMLINK:" + os.readlink(path) if os.path.islink(path) else "DIR"
            for name in files:
                path = os.path.join(base, name)
                relative = os.path.relpath(path, root).replace(os.sep, "/")
                if os.path.islink(path):
                    snapshot[relative] = "SYMLINK:" + os.readlink(path)
                else:
                    snapshot[relative] = hashlib.sha256(self.read_bytes(path)).hexdigest()
        return snapshot

    def read_bytes(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def assert_one_next_step(self, result):
        self.assertEqual("AWB-UPGRADE-v1", result["protocolVersion"])
        self.assertEqual({"action", "arguments"}, set(result["nextStep"]))
        self.assertTrue(result["nextStep"]["action"])
        self.assertIsInstance(result["nextStep"]["arguments"], dict)

    def fake_wheel(self, path, identity, include_codex=False, old_codex=None):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("agent_workboard/_build.py", "BUILD_IDENTITY = " + repr(identity) + "\n")
            if include_codex:
                base_resources = (
                    "codex/skills/awb-orchestrator/SKILL.md", "codex/agents/planner.toml",
                    "codex/agents/implementer.toml", "codex/agents/reviewer.toml",
                    "codex/agents/fast-worker.toml",
                )
                if old_codex == "0.1.0":
                    extra_resources = ()
                elif old_codex == "0.2.0":
                    extra_resources = ("codex/agents/convergence-reviewer.toml",)
                else:
                    extra_resources = (
                    "codex/skills/awb-orchestrator/references/upgrade-and-rollback.md",
                    "codex/agents/convergence-reviewer.toml",
                    )
                for resource in base_resources + extra_resources:
                    raw = pkgutil.get_data("agent_workboard", "resources/" + resource)
                    if old_codex:
                        raw = b"# deterministic old package resource\n" + raw
                    archive.writestr("agent_workboard/resources/" + resource,
                                     raw)

    def prepare_old_project(self, with_codex=False, source_identity=None):
        init_project(self.root, with_codex=with_codex)
        old_identity = dict(source_identity or project_module.RELEASE_0_1_IDENTITY)
        source_version = old_identity["packageVersion"]
        old_wheel = os.path.join(
            self.temporary.name,
            "agent_workboard-{0}-py3-none-any.whl".format(source_version),
        )
        self.fake_wheel(old_wheel, old_identity, include_codex=with_codex,
                        old_codex=source_version)
        if with_codex:
            for target, resource in project_module._codex_targets(self.root).items():
                raw = project_module._wheel_resource_optional(old_wheel, resource)
                if raw is None:
                    if os.path.isfile(target):
                        os.unlink(target)
                else:
                    with open(target, "wb") as handle:
                        handle.write(raw)
        ignore = os.path.join(self.root, ".awb", ".gitignore")
        with open(ignore, "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line != "backups/"]
        with open(ignore, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        config_path = os.path.join(self.root, ".awb", "config.json")
        with open(config_path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
        config.update({"requiredPackageVersion": old_identity["packageVersion"],
                       "requiredSourceCommit": old_identity["sourceCommit"],
                       "requiredSourceTree": old_identity["sourceTree"],
                       "requiredSourceTag": old_identity["sourceTag"]})
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, sort_keys=True, indent=2)
            handle.write("\n")
        with open(old_wheel, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        with open(os.path.join(self.root, ".awb", "requirements-awb.txt"), "w", encoding="utf-8") as handle:
            handle.write("--require-hashes\nfile://" + old_wheel +
                         "#egg=agent-workboard --hash=sha256:" + digest + "\n")
        return old_wheel

    def management(self, work_item_id):
        return {
            "contractVersion": "AWB-WORKITEM-MGMT-v1",
            "templateContractVersion": "AWB-MANAGEMENT-v1",
            "scope": ["transfer test"], "outOfScope": ["remote writes"],
            "authorization": {"allowed": ["local test"], "forbidden": ["remote writes"]},
            "safetyConstraints": ["preserve evidence"],
            "tasks": [
                {"taskId": work_item_id + "-T01", "seq": 1, "title": "规划与诊断",
                 "ownerRole": "PLANNER", "required": True, "acceptance": ["plan"],
                 "closureEvidenceRequired": ["plan evidence"]},
                {"taskId": work_item_id + "-T02", "seq": 2, "title": "实施与本地测试",
                 "ownerRole": "IMPLEMENTER", "required": True, "acceptance": ["implementation"],
                 "closureEvidenceRequired": ["test evidence"]},
                {"taskId": work_item_id + "-T03", "seq": 3, "title": "批量最终复审",
                 "ownerRole": "REVIEWER", "required": True, "acceptance": ["review"],
                 "closureEvidenceRequired": ["review evidence"]},
            ],
            "acceptance": [{"id": "AC-001", "criterion": "transfer succeeds"}],
            "closure": [{"id": "CL-001", "criterion": "evidence retained"}],
        }

    def test_development_init_writes_bound_requirements_and_doctor(self):
        result = init_project(self.root, development=True)
        self.assertEqual("development", result["runtimeMode"])
        self.assertEqual("ok", doctor(self.root)["status"])
        with open(self.wheel, "ab") as handle:
            handle.write(b"tamper")
        with self.assertRaises(LiteError):
            doctor(self.root)
        with self.assertRaises(LiteError):
            init_project(self.root, development=True)

    def test_bootstrap_requires_committed_contract_and_is_noop_for_valid_db(self):
        init_project(self.root, development=True)
        self.bind_requirements()
        database = os.path.join(self.root, ".awb", "dev", "workboard.db")
        os.unlink(database)
        self.assertEqual("ok", bootstrap(self.root)["status"])
        self.assertEqual("no-op", bootstrap(self.root)["status"])
        with open(os.path.join(self.root, ".awb", "config.json"), "w", encoding="utf-8") as handle:
            json.dump({}, handle)
        with self.assertRaises(LiteError):
            bootstrap(self.root)

    def test_codex_templates_are_package_resources_and_no_clobber(self):
        init_project(self.root, development=True)
        self.bind_requirements()
        self.assertEqual("ok", codex_install(self.root)["status"])
        self.assertEqual("ok", codex_check(self.root)["status"])
        with self.assertRaises(LiteError):
            codex_install(self.root)

    def test_development_database_rejects_stable_hardlink_alias(self):
        init_project(self.root, development=True)
        self.bind_requirements()
        development = os.path.join(self.root, ".awb", "dev", "workboard.db")
        stable = os.path.join(self.root, ".awb", "workboard.db")
        os.link(development, stable)
        with self.assertRaises(LiteError):
            doctor(self.root)

    def test_serve_project_database_normalizes_a_symlinked_project_root(self):
        init_project(self.root, development=True)
        self.bind_requirements()
        linked = os.path.join(self.temporary.name, "linked-project")
        os.symlink(self.root, linked)
        self.assertEqual(os.path.realpath(os.path.join(self.root, ".awb", "dev", "workboard.db")),
                         _project_database(linked, development=True))

    def test_explicit_database_requires_matching_configured_project(self):
        external = os.path.join(self.temporary.name, "external", ".awb", "workboard.db")
        initialize_database(external)
        missing = os.path.join(self.temporary.name, "missing-project")
        self.assertEqual(2, cli_main(["lite", "--project", missing, "--database", external, "list"]))
        self.assertEqual(2, cli_main(["lite", "--project", missing, "--database", external,
                                      "--development", "list"]))
        init_project(self.root, development=True)
        configured = os.path.join(self.root, ".awb", "dev", "workboard.db")
        self.assertEqual(0, cli_main(["lite", "--project", self.root, "--database", configured,
                                      "--development", "list"]))

    def test_transfer_is_idempotent_and_conflict_is_zero_write(self):
        source = os.path.join(self.temporary.name, "source.db")
        target = os.path.join(self.temporary.name, "target.db")
        bundle = os.path.join(self.temporary.name, "bundle.json")
        initialize_database(source)
        initialize_database(target)
        create_work_item(
            source, "AWB-002", "AWB", "held item", management=self.management("AWB-002")
        )
        exported = transfer_export(source, ["AWB-002"], bundle)
        self.assertEqual("ok", exported["status"])
        self.assertEqual("ok", transfer_import(target, bundle)["status"])
        self.assertEqual("no-op", transfer_import(target, bundle)["status"])
        with open(bundle, "r", encoding="utf-8") as handle:
            altered = json.load(handle)
        altered["tables"]["work_items"][0]["title"] = "conflict"
        body = dict(altered)
        body.pop("sha256")
        altered["sha256"] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        altered_path = os.path.join(self.temporary.name, "conflict.json")
        with open(altered_path, "w", encoding="utf-8") as handle:
            json.dump(altered, handle, ensure_ascii=False)
        with self.assertRaises(LiteError):
            transfer_import(target, altered_path)

    def test_upgrade_backs_up_and_rebinds_a_stable_0_1_project(self):
        old_wheel = self.prepare_old_project()
        with open(old_wheel, "rb") as handle:
            old_digest = hashlib.sha256(handle.read()).hexdigest()
        with open(os.path.join(self.root, ".awb", "requirements-awb.txt"), "w", encoding="utf-8") as handle:
            handle.write("--require-hashes\nhttps://github.com/cleocn/agent-workboard/releases/download/v0.1.0/" +
                         os.path.basename(old_wheel) + "#egg=agent-workboard --hash=sha256:" + old_digest + "\n")
        target_identity = {"packageVersion": "0.2.1", "sourceCommit": "3" * 40,
                           "sourceTree": "4" * 40, "sourceTag": "v0.2.1"}
        target_wheel = os.path.join(self.temporary.name, "agent_workboard-0.2.1-py3-none-any.whl")
        self.fake_wheel(target_wheel, target_identity)
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity), \
                mock.patch.object(project_module, "_is_editable", return_value=False):
            result = upgrade_project(self.root, target_wheel)
            self.assertEqual("OK", result["status"])
            self.assert_one_next_step(result)
            self.assertTrue(os.path.isfile(result["rollback"]["databaseBackup"]))
            self.assertEqual("ok", doctor(self.root)["status"])
        with open(os.path.join(self.root, ".awb", "config.json"), "r", encoding="utf-8") as handle:
            self.assertEqual("0.2.1", json.load(handle)["requiredPackageVersion"])

    def test_exact_0_2_source_upgrades_and_rolls_back_package_owned_bytes(self):
        old_wheel = self.prepare_old_project(
            with_codex=True, source_identity=project_module.RELEASE_0_2_IDENTITY
        )
        managed = [os.path.join(self.root, ".awb", name) for name in project_module.MANAGED]
        codex = list(project_module._codex_targets(self.root))
        before = {path: (self.read_bytes(path) if os.path.isfile(path) else None)
                  for path in managed + codex}
        target_wheel, target_identity = self.target_wheel()

        before_check = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            checked = upgrade_project(self.root, target_wheel, with_codex=True, check=True)
            self.assertEqual("READY", checked["status"])
            self.assertEqual(project_module.RELEASE_0_2_IDENTITY, checked["from"])
            self.assertEqual(before_check, self.tree_snapshot())
            upgraded = upgrade_project(self.root, target_wheel, with_codex=True)
            self.assertEqual("OK", upgraded["status"])
            rollback_before = self.tree_snapshot()
            rollback_check = upgrade_project(
                self.root, rollback_manifest=upgraded["rollback"]["manifest"], check=True
            )
            self.assertEqual("READY", rollback_check["status"])
            self.assertEqual(rollback_before, self.tree_snapshot())
            rolled_back = upgrade_project(
                self.root, rollback_manifest=upgraded["rollback"]["manifest"]
            )
        self.assertEqual("OK", rolled_back["status"])
        self.assertEqual(old_wheel, rolled_back["nextStep"]["arguments"]["wheel"])
        for path, expected in before.items():
            if expected is None:
                self.assertFalse(os.path.exists(path), path)
            else:
                self.assertEqual(expected, self.read_bytes(path), path)

    def test_upgrade_refuses_customized_codex_without_changing_contract(self):
        self.prepare_old_project(with_codex=True)
        target_identity = {"packageVersion": "0.2.1", "sourceCommit": "3" * 40,
                           "sourceTree": "4" * 40, "sourceTag": "v0.2.1"}
        target_wheel = os.path.join(self.temporary.name, "agent_workboard-0.2.1-py3-none-any.whl")
        self.fake_wheel(target_wheel, target_identity)
        config_path = os.path.join(self.root, ".awb", "config.json")
        with open(config_path, "rb") as handle:
            before = handle.read()
        with open(os.path.join(self.root, ".codex", "agents", "planner.toml"), "ab") as handle:
            handle.write(b"\n# customized\n")
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            result = upgrade_project(self.root, target_wheel, with_codex=True)
            self.assertEqual("REFUSED", result["status"])
        with open(config_path, "rb") as handle:
            self.assertEqual(before, handle.read())

    def test_upgrade_check_is_structured_ready_and_byte_for_byte_zero_write(self):
        self.prepare_old_project(with_codex=True)
        target_wheel, target_identity = self.target_wheel()
        before = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            result = upgrade_project(self.root, target_wheel, with_codex=True, check=True)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, cli_main(["upgrade", "--check", "--project", self.root,
                                             "--wheel", target_wheel, "--with-codex"]))
        self.assertEqual(before, self.tree_snapshot())
        self.assertEqual("CHECK", result["operation"])
        self.assertEqual("READY", result["status"])
        self.assertEqual("SUPPORTED", result["applicability"]["status"])
        self.assertTrue(result["paths"]["replaced"])
        created = [entry["path"] for entry in result["paths"]["created"]]
        self.assertIn(".codex/agents/convergence-reviewer.toml", created)
        self.assertIn(".codex/skills/awb-orchestrator/references/upgrade-and-rollback.md", created)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".awb", "backups")))
        self.assert_one_next_step(result)

    def test_upgrade_check_noop_and_unsupported_are_zero_write(self):
        self.prepare_old_project()
        target_wheel, target_identity = self.target_wheel()
        config_path = os.path.join(self.root, ".awb", "config.json")
        with open(config_path, "r", encoding="utf-8") as handle:
            unsupported = json.load(handle)
        unsupported.update({"requiredPackageVersion": "0.1.1", "requiredSourceTag": "v0.1.1"})
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(unsupported, handle, sort_keys=True, indent=2)
            handle.write("\n")
        before = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            refused = upgrade_project(self.root, target_wheel, check=True)
        self.assertEqual("REFUSED", refused["status"])
        self.assertEqual(before, self.tree_snapshot())
        self.assert_one_next_step(refused)
        self.assertEqual("STOP_UNSUPPORTED_UPGRADE_PAIR", refused["nextStep"]["action"])
        self.assertEqual({"project": os.path.realpath(self.root), "wheel": os.path.realpath(target_wheel),
                          "withCodex": False},
                         refused["nextStep"]["arguments"])

        unsupported.update({"requiredPackageVersion": "0.2.1",
                            "requiredSourceCommit": target_identity["sourceCommit"],
                            "requiredSourceTree": target_identity["sourceTree"],
                            "requiredSourceTag": target_identity["sourceTag"]})
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(unsupported, handle, sort_keys=True, indent=2)
            handle.write("\n")
        with open(target_wheel, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        with open(os.path.join(self.root, ".awb", "requirements-awb.txt"), "w", encoding="utf-8") as handle:
            handle.write("--require-hashes\nfile://" + target_wheel +
                         "#egg=agent-workboard --hash=sha256:" + digest + "\n")
        before = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            no_op = upgrade_project(self.root, target_wheel, check=True)
        self.assertEqual("NO_OP", no_op["status"])
        self.assertEqual(before, self.tree_snapshot())
        self.assert_one_next_step(no_op)
        self.assertEqual({"action": "NONE", "arguments": {}}, no_op["nextStep"])

    def test_upgrade_refuses_symbolic_backup_root_before_any_internal_or_external_write(self):
        self.prepare_old_project()
        target_wheel, target_identity = self.target_wheel()
        external = os.path.join(self.temporary.name, "external-backups")
        os.mkdir(external)
        backups = os.path.join(self.root, ".awb", "backups")
        os.symlink(external, backups)
        project_before = self.tree_snapshot()
        external_before = self.tree_snapshot(external)
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            checked = upgrade_project(self.root, target_wheel, check=True)
            executed = upgrade_project(self.root, target_wheel)
        for result in (checked, executed):
            self.assertEqual("REFUSED", result["status"])
            self.assertIn("backup root", result["reason"])
            self.assertEqual("RESTORE_SAFE_BACKUP_ROOT_AND_RECHECK",
                             result["nextStep"]["action"])
            self.assertEqual({"project": os.path.realpath(self.root),
                              "wheel": os.path.realpath(target_wheel),
                              "withCodex": False}, result["nextStep"]["arguments"])
        self.assertEqual(project_before, self.tree_snapshot())
        self.assertEqual(external_before, self.tree_snapshot(external))

    def test_exact_rollback_restores_bytes_removes_only_created_and_refuses_replay(self):
        old_wheel = self.prepare_old_project(with_codex=True)
        target_wheel, target_identity = self.target_wheel()
        managed = [os.path.join(self.root, ".awb", name) for name in project_module.MANAGED]
        codex = list(project_module._codex_targets(self.root))
        before = {path: (self.read_bytes(path) if os.path.isfile(path) else None)
                  for path in managed + codex}
        database = os.path.join(self.root, ".awb", "workboard.db")
        database_before = self.read_bytes(database)
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity), \
                mock.patch.object(project_module, "_is_editable", return_value=False):
            upgraded = upgrade_project(self.root, target_wheel, with_codex=True)
            self.assertEqual("ok", codex_check(self.root)["status"])
            manifest = upgraded["rollback"]["manifest"]
            check_before = self.tree_snapshot()
            checked = upgrade_project(self.root, rollback_manifest=manifest, check=True)
            self.assertEqual("READY", checked["status"])
            self.assertEqual({"action": "EXECUTE_ROLLBACK",
                              "arguments": {"project": os.path.realpath(self.root),
                                            "rollbackManifest": manifest}},
                             checked["nextStep"])
            self.assertEqual(check_before, self.tree_snapshot())
            rolled_back = upgrade_project(self.root, rollback_manifest=manifest)
        self.assertEqual("OK", rolled_back["status"])
        self.assertEqual("CONSUMED", rolled_back["rollback"]["state"])
        self.assert_one_next_step(rolled_back)
        self.assertEqual({"project": os.path.realpath(self.root), "wheel": old_wheel,
                          "withCodex": True,
                          "rollbackManifest": manifest}, rolled_back["nextStep"]["arguments"])
        for path, expected in before.items():
            if expected is None:
                self.assertFalse(os.path.lexists(path), path)
            else:
                self.assertEqual(expected, self.read_bytes(path), path)
        self.assertEqual(database_before, self.read_bytes(database))
        replay_before = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            replay = upgrade_project(self.root, rollback_manifest=manifest, check=True)
        self.assertEqual("REFUSED", replay["status"])
        self.assertIn("consumed", replay["reason"])
        self.assertEqual(replay_before, self.tree_snapshot())

    def test_rollback_refuses_wrong_project_traversal_duplicate_symlink_and_drift(self):
        self.prepare_old_project(with_codex=True)
        target_wheel, target_identity = self.target_wheel()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            upgraded = upgrade_project(self.root, target_wheel, with_codex=True)
        manifest_path = upgraded["rollback"]["manifest"]
        original = self.read_bytes(manifest_path)

        other = os.path.join(self.temporary.name, "other")
        os.mkdir(other)
        with mock.patch.dict(os.environ, {"AWB_WHEEL": self.wheel}):
            init_project(other, development=True)
        copied_root = os.path.join(other, ".awb", "backups", "upgrade-copy")
        os.makedirs(copied_root)
        copied_manifest = os.path.join(copied_root, "rollback.json")
        shutil.copyfile(manifest_path, copied_manifest)
        other_before = self.tree_snapshot(other)
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            wrong = upgrade_project(other, rollback_manifest=copied_manifest, check=True)
        self.assertEqual("REFUSED", wrong["status"])
        self.assertEqual(other_before, self.tree_snapshot(other))

        manifest = json.loads(original.decode("utf-8"))
        manifest["actions"][0]["path"] = "../escape"
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        before_check = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            traversal = upgrade_project(self.root, rollback_manifest=manifest_path, check=True)
        self.assertEqual("REFUSED", traversal["status"])
        self.assertEqual(before_check, self.tree_snapshot())

        with open(manifest_path, "wb") as handle:
            handle.write(original)
        manifest = json.loads(original.decode("utf-8"))
        duplicate = json.loads(original.decode("utf-8"))
        duplicate["actions"].append(dict(duplicate["actions"][0]))
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(duplicate, handle)
        before_check = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            duplicate_result = upgrade_project(self.root, rollback_manifest=manifest_path, check=True)
        self.assertEqual("REFUSED", duplicate_result["status"])
        self.assertEqual(before_check, self.tree_snapshot())

        with open(manifest_path, "wb") as handle:
            handle.write(original)
        target = os.path.join(self.root, *manifest["actions"][1]["path"].split("/"))
        current = self.read_bytes(target)
        os.unlink(target)
        os.symlink(os.path.join(self.root, ".awb", "project.md"), target)
        before_check = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            symbolic = upgrade_project(self.root, rollback_manifest=manifest_path, check=True)
        self.assertEqual("REFUSED", symbolic["status"])
        self.assertEqual(before_check, self.tree_snapshot())
        os.unlink(target)
        with open(target, "wb") as handle:
            handle.write(current)

        drift_target = os.path.join(self.root, *manifest["actions"][0]["path"].split("/"))
        with open(drift_target, "ab") as handle:
            handle.write(b"user drift")
        before_check = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            drift = upgrade_project(self.root, rollback_manifest=manifest_path, check=True)
        self.assertEqual("REFUSED", drift["status"])
        self.assertEqual(before_check, self.tree_snapshot())

    def test_rollback_refuses_symbolic_backup_root_without_internal_or_external_write(self):
        self.prepare_old_project(with_codex=True)
        target_wheel, target_identity = self.target_wheel()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            upgraded = upgrade_project(self.root, target_wheel, with_codex=True)
        manifest = upgraded["rollback"]["manifest"]
        backups = os.path.join(self.root, ".awb", "backups")
        external = os.path.join(self.temporary.name, "relocated-backups")
        os.rename(backups, external)
        os.symlink(external, backups)
        project_before = self.tree_snapshot()
        external_before = self.tree_snapshot(external)
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            checked = upgrade_project(self.root, rollback_manifest=manifest, check=True)
            executed = upgrade_project(self.root, rollback_manifest=manifest)
        for result in (checked, executed):
            self.assertEqual("REFUSED", result["status"])
            self.assertIn("backup root", result["reason"])
            self.assertEqual({"project": os.path.realpath(self.root),
                              "rollbackManifest": os.path.abspath(manifest)},
                             result["nextStep"]["arguments"])
        self.assertEqual(project_before, self.tree_snapshot())
        self.assertEqual(external_before, self.tree_snapshot(external))

    def test_rollback_rejects_manifest_actions_outside_closed_managed_universe(self):
        self.prepare_old_project(with_codex=True)
        target_wheel, target_identity = self.target_wheel()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            upgraded = upgrade_project(self.root, target_wheel, with_codex=True)
        manifest_path = upgraded["rollback"]["manifest"]
        original = json.loads(self.read_bytes(manifest_path).decode("utf-8"))
        user_path = os.path.join(self.root, "USER-DATA.txt")
        with open(user_path, "wb") as handle:
            handle.write(b"must survive forged rollback actions")
        user_bytes = self.read_bytes(user_path)
        user_sha = hashlib.sha256(user_bytes).hexdigest()
        backup_root = os.path.dirname(manifest_path)
        forged_backup = os.path.join(backup_root, "forged-user-backup")
        with open(forged_backup, "wb") as handle:
            handle.write(user_bytes)
        cases = {}
        forged = json.loads(json.dumps(original))
        forged["actions"].append({
            "action": "REMOVE_CREATED", "path": "USER-DATA.txt",
            "before": {"exists": False, "sha256": None, "backup": None},
            "after": {"exists": True, "sha256": user_sha},
        })
        cases["forged remove-created"] = forged
        forged = json.loads(json.dumps(original))
        forged["actions"].append({
            "action": "RESTORE", "path": "USER-DATA.txt",
            "before": {"exists": True, "sha256": user_sha,
                       "backup": "forged-user-backup"},
            "after": {"exists": True, "sha256": user_sha},
        })
        cases["forged restore"] = forged
        forged = json.loads(json.dumps(original))
        forged["retained"].append({"action": "RETAIN", "path": "USER-DATA.txt",
                                    "backup": "forged-user-backup"})
        cases["forged retain"] = forged
        forged = json.loads(json.dumps(original))
        forged["actions"] = forged["actions"][1:]
        cases["omitted action"] = forged
        forged = json.loads(json.dumps(original))
        forged["actions"][0]["action"] = "REMOVE_CREATED"
        cases["changed action"] = forged

        for label, forged in cases.items():
            with self.subTest(label=label):
                with open(manifest_path, "w", encoding="utf-8") as handle:
                    json.dump(forged, handle, sort_keys=True, indent=2)
                    handle.write("\n")
                project_before = self.tree_snapshot()
                with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
                    checked = upgrade_project(self.root, rollback_manifest=manifest_path, check=True)
                    executed = upgrade_project(self.root, rollback_manifest=manifest_path)
                self.assertEqual("REFUSED", checked["status"])
                self.assertEqual("REFUSED", executed["status"])
                self.assertIn("closed managed action universe", checked["reason"])
                self.assertEqual(project_before, self.tree_snapshot())
                self.assertEqual(user_bytes, self.read_bytes(user_path))
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(original, handle, sort_keys=True, indent=2)
            handle.write("\n")

    def test_rollback_rejects_identity_and_backup_tampering_zero_write(self):
        self.prepare_old_project(with_codex=True)
        target_wheel, target_identity = self.target_wheel()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            upgraded = upgrade_project(self.root, target_wheel, with_codex=True)
        manifest_path = upgraded["rollback"]["manifest"]
        original = self.read_bytes(manifest_path)
        manifest = json.loads(original.decode("utf-8"))
        manifest["to"]["sourceTree"] = "8" * 40
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        before_check = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            refused = upgrade_project(self.root, rollback_manifest=manifest_path, check=True)
        self.assertEqual("REFUSED", refused["status"])
        self.assertEqual(before_check, self.tree_snapshot())

        with open(manifest_path, "wb") as handle:
            handle.write(original)
        manifest = json.loads(original.decode("utf-8"))
        restore = next(action for action in manifest["actions"] if action["action"] == "RESTORE")
        backup = os.path.join(os.path.dirname(manifest_path), *restore["before"]["backup"].split("/"))
        with open(backup, "ab") as handle:
            handle.write(b"tamper")
        before_check = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            refused = upgrade_project(self.root, rollback_manifest=manifest_path, check=True)
        self.assertEqual("REFUSED", refused["status"])
        self.assertIn("backup hash drift", refused["reason"])
        self.assertEqual(before_check, self.tree_snapshot())

    def test_upgrade_refuses_preexisting_or_symlinked_new_codex_path(self):
        self.prepare_old_project(with_codex=True)
        target_wheel, target_identity = self.target_wheel()
        runbook = os.path.join(self.root, ".codex", "skills", "awb-orchestrator", "references",
                               "upgrade-and-rollback.md")
        with open(runbook, "wb") as handle:
            handle.write(b"user-owned")
        before = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            refused = upgrade_project(self.root, target_wheel, with_codex=True, check=True)
        self.assertEqual("REFUSED", refused["status"])
        self.assertEqual(before, self.tree_snapshot())
        os.unlink(runbook)
        os.symlink(os.path.join(self.root, ".awb", "project.md"), runbook)
        before = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            refused = upgrade_project(self.root, target_wheel, with_codex=True, check=True)
        self.assertEqual("REFUSED", refused["status"])
        self.assertEqual(before, self.tree_snapshot())

    def test_without_codex_leaves_codex_uncreated_through_upgrade_and_rollback(self):
        self.prepare_old_project(with_codex=False)
        target_wheel, target_identity = self.target_wheel()
        codex = os.path.join(self.root, ".codex")
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            checked = upgrade_project(self.root, target_wheel, check=True)
            self.assertEqual("READY", checked["status"])
            untouched_codex = [entry["path"] for entry in checked["paths"]["untouched"]
                               if entry["path"].startswith(".codex/")]
            self.assertEqual(len(project_module._codex_targets(self.root)), len(untouched_codex))
            upgraded = upgrade_project(self.root, target_wheel)
            self.assertFalse(os.path.exists(codex))
            rolled_back = upgrade_project(self.root, rollback_manifest=upgraded["rollback"]["manifest"])
        self.assertEqual("OK", rolled_back["status"])
        self.assertFalse(os.path.exists(codex))

    def test_each_rollback_mutation_fault_compensates_and_double_fault_is_recoverable(self):
        self.prepare_old_project(with_codex=True)
        target_wheel, target_identity = self.target_wheel()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            upgraded = upgrade_project(self.root, target_wheel, with_codex=True)
        manifest = upgraded["rollback"]["manifest"]
        after = {action["path"]: self.read_bytes(os.path.join(self.root, *action["path"].split("/")))
                 for action in upgraded["rollback"]["actions"]}
        original_apply = project_module._apply_rollback_action
        for failed_index in range(len(upgraded["rollback"]["actions"])):
            calls = {"index": 0}

            def fail_one(action):
                index = calls["index"]
                calls["index"] += 1
                if index == failed_index:
                    raise IOError("injected mutation failure")
                return original_apply(action)

            with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity), \
                    mock.patch.object(project_module, "_apply_rollback_action", side_effect=fail_one):
                result = upgrade_project(self.root, rollback_manifest=manifest)
            self.assertEqual("REFUSED", result["status"])
            for relative, expected in after.items():
                self.assertEqual(expected, self.read_bytes(os.path.join(self.root, *relative.split("/"))))
            with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
                self.assertEqual("READY", upgrade_project(
                    self.root, rollback_manifest=manifest, check=True)["status"])

        restore_calls = {"count": 0}

        def fail_primary(action):
            if restore_calls["count"] == 1:
                raise IOError("injected primary failure")
            restore_calls["count"] += 1
            return original_apply(action)

        original_restore = project_module._restore_post_upgrade

        def fail_compensation(action, staged):
            raise IOError("injected compensation failure")

        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity), \
                mock.patch.object(project_module, "_apply_rollback_action", side_effect=fail_primary), \
                mock.patch.object(project_module, "_restore_post_upgrade", side_effect=fail_compensation):
            blocked = upgrade_project(self.root, rollback_manifest=manifest)
        self.assertEqual("BLOCKED", blocked["status"])
        self.assertEqual("EXECUTE_RECOVERY", blocked["nextStep"]["action"])
        self.assertEqual({"project": os.path.realpath(self.root), "rollbackManifest": manifest},
                         blocked["nextStep"]["arguments"])
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity), \
                mock.patch.object(project_module, "_restore_post_upgrade", side_effect=original_restore):
            recovered = upgrade_project(self.root, rollback_manifest=manifest)
        self.assertEqual("REFUSED", recovered["status"])
        self.assertEqual("RETRY_ROLLBACK_CHECK", recovered["nextStep"]["action"])
        self.assertEqual({"project": os.path.realpath(self.root), "rollbackManifest": manifest},
                         recovered["nextStep"]["arguments"])
        for relative, expected in after.items():
            self.assertEqual(expected, self.read_bytes(os.path.join(self.root, *relative.split("/"))))
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            self.assertEqual("READY", upgrade_project(
                self.root, rollback_manifest=manifest, check=True)["status"])

    def test_upgrade_cli_refusal_is_json_and_exit_two(self):
        self.prepare_old_project()
        target_wheel, target_identity = self.target_wheel()
        config = os.path.join(self.root, ".awb", "config.json")
        with open(config, "r", encoding="utf-8") as handle:
            changed = json.load(handle)
        changed["requiredSourceTree"] = "9" * 40
        with open(config, "w", encoding="utf-8") as handle:
            json.dump(changed, handle, sort_keys=True, indent=2)
            handle.write("\n")
        output = io.StringIO()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity), redirect_stdout(output):
            code = cli_main(["upgrade", "--check", "--project", self.root, "--wheel", target_wheel])
        result = json.loads(output.getvalue())
        self.assertEqual(2, code)
        self.assertEqual("REFUSED", result["status"])
        self.assert_one_next_step(result)

    def test_upgrade_refuses_active_claim_without_changing_contract(self):
        self.prepare_old_project()
        database = os.path.join(self.root, ".awb", "workboard.db")
        create_work_item(database, "AWB-777", "AWB", "active", management=self.management("AWB-777"))
        acquire_claim(database, "AWB-777", "AWB-777-T01", "planner", "PLANNER",
                      "2099-01-01T00:00:00+00:00")
        target_identity = {"packageVersion": "0.2.1", "sourceCommit": "3" * 40,
                           "sourceTree": "4" * 40, "sourceTag": "v0.2.1"}
        target_wheel = os.path.join(self.temporary.name, "agent_workboard-0.2.1-py3-none-any.whl")
        self.fake_wheel(target_wheel, target_identity)
        config_path = os.path.join(self.root, ".awb", "config.json")
        with open(config_path, "rb") as handle:
            before = handle.read()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            result = upgrade_project(self.root, target_wheel)
            self.assertEqual("REFUSED", result["status"])
            self.assertEqual("RELEASE_ACTIVE_USE_AND_RECHECK_UPGRADE",
                             result["nextStep"]["action"])
            self.assertEqual({"project": os.path.realpath(self.root),
                              "wheel": os.path.realpath(target_wheel),
                              "withCodex": False}, result["nextStep"]["arguments"])
        with open(config_path, "rb") as handle:
            self.assertEqual(before, handle.read())

    def test_upgrade_refuses_active_writer_and_rollback_refuses_active_use(self):
        self.prepare_old_project()
        database = os.path.join(self.root, ".awb", "workboard.db")
        create_work_item(database, "AWB-778", "AWB", "active writer",
                         management=self.management("AWB-778"))
        acquire_claim(database, "AWB-778", "AWB-778-T01", "planner", "PLANNER",
                      "2099-01-01T00:00:00+00:00")
        acquire_repository_lock(database, "AWB-778", "test-repository", "planner",
                                "2099-01-01T00:00:00+00:00")
        target_wheel, target_identity = self.target_wheel()
        before = self.tree_snapshot()
        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
            refused = upgrade_project(self.root, target_wheel, check=True)
        self.assertEqual("REFUSED", refused["status"])
        self.assertEqual(before, self.tree_snapshot())

        second_root = os.path.join(self.temporary.name, "rollback-project")
        os.mkdir(second_root)
        original_root = self.root
        self.root = second_root
        try:
            self.prepare_old_project()
            with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
                upgraded = upgrade_project(self.root, target_wheel)
            rollback_database = os.path.join(self.root, ".awb", "workboard.db")
            create_work_item(rollback_database, "AWB-779", "AWB", "active rollback",
                             management=self.management("AWB-779"))
            acquire_claim(rollback_database, "AWB-779", "AWB-779-T01", "planner", "PLANNER",
                          "2099-01-01T00:00:00+00:00")
            before = self.tree_snapshot()
            with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity):
                refused = upgrade_project(self.root, rollback_manifest=upgraded["rollback"]["manifest"],
                                           check=True)
            self.assertEqual("REFUSED", refused["status"])
            self.assertIn("active", refused["reason"])
            self.assertEqual(before, self.tree_snapshot())
        finally:
            self.root = original_root

    def test_upgrade_write_fault_restores_contract_and_created_paths(self):
        self.prepare_old_project(with_codex=True)
        target_wheel, target_identity = self.target_wheel()
        before = {path: (self.read_bytes(path) if os.path.isfile(path) else None)
                  for path in ([os.path.join(self.root, ".awb", name) for name in project_module.MANAGED] +
                               list(project_module._codex_targets(self.root)))}
        original = project_module._atomic_bytes
        calls = {"count": 0}

        def fail_second(path, value):
            calls["count"] += 1
            if calls["count"] == 2:
                raise IOError("injected upgrade write failure")
            return original(path, value)

        with mock.patch.object(project_module, "BUILD_IDENTITY", target_identity), \
                mock.patch.object(project_module, "_atomic_bytes", side_effect=fail_second):
            result = upgrade_project(self.root, target_wheel, with_codex=True)
        self.assertEqual("REFUSED", result["status"])
        self.assertEqual("RETRY_UPGRADE_CHECK", result["nextStep"]["action"])
        self.assertEqual({"project": os.path.realpath(self.root),
                          "wheel": os.path.realpath(target_wheel),
                          "withCodex": True}, result["nextStep"]["arguments"])
        for path, expected in before.items():
            if expected is None:
                self.assertFalse(os.path.lexists(path), path)
            else:
                self.assertEqual(expected, self.read_bytes(path), path)

    def test_real_pip_wheel_init_and_doctor_work_from_an_unrelated_directory(self):
        repository = os.path.dirname(os.path.dirname(__file__))
        wheel = os.path.join(repository, "dist", "agent_workboard-0.2.1-py3-none-any.whl")
        self.assertTrue(os.path.isfile(wheel), "final candidate wheel must be present for this lifecycle test")
        with tempfile.TemporaryDirectory() as temporary:
            environment = dict(os.environ)
            environment.pop("AWB_WHEEL", None)
            virtualenv = os.path.join(temporary, "venv")
            project = os.path.join(temporary, "project")
            unrelated = os.path.join(temporary, "unrelated")
            os.mkdir(project)
            os.mkdir(unrelated)
            subprocess.check_call([sys.executable, "-m", "venv", virtualenv], env=environment)
            pip = os.path.join(virtualenv, "bin", "pip")
            awb = os.path.join(virtualenv, "bin", "awb")
            python = os.path.join(virtualenv, "bin", "python")
            subprocess.check_call([pip, "install", "--no-deps", wheel], cwd=unrelated, env=environment)
            direct_urls = [path for path in subprocess.check_output([python, "-c", "import agent_workboard,os; root=os.path.dirname(os.path.dirname(agent_workboard.__file__)); print('\\n'.join(os.path.join(base, 'direct_url.json') for base, dirs, names in os.walk(root) if base.lower().endswith('.dist-info') and 'agent' in os.path.basename(base).lower() and 'direct_url.json' in names))"], cwd=unrelated, env=environment).decode("utf-8").splitlines() if path]
            for direct_url in direct_urls:
                os.unlink(direct_url)
            subprocess.check_call([awb, "init", "--project", project], cwd=unrelated, env=environment)
            subprocess.check_call([awb, "doctor", "--project", project], cwd=unrelated, env=environment)
            artifact = os.path.join(project, ".awb", "artifacts", "agent_workboard-0.2.1-py3-none-any.whl")
            requirements = os.path.join(project, ".awb", "requirements-awb.txt")
            self.assertTrue(os.path.isfile(artifact))
            with open(requirements, encoding="utf-8") as handle:
                lock = handle.read()
            self.assertIn("file://" + os.path.realpath(artifact) + "#egg=agent-workboard", lock)
            second = os.path.join(temporary, "second-venv")
            subprocess.check_call([sys.executable, "-m", "venv", second], env=environment)
            second_pip = os.path.join(second, "bin", "pip")
            second_awb = os.path.join(second, "bin", "awb")
            subprocess.check_call([second_pip, "install", "--require-hashes", "-r", requirements], cwd=unrelated,
                                  env=environment)
            subprocess.check_call([second_awb, "doctor", "--project", project], cwd=unrelated, env=environment)
            with open(requirements, "rb") as handle:
                before = handle.read()
            self.assertNotEqual(0, subprocess.call([awb, "init", "--project", project], cwd=unrelated,
                                                   env=environment))
            with open(requirements, "rb") as handle:
                self.assertEqual(before, handle.read())
            with open(artifact, "ab") as handle:
                handle.write(b"tamper")
            self.assertNotEqual(0, subprocess.call([awb, "doctor", "--project", project], cwd=unrelated,
                                                   env=environment))


if __name__ == "__main__":
    unittest.main()
