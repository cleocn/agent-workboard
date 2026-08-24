import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile


SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "release_gate.py")


class ReleaseGateTest(unittest.TestCase):
    def _git(self, repository, *args):
        return subprocess.check_output(["git"] + list(args), cwd=repository).decode("ascii").strip()

    def _repository(self, repository):
        subprocess.check_call(["git", "init"], cwd=repository, stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "config", "user.name", "test"], cwd=repository)
        subprocess.check_call(["git", "config", "user.email", "test@example.invalid"], cwd=repository)
        with open(os.path.join(repository, "release.txt"), "w", encoding="utf-8") as handle:
            handle.write("base\n")
        subprocess.check_call(["git", "add", "release.txt"], cwd=repository)
        subprocess.check_call(["git", "commit", "-m", "base"], cwd=repository,
                              stdout=subprocess.DEVNULL)
        base = self._git(repository, "rev-parse", "HEAD")
        subprocess.check_call(["git", "tag", "-a", "v-old", "-m", "old"], cwd=repository)
        subprocess.check_call(["git", "switch", "-c", "release/next"], cwd=repository,
                              stdout=subprocess.DEVNULL)
        with open(os.path.join(repository, "release.txt"), "w", encoding="utf-8") as handle:
            handle.write("candidate\n")
        subprocess.check_call(["git", "add", "release.txt"], cwd=repository)
        subprocess.check_call(["git", "commit", "-m", "candidate"], cwd=repository,
                              stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "tag", "-a", "v-next", "-m", "next"], cwd=repository)
        return base

    def test_dynamic_successor_gate_keeps_exact_path_and_preserved_tag(self):
        with tempfile.TemporaryDirectory() as repository:
            base = self._repository(repository)
            command = [sys.executable, SCRIPT, "check-successor", "--repository", repository,
                       "--base", base, "--tag", "v-next", "--branch", "release/next",
                       "--preserve", "v-old=" + base, "--path", "release.txt"]
            subprocess.check_call(command, stdout=subprocess.DEVNULL)
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.check_call(command[:-1] + ["other.txt"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.check_call(["git", "tag", "-d", "v-next"], cwd=repository,
                                  stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "tag", "v-next"], cwd=repository)
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.check_call(command, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)

    def test_artifact_scan_accepts_regular_members_and_rejects_secret_traversal_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            wheel = os.path.join(temporary, "safe.whl")
            safe_raw = (b"value = 1\n"
                        b"docs=https://example.invalid/home/guide\n"
                        b"route=/api/private/resource\n")
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("agent_workboard/module.py", safe_raw)
            subprocess.check_call([sys.executable, SCRIPT, "scan-artifact", "--artifact", wheel],
                                  stdout=subprocess.DEVNULL)
            safe_sdist = os.path.join(temporary, "safe.tar.gz")
            with tarfile.open(safe_sdist, "w:gz") as archive:
                info = tarfile.TarInfo("package/module.py")
                info.size = len(safe_raw)
                info.mtime = 0
                archive.addfile(info, io.BytesIO(safe_raw))
            subprocess.check_call([sys.executable, SCRIPT, "scan-artifact", "--artifact", safe_sdist],
                                  stdout=subprocess.DEVNULL)
            unsafe_archive = os.path.join(temporary, "secret.whl")
            with zipfile.ZipFile(unsafe_archive, "w") as archive:
                archive.writestr("agent_workboard/module.py",
                                 "api_" + "key=abcdefghijk\n")
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.check_call([sys.executable, SCRIPT, "scan-artifact", "--artifact", unsafe_archive],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            forbidden_payloads = {
                "generic-pkcs8": b"-----BEGIN " + b"PRIVATE KEY-----\nencoded\n",
                "ec-private-key": b"-----BEGIN " + b"EC PRIVATE KEY-----\nencoded\n",
                "dsa-private-key": b"-----BEGIN " + b"DSA PRIVATE KEY-----\nencoded\n",
                "macos-private-path": b"build_path=" + b"/" + b"private/tmp/private/worktree\n",
                "linux-home-path": b"build_path=" + b"/" + b"home/release/worktree\n",
                "windows-users-path": b"build_path=" + b"C:" + b"\\Users\\release\\worktree\n",
            }
            for label, raw in forbidden_payloads.items():
                with self.subTest(label=label, artifact="wheel"):
                    unsafe_wheel = os.path.join(temporary, label + ".whl")
                    with zipfile.ZipFile(unsafe_wheel, "w") as archive:
                        archive.writestr("agent_workboard/payload.txt", raw)
                    with self.assertRaises(subprocess.CalledProcessError):
                        subprocess.check_call(
                            [sys.executable, SCRIPT, "scan-artifact", "--artifact", unsafe_wheel],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                with self.subTest(label=label, artifact="sdist"):
                    unsafe_sdist = os.path.join(temporary, label + ".tar.gz")
                    with tarfile.open(unsafe_sdist, "w:gz") as archive:
                        info = tarfile.TarInfo("package/payload.txt")
                        info.size = len(raw)
                        info.mtime = 0
                        archive.addfile(info, io.BytesIO(raw))
                    with self.assertRaises(subprocess.CalledProcessError):
                        subprocess.check_call(
                            [sys.executable, SCRIPT, "scan-artifact", "--artifact", unsafe_sdist],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
            traversal = os.path.join(temporary, "traversal.whl")
            with zipfile.ZipFile(traversal, "w") as archive:
                archive.writestr("../escape", "x")
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.check_call([sys.executable, SCRIPT, "scan-artifact", "--artifact", traversal],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sdist = os.path.join(temporary, "unsafe.tar.gz")
            with tarfile.open(sdist, "w:gz") as archive:
                info = tarfile.TarInfo("package/link")
                info.type = tarfile.SYMTYPE
                info.linkname = "target"
                archive.addfile(info, io.BytesIO())
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.check_call([sys.executable, SCRIPT, "scan-artifact", "--artifact", sdist],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
