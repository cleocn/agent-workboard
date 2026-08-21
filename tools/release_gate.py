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


def check_successor(repository, base, candidate, v01_commit, allowlist):
    repository = os.path.realpath(repository)
    base = run(repository, "rev-parse", base + "^{commit}").strip()
    candidate = run(repository, "rev-parse", candidate + "^{commit}").strip()
    v01_commit = run(repository, "rev-parse", v01_commit + "^{commit}").strip()
    if subprocess.call(["git", "merge-base", "--is-ancestor", base, candidate], cwd=repository):
        raise ValueError("candidate is not a descendant of the verified public main")
    parents = run(repository, "rev-list", "--parents", "-n", "1", candidate).split()
    if len(parents) != 2 or parents[1] != base:
        raise ValueError("release candidate must be the direct normal successor of public main")
    if subprocess.call(["git", "merge-base", "--is-ancestor", v01_commit, candidate], cwd=repository):
        raise ValueError("v0.1.0 history is not preserved")
    if run(repository, "rev-parse", "v0.1.0^{commit}").strip() != v01_commit:
        raise ValueError("v0.1.0 tag moved")
    if run(repository, "rev-parse", "v0.2.0^{commit}").strip() != base:
        raise ValueError("v0.2.0 tag moved from the verified public main")
    if run(repository, "cat-file", "-t", "v0.2.1").strip() != "tag":
        raise ValueError("v0.2.1 must be an annotated tag")
    if run(repository, "rev-parse", "v0.2.1^{commit}").strip() != candidate:
        raise ValueError("v0.2.1 tag does not identify the candidate")
    exact_tags = sorted(line for line in run(repository, "tag", "--points-at", candidate).splitlines()
                        if line)
    if exact_tags != ["v0.2.1"]:
        raise ValueError("release candidate must have the unique exact v0.2.1 tag")
    changed = sorted(line for line in run(repository, "diff", "--name-only", base, candidate).splitlines() if line)
    unexpected = sorted(set(changed) - set(allowlist))
    if unexpected:
        raise ValueError("candidate changes files outside the release allowlist: " + ", ".join(unexpected))
    if run(repository, "status", "--porcelain").strip():
        raise ValueError("release candidate worktree is not clean")
    scanned = scan(repository)
    return {"base": base, "candidate": candidate, "v0.1.0": v01_commit,
            "v0.2.0": base, "v0.2.1": candidate,
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
    check.add_argument("--path", action="append", required=True)
    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("--repository", required=True)
    args = parser.parse_args(argv)
    if args.command == "check-successor":
        result = check_successor(args.repository, args.base, args.candidate, args.v01_commit, args.path)
    else:
        result = scan(args.repository)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
