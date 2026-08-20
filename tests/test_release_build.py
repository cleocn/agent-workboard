import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


TEMPLATES = (
    "README.md", "awb-template.md", "fe-template.md",
    "remediation-plan-template.md", "test-issue-template.md",
    "test-issue-trigger-rules.md", "wa-template.md",
)


class ReleaseBuildTest(unittest.TestCase):
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
        subprocess.check_call(["git", "tag", "v0.2.0"], cwd=repository)
        return repository

    def _identity(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return ast.literal_eval(handle.read().split("=", 1)[1].strip())

    def test_tagged_build_and_no_git_sdist_preserve_identity_and_templates(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._release_repository(temporary)
            environment = dict(os.environ)
            environment["SOURCE_DATE_EPOCH"] = "946684800"
            subprocess.check_call([sys.executable, "setup.py", "build_py"], cwd=repository, env=environment,
                                  stdout=subprocess.DEVNULL)
            direct_identity = self._identity(os.path.join(repository, "build", "lib", "agent_workboard", "_build.py"))
            self.assertEqual("0.2.0", direct_identity["packageVersion"])
            self.assertEqual("v0.2.0", direct_identity["sourceTag"])
            for name in TEMPLATES:
                with open(os.path.join(repository, "docs", "work-item-templates", name), "rb") as source:
                    expected = source.read()
                with open(os.path.join(repository, "build", "lib", "agent_workboard", "resources",
                                       "work_item_templates", name), "rb") as packaged:
                    self.assertEqual(expected, packaged.read())

            hashes = []
            archives = []
            for sequence in ("one", "two"):
                checkout = os.path.join(temporary, sequence)
                subprocess.check_call(["git", "clone", "--quiet", repository, checkout])
                subprocess.check_call(["git", "checkout", "--quiet", "v0.2.0"], cwd=checkout)
                subprocess.check_call([sys.executable, "setup.py", "sdist"], cwd=checkout, env=environment,
                                      stdout=subprocess.DEVNULL)
                archive = os.path.join(checkout, "dist", "agent-workboard-0.2.0.tar.gz")
                with open(archive, "rb") as handle:
                    hashes.append(hashlib.sha256(handle.read()).hexdigest())
                archives.append(archive)
            self.assertEqual(hashes[0], hashes[1])

            extracted = os.path.join(temporary, "extracted")
            with tarfile.open(archives[0], "r:gz") as archive:
                archive.extractall(extracted)
            source_tree = os.path.join(extracted, "agent-workboard-0.2.0")
            self.assertFalse(os.path.exists(os.path.join(source_tree, ".git")))
            with open(os.path.join(source_tree, ".awb-release-identity.json"), "r", encoding="utf-8") as handle:
                envelope = json.load(handle)
            self.assertEqual(direct_identity, envelope)
            subprocess.check_call([sys.executable, "setup.py", "build_py"], cwd=source_tree, env=environment,
                                  stdout=subprocess.DEVNULL)
            self.assertEqual(direct_identity, self._identity(
                os.path.join(source_tree, "build", "lib", "agent_workboard", "_build.py")))

            envelope["sourceTag"] = "v9.9.9"
            with open(os.path.join(source_tree, ".awb-release-identity.json"), "w", encoding="utf-8") as handle:
                json.dump(envelope, handle)
            self.assertNotEqual(0, subprocess.call([sys.executable, "setup.py", "build_py"], cwd=source_tree,
                                                   env=environment, stdout=subprocess.DEVNULL,
                                                   stderr=subprocess.DEVNULL))


if __name__ == "__main__":
    unittest.main()
