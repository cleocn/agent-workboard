import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_workboard.lite import LiteError, create_work_item, initialize_database
from agent_workboard.cli import _project_database, main as cli_main
from agent_workboard.project import (bootstrap, codex_check, codex_install, doctor,
                                     init_project, transfer_export, transfer_import)


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
        create_work_item(source, "AWB-002", "AWB", "held item")
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

    def test_real_pip_wheel_init_and_doctor_work_from_an_unrelated_directory(self):
        repository = os.path.dirname(os.path.dirname(__file__))
        wheel = os.path.join(repository, "dist", "agent_workboard-0.1.0-py3-none-any.whl")
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
            artifact = os.path.join(project, ".awb", "artifacts", "agent_workboard-0.1.0-py3-none-any.whl")
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
