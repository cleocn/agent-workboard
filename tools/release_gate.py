"""Offline AWB-004 release-root constructor and reachable-object scanner."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile


FORBIDDEN_CONTENT = (
    b"/" + b"Users/", b"BEGIN " + b"RSA", b"101" + b".96.", b"newstart" + b"2",
)
FORBIDDEN_PATTERNS = (re.compile(br"gho_[A-Za-z0-9_]{20,}"),)


def run(cwd, *args):
    return subprocess.check_output(["git"] + list(args), cwd=cwd).decode("utf-8")


def run_bytes(cwd, *args):
    return subprocess.check_output(["git"] + list(args), cwd=cwd)


def _copy_tree(source, destination):
    if run(source, "status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("source working tree must be clean")
    for record in run_bytes(source, "ls-tree", "-r", "-z", "HEAD").split(b"\0"):
        if not record:
            continue
        metadata, relative_bytes = record.split(b"\t", 1)
        relative = relative_bytes.decode("utf-8")
        mode, kind, _ = metadata.decode("ascii").split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError("approved tree contains unsupported entry: " + relative)
        target = os.path.join(destination, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(run_bytes(source, "show", "HEAD:" + relative))


def scan(repository, allowlist):
    refs = [line.split()[1] for line in run(
        repository, "for-each-ref", "--format=%(objectname) %(refname)").splitlines()]
    if sorted(refs) != sorted(allowlist):
        raise ValueError("ref allowlist mismatch")
    objects = []
    for line in run(repository, "rev-list", "--objects", "--all").splitlines():
        object_id, _, path = line.partition(" ")
        kind = run(repository, "cat-file", "-t", object_id).strip()
        raw = subprocess.check_output(["git", "cat-file", kind, object_id], cwd=repository)
        if any(marker in raw for marker in FORBIDDEN_CONTENT) or any(pattern.search(raw) for pattern in FORBIDDEN_PATTERNS):
            raise ValueError("forbidden content in reachable object")
        objects.append({"object": object_id, "type": kind, "path": path,
                        "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)})
    return {"refs": sorted(refs), "objects": sorted(objects, key=lambda item: (item["type"], item["object"]))}


def export_root(source, destination, branch):
    source = os.path.realpath(source)
    destination = os.path.realpath(destination)
    if os.path.exists(destination):
        raise ValueError("destination already exists")
    if os.path.commonpath((source, destination)) == source:
        raise ValueError("destination must not be inside source tree")
    staging = tempfile.mkdtemp(prefix="awb-release-root-", dir=os.path.dirname(destination))
    try:
        _copy_tree(source, staging)
        run(staging, "init")
        run(staging, "add", ".")
        env = os.environ.copy()
        env.update({"GIT_AUTHOR_NAME": "AWB Release Gate", "GIT_AUTHOR_EMAIL": "release@example.invalid",
                    "GIT_COMMITTER_NAME": "AWB Release Gate", "GIT_COMMITTER_EMAIL": "release@example.invalid"})
        # Do not let Git's human-oriented commit chatter corrupt the CLI's
        # single JSON document on stdout.  Preserve stdout and stderr
        # separately for a caller inspecting a failed subprocess.
        committed = subprocess.run(["git", "commit", "--no-gpg-sign", "-m", "AWB approved file tree"],
                                   cwd=staging, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        if committed.returncode:
            raise subprocess.CalledProcessError(committed.returncode, committed.args,
                                                output=committed.stdout, stderr=committed.stderr)
        run(staging, "branch", "-M", branch)
        if len(run(staging, "rev-list", "--parents", "-n", "1", "HEAD").split()) != 1:
            raise ValueError("public root must have no parent")
        manifest = scan(staging, ["refs/heads/" + branch])
        os.rename(staging, destination)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="offline release-root constructor; never pushes")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export-root")
    export.add_argument("--tree", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--branch", default="main")
    check = sub.add_parser("scan")
    check.add_argument("--repository", required=True)
    check.add_argument("--ref", action="append", required=True)
    args = parser.parse_args(argv)
    if args.command == "export-root":
        print(json.dumps(export_root(args.tree, args.output, args.branch), sort_keys=True))
    else:
        print(json.dumps(scan(args.repository, args.ref), sort_keys=True))


if __name__ == "__main__":
    main()
