"""Lean offline Preview gate: exact successor/path checks and artifact scan."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import zipfile


FORBIDDEN_CONTENT = (
    b"BEGIN " + b"RSA", b"BEGIN OPENSSH PRIVATE KEY",
    b"-----BEGIN PRIVATE KEY-----", b"101" + b".96.", b"newstart" + b"2",
)
FORBIDDEN_PATTERNS = (
    re.compile(br"gh[opsu]_[A-Za-z0-9_]{20,}"),
    re.compile(br"(?i)(password|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*[^\s]{8,}"),
    re.compile(br"-----BEGIN (?:[A-Z0-9]+ )+PRIVATE KEY-----"),
    re.compile(br"(?i)(?:^|[\s=:'\"(]|file://)/(?:Users|private|home|root|tmp|var/(?:folders|tmp))/"),
    re.compile(br"(?i)(?:^|[\s=:'\"(])[A-Z]:[\\/](?:Users|Documents and Settings|Temp|tmp|work|workspace)[\\/]"),
)


def run(cwd, *args):
    return subprocess.check_output(["git"] + list(args), cwd=cwd).decode("utf-8")


def _safe_member(name):
    if (not name or "\\" in name or name.startswith("/") or
            any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))):
        raise ValueError("artifact contains an unsafe member path")


def _scan_raw(name, raw):
    if any(marker in raw for marker in FORBIDDEN_CONTENT) or any(
            pattern.search(raw) for pattern in FORBIDDEN_PATTERNS):
        raise ValueError("forbidden content in artifact member: " + name)
    return {"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}


def scan_artifact(path):
    path = os.path.realpath(os.path.abspath(path))
    if not os.path.isfile(path) or os.path.islink(path):
        raise ValueError("artifact must be a regular non-symlink file")
    members = []
    if path.endswith(".whl"):
        with zipfile.ZipFile(path) as archive:
            seen = set()
            for info in archive.infolist():
                _safe_member(info.filename)
                if info.filename in seen:
                    raise ValueError("artifact contains duplicate members")
                seen.add(info.filename)
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.is_dir():
                    continue
                if stat.S_IFMT(mode) and not stat.S_ISREG(mode):
                    raise ValueError("artifact contains a non-regular member")
                members.append(_scan_raw(info.filename, archive.read(info)))
    elif path.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as archive:
            seen = set()
            for info in archive.getmembers():
                _safe_member(info.name)
                if info.name in seen:
                    raise ValueError("artifact contains duplicate members")
                seen.add(info.name)
                if info.isdir():
                    continue
                if not info.isfile():
                    raise ValueError("artifact contains a non-regular member")
                handle = archive.extractfile(info)
                if handle is None:
                    raise ValueError("artifact member cannot be read")
                members.append(_scan_raw(info.name, handle.read()))
    else:
        raise ValueError("artifact type must be wheel or gzipped sdist")
    if not members:
        raise ValueError("artifact contains no regular members")
    with open(path, "rb") as handle:
        artifact_sha = hashlib.sha256(handle.read()).hexdigest()
    return {"artifact": path, "sha256": artifact_sha,
            "memberCount": len(members), "members": sorted(members, key=lambda row: row["path"]),
            "privacy": "ALLOWLIST_ONLY"}


def check_successor(repository, base, candidate, tag, branch, preserved, allowlist):
    repository = os.path.realpath(repository)
    base = run(repository, "rev-parse", base + "^{commit}").strip()
    candidate = run(repository, "rev-parse", candidate + "^{commit}").strip()
    parents = run(repository, "rev-list", "--parents", "-n", "1", candidate).split()
    if len(parents) != 2 or parents[1] != base:
        raise ValueError("release candidate must be the direct single-parent successor")
    if run(repository, "branch", "--show-current").strip() != branch:
        raise ValueError("release candidate is not on the approved release branch")
    if run(repository, "cat-file", "-t", tag).strip() != "tag":
        raise ValueError(tag + " must be an annotated tag")
    if run(repository, "rev-parse", tag + "^{commit}").strip() != candidate:
        raise ValueError(tag + " does not identify the candidate")
    for preserved_tag, expected in sorted(preserved.items()):
        expected_commit = run(repository, "rev-parse", expected + "^{commit}").strip()
        if run(repository, "cat-file", "-t", preserved_tag).strip() != "tag":
            raise ValueError(preserved_tag + " must remain an annotated tag")
        if run(repository, "rev-parse", preserved_tag + "^{commit}").strip() != expected_commit:
            raise ValueError(preserved_tag + " moved")
        if subprocess.call(["git", "merge-base", "--is-ancestor", expected_commit, candidate],
                           cwd=repository):
            raise ValueError(preserved_tag + " history is not preserved")
    changed = sorted(line for line in run(
        repository, "diff", "--name-only", base, candidate
    ).splitlines() if line)
    if changed != sorted(allowlist) or len(allowlist) != len(set(allowlist)):
        raise ValueError("candidate changed paths differ from the exact release allowlist")
    if run(repository, "status", "--porcelain").strip():
        raise ValueError("release candidate worktree is not clean")
    return {"base": base, "candidate": candidate, "tag": tag, "branch": branch,
            "preservedTags": preserved, "changedPaths": changed, "status": "PASS"}


def _preserved(values):
    result = {}
    for value in values:
        tag, separator, commit = value.partition("=")
        if not separator or not tag or not commit or tag in result:
            raise ValueError("--preserve must be unique TAG=COMMIT values")
        result[tag] = commit
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="lean offline Preview release gate")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-successor")
    check.add_argument("--repository", required=True)
    check.add_argument("--base", required=True)
    check.add_argument("--candidate", default="HEAD")
    check.add_argument("--tag", required=True)
    check.add_argument("--branch", required=True)
    check.add_argument("--preserve", action="append", default=[])
    check.add_argument("--path", action="append", required=True)
    artifact = sub.add_parser("scan-artifact")
    artifact.add_argument("--artifact", required=True)
    args = parser.parse_args(argv)
    if args.command == "check-successor":
        result = check_successor(args.repository, args.base, args.candidate, args.tag,
                                 args.branch, _preserved(args.preserve), args.path)
    else:
        result = scan_artifact(args.artifact)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
