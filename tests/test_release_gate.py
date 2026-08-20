import json
import os
import subprocess
import sys
import tempfile
import unittest


class ReleaseGateTest(unittest.TestCase):
    def test_export_root_has_no_parent_and_rejected_only_blob_is_unreachable(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "source")
            os.mkdir(source)
            with open(os.path.join(source, "approved.txt"), "w", encoding="utf-8") as handle:
                handle.write("approved tree")
            subprocess.check_call(["git", "init"], cwd=source)
            subprocess.check_call(["git", "add", "approved.txt"], cwd=source)
            subprocess.check_call(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                                   "commit", "-m", "private source"], cwd=source)
            with open(os.path.join(source, "ignored-private.txt"), "w", encoding="utf-8") as handle:
                handle.write("/" + "Users/private/rejected-only fixture")
            rejected = subprocess.check_output(["git", "hash-object", "-w", "--stdin"], cwd=source,
                                               input=b"rejected-only fixture").decode("ascii").strip()
            output = os.path.join(root, "public-root")
            script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "release_gate.py")
            encoded = subprocess.check_output([sys.executable, script, "export-root", "--tree", source,
                                                "--output", output])
            manifest = json.loads(encoded.decode("utf-8"))
            self.assertEqual(["refs/heads/main"], manifest["refs"])
            self.assertTrue(manifest["objects"])
            for item in manifest["objects"]:
                self.assertIn(item["type"], ("blob", "commit", "tree", "tag"))
                self.assertEqual(64, len(item["sha256"]))
                self.assertIsInstance(item["size"], int)
            parent_count = subprocess.check_output(["git", "rev-list", "--parents", "-n", "1", "HEAD"],
                                                   cwd=output).split()
            reachable = subprocess.check_output(["git", "rev-list", "--objects", "--all"], cwd=output)
            self.assertEqual(1, len(parent_count))
            self.assertNotIn(rejected.encode("ascii"), reachable)
            self.assertFalse(os.path.exists(os.path.join(output, "ignored-private.txt")))
            self.assertEqual(b"", subprocess.check_output(["git", "status", "--porcelain"], cwd=output))


if __name__ == "__main__":
    unittest.main()
