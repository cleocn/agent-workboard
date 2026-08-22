import os
import subprocess
import sys
import tempfile
import unittest


class ReleaseGateTest(unittest.TestCase):
    def _git(self, repository, *args):
        return subprocess.check_output(["git"] + list(args), cwd=repository).decode("ascii").strip()

    def test_release_gate_requires_allowlisted_direct_public_successor(self):
        with tempfile.TemporaryDirectory() as repository:
            subprocess.check_call(["git", "init"], cwd=repository, stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "config", "user.name", "test"], cwd=repository)
            subprocess.check_call(["git", "config", "user.email", "test@example.invalid"], cwd=repository)
            with open(os.path.join(repository, "release.txt"), "w", encoding="utf-8") as handle:
                handle.write("0.1.0\n")
            subprocess.check_call(["git", "add", "--", "release.txt"], cwd=repository)
            subprocess.check_call(["git", "commit", "-m", "v0.1.0"], cwd=repository, stdout=subprocess.DEVNULL)
            v01 = self._git(repository, "rev-parse", "HEAD")
            subprocess.check_call(["git", "tag", "-a", "v0.1.0", "-m", "v0.1.0"], cwd=repository)
            with open(os.path.join(repository, "stable.txt"), "w", encoding="utf-8") as handle:
                handle.write("stable self-hosting\n")
            subprocess.check_call(["git", "add", "--", "stable.txt"], cwd=repository)
            subprocess.check_call(["git", "commit", "-m", "v0.2.0"], cwd=repository,
                                  stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "tag", "-a", "v0.2.0", "-m", "v0.2.0"], cwd=repository)
            v02 = self._git(repository, "rev-parse", "HEAD")
            with open(os.path.join(repository, "release.txt"), "w", encoding="utf-8") as handle:
                handle.write("0.2.1\n")
            subprocess.check_call(["git", "add", "--", "release.txt"], cwd=repository)
            subprocess.check_call(["git", "commit", "-m", "v0.2.1"], cwd=repository,
                                  stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "tag", "-a", "v0.2.1", "-m", "v0.2.1"], cwd=repository)
            v021 = self._git(repository, "rev-parse", "HEAD")
            subprocess.check_call(["git", "switch", "-c", "release/awb-011-v0.3.0b1"],
                                  cwd=repository, stdout=subprocess.DEVNULL)
            with open(os.path.join(repository, "release.txt"), "w", encoding="utf-8") as handle:
                handle.write("0.3.0b1\n")
            subprocess.check_call(["git", "add", "--", "release.txt"], cwd=repository)
            subprocess.check_call(["git", "commit", "-m", "v0.3.0b1"], cwd=repository,
                                  stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "tag", "-a", "v0.3.0b1", "-m", "v0.3.0b1"],
                                  cwd=repository)
            v030b1 = self._git(repository, "rev-parse", "HEAD")
            subprocess.check_call(["git", "switch", "-c", "release/awb-015-v0.3.1b1"],
                                  cwd=repository, stdout=subprocess.DEVNULL)
            with open(os.path.join(repository, "release.txt"), "w", encoding="utf-8") as handle:
                handle.write("0.3.1b1\n")
            subprocess.check_call(["git", "add", "--", "release.txt"], cwd=repository)
            subprocess.check_call(["git", "commit", "-m", "v0.3.1b1"], cwd=repository,
                                  stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "tag", "-a", "v0.3.1b1", "-m", "v0.3.1b1"],
                                  cwd=repository)
            base = self._git(repository, "rev-parse", "HEAD")
            subprocess.check_call(["git", "switch", "-c", "release/awb-018-v0.3.1b2"],
                                  cwd=repository, stdout=subprocess.DEVNULL)
            with open(os.path.join(repository, "release.txt"), "w", encoding="utf-8") as handle:
                handle.write("0.3.1b2\n")
            subprocess.check_call(["git", "add", "--", "release.txt"], cwd=repository)
            subprocess.check_call(["git", "commit", "-m", "v0.3.1b2"], cwd=repository,
                                  stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "tag", "-a", "v0.3.1b2", "-m", "v0.3.1b2"],
                                  cwd=repository)
            script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "release_gate.py")
            subprocess.check_call([sys.executable, script, "check-successor", "--repository", repository,
                                   "--base", base, "--v0.1-commit", v01, "--v0.2-commit", v02,
                                   "--v0.2.1-commit", v021, "--v0.3.0b1-commit", v030b1,
                                   "--v0.3.1b1-commit", base,
                                   "--path", "release.txt"],
                                  stdout=subprocess.DEVNULL)
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.check_call([sys.executable, script, "check-successor", "--repository", repository,
                                       "--base", base, "--v0.1-commit", v01, "--v0.2-commit", v02,
                                       "--v0.2.1-commit", v021, "--v0.3.0b1-commit", v030b1,
                                       "--v0.3.1b1-commit", base,
                                       "--path", "stable.txt"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            subprocess.check_call(["git", "tag", "-d", "v0.3.1b2"], cwd=repository,
                                  stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "tag", "v0.3.1b2"], cwd=repository)
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.check_call([sys.executable, script, "check-successor", "--repository", repository,
                                       "--base", base, "--v0.1-commit", v01, "--v0.2-commit", v02,
                                       "--v0.2.1-commit", v021, "--v0.3.0b1-commit", v030b1,
                                       "--v0.3.1b1-commit", base,
                                       "--path", "release.txt"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
