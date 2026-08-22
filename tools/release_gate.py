"""Offline public-lineage and reachable-object release gate."""

import argparse
import hashlib
import json
import os
import re
import subprocess


FORBIDDEN_CONTENT = (
    b"/" + b"Users/", b"BEGIN " + b"RSA", b"101" + b".96.", b"newstart" + b"2",
)
FORBIDDEN_PATTERNS = (re.compile(br"gho_[A-Za-z0-9_]{20,}"),)


def run(cwd, *args):
    return subprocess.check_output(["git"] + list(args), cwd=cwd).decode("utf-8")


def scan(repository):
    refs = sorted(line.strip() for line in run(repository, "for-each-ref", "--format=%(refname)").splitlines())
    objects = []
    for line in run(repository, "rev-list", "--objects", "--all").splitlines():
        object_id, _, path = line.partition(" ")
        kind = run(repository, "cat-file", "-t", object_id).strip()
        raw = subprocess.check_output(["git", "cat-file", kind, object_id], cwd=repository)
        if any(marker in raw for marker in FORBIDDEN_CONTENT) or any(pattern.search(raw) for pattern in FORBIDDEN_PATTERNS):
            raise ValueError("forbidden content in reachable object")
        objects.append({"object": object_id, "type": kind, "path": path,
                        "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)})
    return {"refs": refs, "objects": sorted(objects, key=lambda item: (item["type"], item["object"]))}


def check_successor(repository, base, candidate, preserved, allowlist,
                    expected_branch="release/awb-011-v0.3.0b1"):
    repository = os.path.realpath(repository)
    base = run(repository, "rev-parse", base + "^{commit}").strip()
    candidate = run(repository, "rev-parse", candidate + "^{commit}").strip()
    if subprocess.call(["git", "merge-base", "--is-ancestor", base, candidate], cwd=repository):
        raise ValueError("candidate is not a descendant of the verified public main")
    parents = run(repository, "rev-list", "--parents", "-n", "1", candidate).split()
    if len(parents) != 2 or parents[1] != base:
        raise ValueError("release candidate must be the direct normal successor of public main")
    if run(repository, "branch", "--show-current").strip() != expected_branch:
        raise ValueError("release candidate is not on the approved release branch")
    for tag, expected in sorted(preserved.items()):
        expected = run(repository, "rev-parse", expected + "^{commit}").strip()
        if run(repository, "cat-file", "-t", tag).strip() != "tag":
            raise ValueError(tag + " must remain an annotated tag")
        if run(repository, "rev-parse", tag + "^{commit}").strip() != expected:
            raise ValueError(tag + " moved")
        if subprocess.call(["git", "merge-base", "--is-ancestor", expected, candidate], cwd=repository):
            raise ValueError(tag + " history is not preserved")
    if run(repository, "cat-file", "-t", "v0.3.0b1").strip() != "tag":
        raise ValueError("v0.3.0b1 must be an annotated tag")
    if run(repository, "rev-parse", "v0.3.0b1^{commit}").strip() != candidate:
        raise ValueError("v0.3.0b1 tag does not identify the candidate")
    exact_tags = sorted(line for line in run(repository, "tag", "--points-at", candidate).splitlines()
                        if line)
    if exact_tags != ["v0.3.0b1"]:
        raise ValueError("release candidate must have the unique exact v0.3.0b1 tag")
    changed = sorted(line for line in run(repository, "diff", "--name-only", base, candidate).splitlines() if line)
    if (set(changed) != set(allowlist) or len(changed) != len(set(allowlist)) or
            len(allowlist) != len(set(allowlist))):
        raise ValueError("candidate changed paths differ from the exact release allowlist")
    if run(repository, "status", "--porcelain").strip():
        raise ValueError("release candidate worktree is not clean")
    scanned = scan(repository)
    return {"base": base, "candidate": candidate, "preservedTags": preserved,
            "v0.3.0b1": candidate,
            "changedPaths": changed, "reachableObjectCount": len(scanned["objects"]),
            "refs": scanned["refs"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="offline public successor gate; never pushes")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-successor")
    check.add_argument("--repository", required=True)
    check.add_argument("--base", required=True)
    check.add_argument("--candidate", default="HEAD")
    check.add_argument("--v0.1-commit", dest="v01_commit", required=True)
    check.add_argument("--v0.2-commit", dest="v02_commit", required=True)
    check.add_argument("--v0.2.1-commit", dest="v021_commit", required=True)
    check.add_argument("--branch", default="release/awb-011-v0.3.0b1")
    check.add_argument("--path", action="append", required=True)
    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("--repository", required=True)
    args = parser.parse_args(argv)
    if args.command == "check-successor":
        preserved = {"v0.1.0": args.v01_commit, "v0.2.0": args.v02_commit,
                     "v0.2.1": args.v021_commit}
        result = check_successor(args.repository, args.base, args.candidate, preserved,
                                 args.path, expected_branch=args.branch)
    else:
        result = scan(args.repository)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
