"""Single verification policy, registered checks, and receipt authority.

Verification cost is policy.  Workflow integrity, exact ownership, independent
review, risky-action HUMAN authority, and a current candidate-bound PASS
receipt at the classifier hard floor are not policy.
"""

from __future__ import absolute_import

import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from . import lite
from . import workflow as workflow_kernel


CLASSIFIER_PROTOCOL = "AWB-VERIFY-CLASSIFIER-v1"
RECEIPT_PROTOCOL = "AWB-VERIFY-RECEIPT-v1"
STATUS_PROTOCOL = "AWB-VERIFY-STATUS-v1"
OVERRIDE_PROTOCOL = "AWB-VERIFY-POLICY-OVERRIDE-v1"

POLICY_ORDER = {
    "database": ("DISPOSABLE", "DISPOSABLE_PLUS_LIVE_READ_ONLY"),
    "revision": ("NONE", "FOCUSED", "FULL"),
    "final": ("REUSE_OR_RUN", "ALWAYS_RUN"),
    "profile": ("NORMAL", "PREVIEW", "CROSS_VERSION"),
    "rebuild": ("NEVER", "IF_REQUIRED", "ALWAYS"),
    "compatibility": ("CORE", "DECLARED_EXTRA", "FULL_GRAPH"),
    "identityEvidence": (
        "CANDIDATE_ONLY", "EXACT_CHANGED_PATHS",
        "EXACT_PATHS_AND_ASSET_HASHES",
    ),
    "previewCost": ("BASELINE", "STANDARD", "EXTENDED"),
}
DEFAULT_POLICY = {
    "database": "DISPOSABLE", "revision": "FOCUSED",
    "final": "REUSE_OR_RUN", "profile": "AUTO",
    "runs": {"core": 1}, "rebuild": "IF_REQUIRED",
    "compatibility": "CORE", "identityEvidence": "CANDIDATE_ONLY",
    "previewCost": "BASELINE",
}

_IGNORED_DIRECTORIES = frozenset((
    ".git", ".awb", "__pycache__", ".pytest_cache", ".mypy_cache",
    "dist", "build",
))
_FORBIDDEN_CONTENT = (
    b"BEGIN " + b"RSA", b"BEGIN " + b"OPENSSH PRIVATE KEY",
    b"-----BEGIN " + b"PRIVATE KEY-----",
)
_FORBIDDEN_PATTERNS = (
    re.compile(br"gh" + br"[opsu]_[A-Za-z0-9_]{20,}"),
    re.compile(br"(?i)(password|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*[^\s]{8,}"),
    re.compile(br"-----BEGIN (?:[A-Z0-9]+ )+PRIVATE KEY-----"),
)
_FORBIDDEN_LOCAL_PATH_PATTERNS = (
    re.compile(br"(?i)(?:^|[\s=:'\"(]|file://)/(?:Users|private|home|root|tmp|var/(?:folders|tmp))/"),
    re.compile(br"(?i)(?:^|[\s=:'\"(])[A-Z]:[\\/](?:Users|Documents and Settings|Temp|tmp|work|workspace)[\\/]"),
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _sha(value):
    if not isinstance(value, bytes):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _file_sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _event_payload(row):
    try:
        value = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _regular_source(project_root, source):
    root = os.path.realpath(os.path.abspath(project_root))
    if (not isinstance(source, str) or not source or os.path.isabs(source) or
            "\\" in source):
        raise lite.LiteError("verify source must be project-relative")
    absolute = os.path.realpath(os.path.join(root, source))
    if (os.path.commonpath((root, absolute)) != root or
            not os.path.isdir(absolute) or os.path.islink(absolute)):
        raise lite.LiteError("verify source must be a project directory")
    return root, source.replace(os.sep, "/"), absolute


def _ignored(relative, is_directory=False):
    parts = relative.replace(os.sep, "/").split("/")
    if any(part in _IGNORED_DIRECTORIES or part.endswith(".egg-info")
           for part in parts):
        return True
    return (not is_directory and
            (relative.endswith((".pyc", ".pyo")) or
             os.path.basename(relative) in (".coverage",)))


def source_identity(project_root, source):
    """Compute an exact ordinary candidate identity from owned source bytes."""
    unused_root, relative_source, absolute = _regular_source(project_root, source)
    members = []
    for directory, names, files in os.walk(absolute, topdown=True, followlinks=False):
        relative_directory = os.path.relpath(directory, absolute)
        relative_directory = "" if relative_directory == "." else relative_directory
        kept = []
        for name in sorted(names):
            relative = os.path.join(relative_directory, name) if relative_directory else name
            path = os.path.join(directory, name)
            if _ignored(relative, True):
                continue
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise lite.LiteError("verify source contains an unsafe directory")
            kept.append(name)
        names[:] = kept
        for name in sorted(files):
            relative = os.path.join(relative_directory, name) if relative_directory else name
            if _ignored(relative):
                continue
            path = os.path.join(directory, name)
            info = os.lstat(path)
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
                    info.st_nlink != 1):
                raise lite.LiteError("verify source contains a non-regular or linked file")
            members.append({
                "path": relative.replace(os.sep, "/"),
                "mode": info.st_mode & 0o777, "size": info.st_size,
                "sha256": _file_sha(path),
            })
    if not members:
        raise lite.LiteError("verify source contains no candidate files")
    descriptor = {"source": relative_source, "members": members}
    descriptor_digest = _sha(_json(descriptor))
    candidate_fingerprint = _sha(_json({
        "kind": "ORDINARY", "sourceDescriptorDigest": descriptor_digest,
    }))
    return {
        "kind": "ORDINARY", "candidateId": None,
        "candidateFingerprint": candidate_fingerprint,
        "buildFingerprint": None,
        "sourceDescriptorDigest": descriptor_digest,
        "source": relative_source,
    }


def _candidate_identity(connection, project_root, work_item_id, source,
                        candidate_id=None, allow_finalized=False):
    if candidate_id is None:
        return source_identity(project_root, source)
    from . import candidate as candidate_module
    built = candidate_module.release_submission_candidate(connection, work_item_id)
    if built is None and allow_finalized:
        rows = connection.execute(
            "SELECT event_id,event_type,payload_json FROM events "
            "WHERE work_item_id=? AND event_type IN "
            "('CANDIDATE_BUILT','CANDIDATE_QUARANTINED') ORDER BY event_id DESC",
            (work_item_id,),
        ).fetchall()
        for row in rows:
            payload = _event_payload(row)
            if payload.get("candidateId") != candidate_id:
                continue
            if row["event_type"] == "CANDIDATE_QUARANTINED":
                break
            built = {
                "candidateId": payload["candidateId"],
                "candidateFingerprint": payload["candidateFingerprint"],
                "buildFingerprint": payload["buildFingerprint"],
                "changedPaths": payload["frozen"]["changedPaths"],
            }
            break
    if built is None or built.get("candidateId") != candidate_id:
        raise lite.LiteError("VERIFY_MANAGED_CANDIDATE_NOT_CURRENT")
    candidate_module._verify_current_build(project_root, work_item_id, built)
    ordinary = source_identity(project_root, source)
    root, managed = candidate_module._managed(project_root, work_item_id)
    expected = os.path.realpath(os.path.join(
        managed, "active", candidate_id, "source"
    ))
    supplied = os.path.realpath(os.path.join(root, source))
    if supplied != expected:
        raise lite.LiteError("verify managed source does not match candidate")
    return {
        "kind": "MANAGED_RELEASE", "candidateId": candidate_id,
        "candidateFingerprint": built["candidateFingerprint"],
        "buildFingerprint": built["buildFingerprint"],
        "sourceDescriptorDigest": ordinary["sourceDescriptorDigest"],
        "source": ordinary["source"],
    }


def _changed_paths(source_absolute):
    if not os.path.isdir(os.path.join(source_absolute, ".git")):
        return []
    try:
        changed = subprocess.check_output(
            ["git", "diff", "--name-only", "HEAD"], cwd=source_absolute,
            stderr=subprocess.STDOUT,
        ).decode("utf-8").splitlines()
        untracked = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=source_absolute, stderr=subprocess.STDOUT,
        ).decode("utf-8").splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise lite.LiteError("verify candidate Git facts are unavailable: {0}".format(exc))
    return sorted(set(path for path in changed + untracked if path))


def _management(connection, work_item_id):
    value = lite._management_from_events(connection, work_item_id)
    if not isinstance(value, dict):
        raise lite.LiteError("verify requires a management envelope")
    return value


def classify(management, candidate, changed_paths=None):
    changed_paths = changed_paths or []
    text = " ".join(management.get("scope", [])).lower()
    lower_paths = [path.lower() for path in changed_paths]
    cross_tokens = ["cross_version", "cross-version", "migration", "rollback"]
    if candidate.get("kind") != "MANAGED_RELEASE":
        cross_tokens.extend(("升级", "回滚"))
    cross = (any(token in text for token in cross_tokens) or
             any(any(token in path for token in (
        "upgrade", "migration", "rollback"
    )) for path in lower_paths))
    preview = (candidate.get("kind") == "MANAGED_RELEASE" or
               any(token in text for token in ("preview", "publication", "发布")) or
               any(path in ("manifest.json", "docs/publication-plan.md") or
                   path.startswith(("src/agent_workboard/candidate.py", "tools/"))
                   for path in lower_paths))
    risk = "CROSS_VERSION" if cross else "PREVIEW" if preview else "NORMAL"
    signals = (["CROSS_VERSION_SURFACE"] if cross else
               ["PREVIEW_SURFACE"] if preview else [])
    hard = dict(DEFAULT_POLICY)
    hard.update({
        "revision": "NONE", "profile": risk, "rebuild": "NEVER",
        "runs": {"core": 1}, "compatibility": "CORE",
        "identityEvidence": "CANDIDATE_ONLY", "previewCost": "BASELINE",
    })
    recommended = dict(DEFAULT_POLICY)
    recommended["runs"] = {"core": 1}
    recommended["profile"] = risk
    if risk == "PREVIEW":
        recommended["identityEvidence"] = "EXACT_CHANGED_PATHS"
        recommended["previewCost"] = "STANDARD"
    elif risk == "CROSS_VERSION":
        recommended["compatibility"] = "DECLARED_EXTRA"
    return {
        "version": CLASSIFIER_PROTOCOL, "riskClass": risk,
        "signals": signals, "hardFloor": hard,
        "recommendedPolicy": recommended,
    }


def _management_basis(management):
    """Bind semantic management inputs without volatile workflow projection."""
    tasks = []
    for task in management.get("tasks", []):
        tasks.append({key: task.get(key) for key in (
            "taskId", "seq", "title", "ownerRole", "required",
            "acceptance", "closureEvidenceRequired",
        )})
    semantic = {key: management.get(key) for key in (
        "contractVersion", "templateContractVersion", "workItemId", "type",
        "mode", "title", "priority", "scope", "outOfScope", "authorization",
        "safetyConstraints", "acceptance", "closure", "stages",
    )}
    semantic["tasks"] = tasks
    acceptance = []
    for entry in management.get("acceptance", []):
        if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or
                not entry["id"] or not isinstance(entry.get("criterion"), str) or
                not entry["criterion"]):
            raise lite.LiteError("VERIFY_MANAGEMENT_ACCEPTANCE_INVALID")
        acceptance.append({
            "id": entry["id"],
            "criterionDigest": _sha(entry["criterion"]),
        })
    acceptance.sort(key=lambda value: value["id"])
    if not acceptance or len({entry["id"] for entry in acceptance}) != len(acceptance):
        raise lite.LiteError("VERIFY_MANAGEMENT_ACCEPTANCE_INVALID")
    return {"digest": _sha(_json(semantic)), "acceptance": acceptance}


def _normalize_runs(value):
    value = value if value is not None else {"core": 1}
    if (not isinstance(value, dict) or not value or
            any(not isinstance(key, str) or not key or type(count) is not int or count < 0
                for key, count in value.items())):
        raise lite.LiteError("verify runs policy is invalid")
    return dict(sorted(value.items()))


def _normalize_policy(requested, risk_class):
    requested = requested or {}
    unknown = set(requested) - set(DEFAULT_POLICY)
    if unknown:
        raise lite.LiteError("unknown verify policy field")
    result = dict(DEFAULT_POLICY)
    result["runs"] = dict(DEFAULT_POLICY["runs"])
    result.update(requested)
    result["runs"] = _normalize_runs(result.get("runs"))
    if result["profile"] == "AUTO":
        result["profile"] = risk_class
    for name, order in POLICY_ORDER.items():
        if result.get(name) not in order:
            raise lite.LiteError("unknown verify policy value: " + name)
    return result


def _at_least(actual, expected, name):
    return POLICY_ORDER[name].index(actual) >= POLICY_ORDER[name].index(expected)


def policy_decision(classifier, requested=None):
    hard, recommended = classifier["hardFloor"], classifier["recommendedPolicy"]
    if requested:
        effective = _normalize_policy(requested, classifier["riskClass"])
    else:
        effective = dict(recommended)
        effective["runs"] = dict(recommended["runs"])
    below_floor = [name for name in POLICY_ORDER
                   if not _at_least(effective[name], hard[name], name)]
    for check_id, count in hard["runs"].items():
        if effective["runs"].get(check_id, 0) < count:
            below_floor.append("runs:" + check_id)
    if below_floor:
        raise lite.LiteError("VERIFY_POLICY_BELOW_HARD_FLOOR:" + ",".join(below_floor))
    downgraded = [name for name in POLICY_ORDER
                  if not _at_least(effective[name], recommended[name], name)]
    for check_id, count in recommended["runs"].items():
        if effective["runs"].get(check_id, 0) < count:
            downgraded.append("runs:" + check_id)
    return {"requested": requested or dict(DEFAULT_POLICY),
            "effective": effective, "overrideRequired": bool(downgraded),
            "downgraded": sorted(downgraded)}


def _path_snapshot(paths):
    result = []
    for path in sorted(set(paths)):
        try:
            info = os.stat(path)
        except OSError:
            result.append({"pathDigest": _sha(path), "exists": False})
            continue
        result.append({
            "pathDigest": _sha(path), "exists": True,
            "device": info.st_dev, "inode": info.st_ino,
            "size": info.st_size, "mtimeNs": getattr(
                info, "st_mtime_ns", int(info.st_mtime * 1000000000)),
            "sha256": _file_sha(path) if stat.S_ISREG(info.st_mode) else None,
        })
    return result


def _live_deny_paths(project_root, database):
    root = os.path.realpath(os.path.abspath(project_root))
    database = os.path.realpath(os.path.abspath(database))
    paths = [database, database + "-wal", database + "-shm"]
    awb = os.path.join(root, ".awb")
    if os.path.isdir(awb):
        for name in os.listdir(awb):
            if "backup" in name.lower():
                paths.append(os.path.join(awb, name))
        releases = os.path.join(awb, "release-candidates")
        if os.path.isdir(releases):
            for directory, names, unused_files in os.walk(releases):
                if os.path.basename(directory) == "staging":
                    paths.append(directory)
                    names[:] = []
    return paths


_GUARD_SOURCE = r'''
import builtins, functools, io, json, os, sqlite3, subprocess
_paths = set(json.loads(os.environ["AWB_VERIFY_DENY_PATHS"]))
_inodes = set(tuple(value) for value in json.loads(os.environ["AWB_VERIFY_DENY_INODES"]))
_guard_dir = os.environ["AWB_VERIFY_GUARD_DIR"]
def _path(value):
    try: return os.path.realpath(os.path.abspath(os.fspath(value)))
    except (TypeError, ValueError): return None
def _denied(value):
    path = _path(value)
    if path and any(path == denied or path.startswith(denied + os.sep)
                    for denied in _paths): return True
    try:
        info = os.stat(path)
        return (info.st_dev, info.st_ino) in _inodes
    except (OSError, TypeError): return False
def _check(value):
    if _denied(value): raise PermissionError("VERIFY_LIVE_DB_WRITE_DENIED")
_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in "wax+"): _check(file)
    return _open(file, mode, *args, **kwargs)
builtins.open = guarded_open; io.open = guarded_open
_os_open = os.open
def guarded_os_open(path, flags, *args, **kwargs):
    if flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND): _check(path)
    return _os_open(path, flags, *args, **kwargs)
# pathlib stores os.open as a class attribute.  A partial remains callable
# without becoming a bound Python method and preserves the native signature.
os.open = functools.partial(guarded_os_open)
for _name in ("unlink", "remove", "truncate"):
    _original = getattr(os, _name)
    def _make(original):
        def guarded(path, *args, **kwargs): _check(path); return original(path, *args, **kwargs)
        return guarded
    setattr(os, _name, _make(_original))
for _name in ("rename", "replace", "link", "symlink"):
    _original = getattr(os, _name)
    def _make2(original):
        def guarded(source, target, *args, **kwargs):
            _check(source); _check(target); return original(source, target, *args, **kwargs)
        return guarded
    setattr(os, _name, _make2(_original))
_connect = sqlite3.connect
def guarded_connect(database, *args, **kwargs):
    text = str(database)
    path = text[5:].split("?", 1)[0] if text.startswith("file:") else text
    if _denied(path):
        query = text.split("?", 1)[1] if "?" in text else ""
        if not (kwargs.get("uri") and "mode=ro" in query and "immutable=1" in query):
            raise PermissionError("VERIFY_LIVE_DB_WRITE_DENIED")
    return _connect(database, *args, **kwargs)
sqlite3.connect = guarded_connect
_popen = subprocess.Popen
class GuardedPopen(_popen):
    def __init__(self, *args, **kwargs):
        env = dict(kwargs.get("env") or os.environ)
        current = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = _guard_dir + (os.pathsep + current if current else "")
        kwargs["env"] = env
        _popen.__init__(self, *args, **kwargs)
subprocess.Popen = GuardedPopen
os.environ["AWB_VERIFY_GUARD_ACTIVE"] = "1"
'''


def _sandboxed_command(command, deny_paths, profile_path):
    if os.environ.get("AWB_VERIFY_GUARD_ACTIVE") == "1":
        # A verifier self-test already runs under the outer package sandbox.
        # Its nested disposable target still receives the path/inode guard,
        # while the real workspace remains protected by the outer OS policy.
        return command
    executable = "/usr/bin/sandbox-exec"
    if sys.platform != "darwin" or not os.path.isfile(executable):
        raise lite.LiteError("VERIFY_LIVE_DB_GUARD_UNAVAILABLE")
    clauses = []
    for path in sorted(set(os.path.realpath(value) for value in deny_paths)):
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        clauses.append('(deny file-write* (literal "{0}"))'.format(escaped))
        clauses.append('(deny file-write* (subpath "{0}"))'.format(escaped))
    with open(profile_path, "w", encoding="utf-8") as handle:
        handle.write("(version 1)\n(allow default)\n" + "\n".join(clauses) + "\n")
    return [executable, "-f", profile_path] + command


def _managed_wheel_for_source(source_absolute):
    candidate_root = os.path.dirname(source_absolute)
    manifest_path = os.path.join(candidate_root, "candidate.json")
    if not os.path.isfile(manifest_path) or os.path.islink(manifest_path):
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        marker = os.sep + ".awb" + os.sep + "release-candidates" + os.sep
        project_root, separator, unused_tail = source_absolute.partition(marker)
        if not separator or manifest.get("lifecycle") != "BUILT":
            return None
        assets = os.path.join(project_root, manifest["buildPath"], "assets")
        wheels = [os.path.join(assets, name) for name in os.listdir(assets)
                  if name.endswith(".whl")]
        if len(wheels) != 1 or os.path.islink(wheels[0]):
            return None
        return wheels[0]
    except (IOError, OSError, TypeError, ValueError, KeyError):
        return None


def _copy_execution_source(source_absolute, target, managed_wheel=None):
    def ignore(directory, names):
        relative = os.path.relpath(directory, source_absolute)
        relative = "" if relative == "." else relative
        return [name for name in names if _ignored(
            os.path.join(relative, name) if relative else name,
            os.path.isdir(os.path.join(directory, name)),
        )]
    shutil.copytree(source_absolute, target, symlinks=False, ignore=ignore)
    fixtures = []
    distribution = os.path.join(source_absolute, "dist")
    if os.path.isdir(distribution):
        wheels = [name for name in sorted(os.listdir(distribution))
                  if name.endswith(".whl")]
        if len(wheels) > 1:
            raise lite.LiteError("verify execution has ambiguous wheel fixtures")
        for name in wheels:
            source = os.path.join(distribution, name)
            info = os.lstat(source)
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
                    info.st_nlink != 1):
                raise lite.LiteError("verify execution wheel fixture is unsafe")
            destination = os.path.join(target, "dist", name)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copyfile(source, destination)
            fixtures.append({"path": "dist/" + name, "sha256": _file_sha(source)})
    managed_wheel = managed_wheel or _managed_wheel_for_source(source_absolute)
    if managed_wheel is not None:
        if fixtures:
            raise lite.LiteError("verify execution has ambiguous wheel fixtures")
        name = os.path.basename(managed_wheel)
        destination = os.path.join(target, "dist", name)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copyfile(managed_wheel, destination)
        fixtures.append({"path": "dist/" + name,
                         "sha256": _file_sha(managed_wheel)})
    return fixtures


def _registered_check_plan(phase, policy):
    """Return the only deterministic mapping from effective policy to work."""
    policy = _normalize_policy(policy, policy.get("profile", "NORMAL"))
    dimensions = sorted(DEFAULT_POLICY)
    if phase == "FINAL" or policy["revision"] == "FULL":
        core_command = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    elif policy["revision"] == "FOCUSED":
        core_command = [sys.executable, "-m", "unittest", "tests.test_verify"]
    else:
        core_command = [
            sys.executable, "-m", "unittest",
            "tests.test_verify.VerifyPolicyTest."
            "test_classifier_and_policy_floor_are_single_monotonic_authority",
        ]
    plan = []
    for unused in range(max(policy.get("runs", {}).get("core", 1), 1)):
        plan.append({
            "checkId": "registered-unittest-" + phase.lower(),
            "kind": "UNITTEST", "command": core_command,
            "dimensions": dimensions,
        })
    if policy["database"] == "DISPOSABLE_PLUS_LIVE_READ_ONLY":
        plan.append({"checkId": "registered-live-read-only", "kind": "LIVE_READ_ONLY",
                     "dimensions": ["database"]})
    if policy["final"] == "ALWAYS_RUN":
        plan.append({
            "checkId": "registered-final-always-run", "kind": "UNITTEST",
            "command": [sys.executable, "-m", "unittest",
                        "tests.test_verify.VerifyPolicyTest."
                        "test_final_receipt_is_candidate_bound_replay_safe_and_status_visible"],
            "dimensions": ["final"],
        })
    if policy["profile"] in ("PREVIEW", "CROSS_VERSION"):
        plan.append({"checkId": "registered-preview-artifact", "kind": "ARTIFACT",
                     "dimensions": ["profile"]})
    if policy["profile"] == "CROSS_VERSION":
        plan.append({
            "checkId": "registered-cross-version-core", "kind": "UNITTEST",
            "command": [sys.executable, "-m", "unittest",
                        "tests.test_project.ProjectLifecycleTest."
                        "test_exact_b6_direct_upgrade_and_rollback_use_state_reliability_edge"],
            "dimensions": (["profile", "compatibility"]
                           if policy["compatibility"] != "CORE" else ["profile"]),
        })
    if (policy["rebuild"] == "ALWAYS" or
            policy["rebuild"] == "IF_REQUIRED" and
            policy["profile"] in ("PREVIEW", "CROSS_VERSION")):
        plan.append({
            "checkId": "registered-rebuild", "kind": "UNITTEST",
            "command": [sys.executable, "-m", "unittest",
                        "tests.test_release_build.ReleaseBuildTest."
                        "test_one_tagged_build_preserves_identity_and_templates"],
            "dimensions": ["rebuild"],
        })
    if (policy["compatibility"] in ("DECLARED_EXTRA", "FULL_GRAPH") and
            policy["profile"] != "CROSS_VERSION"):
        plan.append({
            "checkId": "registered-compatibility-declared", "kind": "UNITTEST",
            "command": [sys.executable, "-m", "unittest",
                        "tests.test_project.ProjectLifecycleTest."
                        "test_exact_b6_direct_upgrade_and_rollback_use_state_reliability_edge"],
            "dimensions": ["compatibility"],
        })
    if policy["compatibility"] == "FULL_GRAPH":
        plan.append({
            "checkId": "registered-compatibility-full-graph", "kind": "UNITTEST",
            "command": [sys.executable, "-m", "unittest", "tests.test_project"],
            "dimensions": ["compatibility"],
        })
    if policy["identityEvidence"] != "CANDIDATE_ONLY":
        plan.append({"checkId": "registered-identity-evidence", "kind": "IDENTITY",
                     "dimensions": ["identityEvidence"]})
    if policy["previewCost"] in ("STANDARD", "EXTENDED"):
        plan.append({
            "checkId": "registered-preview-fresh-install", "kind": "UNITTEST",
            "command": [sys.executable, "-m", "unittest",
                        "tests.test_project.ProjectLifecycleTest."
                        "test_real_pip_wheel_init_and_doctor_work_from_an_unrelated_directory"],
            "dimensions": ["previewCost"],
        })
    if policy["previewCost"] == "EXTENDED":
        plan.append({
            "checkId": "registered-preview-package-seam", "kind": "UNITTEST",
            "command": [sys.executable, "-m", "unittest", "tests.test_release_build"],
            "dimensions": ["previewCost"],
        })
    return plan


def _run_registered_checks(project_root, database, source_absolute, phase, policy):
    deny_paths = _live_deny_paths(project_root, database)
    before = _path_snapshot(deny_paths)
    with tempfile.TemporaryDirectory(prefix="awb-verify-") as temporary:
        execution = os.path.join(temporary, "source")
        fixtures = _copy_execution_source(source_absolute, execution)
        awb = os.path.join(execution, ".awb")
        os.makedirs(awb)
        disposable_database = os.path.join(awb, "workboard.db")
        lite.initialize_database(disposable_database, schema_path=None)
        with open(os.path.join(awb, "config.json"), "w", encoding="utf-8") as handle:
            json.dump({
                "configVersion": 1, "database": ".awb/workboard.db",
                "projectId": "verify-disposable", "repositoryKey": "verify-disposable",
                "requiredPackageVersion": "0.0.0", "requiredSourceCommit": "0" * 40,
                "requiredSourceTree": "0" * 40, "requiredSourceTag": "verify-disposable",
                "runtimeMode": "development", "usagePolicy": "OFF",
            }, handle, sort_keys=True)
        guard = os.path.join(temporary, "guard")
        os.makedirs(guard)
        with open(os.path.join(guard, "sitecustomize.py"), "w", encoding="utf-8") as handle:
            handle.write(_GUARD_SOURCE)
        inodes = []
        for path in deny_paths:
            try:
                info = os.stat(path); inodes.append([info.st_dev, info.st_ino])
            except OSError:
                pass
        env = dict(os.environ)
        env.update({
            "AWB_VERIFY_DENY_PATHS": json.dumps([os.path.realpath(path) for path in deny_paths]),
            "AWB_VERIFY_DENY_INODES": json.dumps(inodes),
            "AWB_VERIFY_GUARD_DIR": guard, "PYTHONPATH": guard + os.pathsep +
            os.path.join(execution, "src"), "PYTHONDONTWRITEBYTECODE": "1",
        })
        probe = subprocess.run(
            [sys.executable, "-c", "import os;print(os.environ.get('AWB_VERIFY_GUARD_ACTIVE',''))"],
            cwd=execution, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if probe.returncode or probe.stdout.decode("utf-8", "replace").strip() != "1":
            raise lite.LiteError("VERIFY_LIVE_DB_GUARD_UNAVAILABLE")
        checks = []
        changed_paths = _changed_paths(source_absolute)
        for entry in _registered_check_plan(phase, policy):
            result, details = "PASS", {}
            if entry["kind"] == "UNITTEST":
                command = _sandboxed_command(
                    entry["command"], deny_paths,
                    os.path.join(temporary, "sandbox.sb"),
                )
                completed = subprocess.run(
                    command, cwd=execution, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                combined = completed.stdout + completed.stderr
                count = re.search(br"Ran ([0-9]+) tests?", combined)
                result = "PASS" if completed.returncode == 0 else "FAIL"
                details = {"exitCode": completed.returncode,
                           "testCount": int(count.group(1)) if count else None}
            elif entry["kind"] == "LIVE_READ_ONLY":
                uri = "file:" + os.path.realpath(database) + "?mode=ro&immutable=1"
                live = sqlite3.connect(uri, uri=True)
                try:
                    count = live.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
                finally:
                    live.close()
                details = {"schemaObjectCount": count,
                           "snapshotDigest": _sha(_json(before))}
            elif entry["kind"] == "IDENTITY":
                details = {"changedPaths": changed_paths}
                if policy["identityEvidence"] == "EXACT_PATHS_AND_ASSET_HASHES":
                    details["assetHashes"] = fixtures
            elif entry["kind"] == "ARTIFACT":
                wheel_paths = [os.path.join(execution, value["path"])
                               for value in fixtures if value["path"].endswith(".whl")]
                if len(wheel_paths) != 1:
                    raise lite.LiteError("VERIFY_PREVIEW_ARTIFACT_REQUIRED")
                scanned = scan_artifact(wheel_paths[0])
                import_env = dict(env)
                import_env["PYTHONPATH"] = guard + os.pathsep + wheel_paths[0]
                imported = subprocess.run(
                    _sandboxed_command([
                        sys.executable, "-c",
                        "import agent_workboard;print(agent_workboard.__version__)",
                    ], deny_paths, os.path.join(temporary, "sandbox.sb")),
                    cwd=temporary, env=import_env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                result = "PASS" if imported.returncode == 0 else "FAIL"
                details = {"artifactSha256": scanned["sha256"],
                           "memberCount": scanned["memberCount"],
                           "importExitCode": imported.returncode}
            checks.append({
                "checkId": entry["checkId"],
                "phase": phase, "result": result,
                "resultDigest": _sha(_json({
                    "details": details, "fixtures": fixtures,
                })),
                "covers": ["HC-1", "HC-5", "VP-1", "VP-2"],
            })
            if result != "PASS":
                break
    after = _path_snapshot(deny_paths)
    if before != after:
        raise lite.LiteError("VERIFY_LIVE_DB_MUTATION_DETECTED")
    if any(check["result"] != "PASS" for check in checks):
        raise lite.LiteError("VERIFY_REGISTERED_CHECK_FAILED")
    return checks


def _latest_receipt(connection, work_item_id):
    rows = connection.execute(
        "SELECT * FROM events WHERE work_item_id=? AND event_type IN "
        "('VERIFY_RECEIPT_RECORDED','VERIFY_RECEIPT_EXTENDED') "
        "ORDER BY event_id DESC", (work_item_id,),
    ).fetchall()
    for row in rows:
        receipt = _event_payload(row).get("receipt")
        if isinstance(receipt, dict) and receipt.get("protocolVersion") == RECEIPT_PROTOCOL:
            return dict(row), receipt
    return None, None


def _receipt_identity(receipt):
    result = {key: receipt.get(key) for key in (
        "receiptId", "coreFingerprint",
    )}
    result["receiptFingerprint"] = receipt.get("projection", {}).get(
        "receiptFingerprint")
    return result


def _receipt_contract(management, phase, policy, checks):
    """Bind policy work and current acceptance to exact PASS check ids."""
    plan = _registered_check_plan(phase, policy)
    if (len(checks) != len(plan) or
            any(not isinstance(check, dict) or check.get("checkId") != entry["checkId"] or
                check.get("phase") != phase or check.get("result") != "PASS"
                for check, entry in zip(checks, plan))):
        raise lite.LiteError("VERIFY_RECEIPT_CHECK_PLAN_MISMATCH")
    basis = _management_basis(management)
    acceptance_ids = [entry["id"] for entry in basis["acceptance"]]
    canonical_checks = [dict(check) for check in checks]
    canonical_checks[0]["covers"] = sorted(set(
        canonical_checks[0].get("covers", []) + acceptance_ids
    ))
    coverage = {}
    for name in sorted(DEFAULT_POLICY):
        coverage[name] = sorted(set(
            entry["checkId"] for entry in plan if name in entry["dimensions"]
        ))
        if not coverage[name]:
            raise lite.LiteError("VERIFY_RECEIPT_POLICY_COVERAGE_MISSING:" + name)
    primary = canonical_checks[0]["checkId"]
    trace = [{"acceptanceId": entry["id"],
              "criterionDigest": entry["criterionDigest"],
              "checkIds": [primary]}
             for entry in basis["acceptance"]]
    return canonical_checks, basis, coverage, trace


def validate_current_receipt(connection, project_root, work_item_id,
                             receipt_id=None, require_final=True):
    unused_row, receipt = _latest_receipt(connection, work_item_id)
    if receipt is None:
        raise lite.LiteError("RUN_VERIFY_FOR_CURRENT_CANDIDATE")
    if receipt_id is not None and receipt.get("receiptId") != receipt_id:
        raise lite.LiteError("VERIFY_RECEIPT_ID_MISMATCH")
    candidate = receipt.get("candidate", {})
    if candidate.get("kind") not in ("ORDINARY", "MANAGED_RELEASE"):
        raise lite.LiteError("VERIFY_CANDIDATE_KIND_INVALID")
    current = _candidate_identity(
        connection, project_root, work_item_id, candidate.get("source"),
        candidate.get("candidateId") if candidate.get("kind") ==
        "MANAGED_RELEASE" else None, allow_finalized=True,
    )
    if current != candidate:
        raise lite.LiteError("VERIFY_CANDIDATE_DRIFT")
    unused_root, unused_relative, source_absolute = _regular_source(
        project_root, candidate.get("source")
    )
    management = _management(connection, work_item_id)
    current_classifier = classify(
        management, current, _changed_paths(source_absolute)
    )
    classifier = receipt.get("classifier", {})
    if current_classifier != classifier:
        raise lite.LiteError("VERIFY_CLASSIFIER_DRIFT")
    policy = receipt.get("policy", {}).get("effective", {})
    decision = policy_decision(current_classifier, policy)
    if decision["effective"] != policy:
        raise lite.LiteError("VERIFY_RECEIPT_POLICY_MISMATCH")
    projection = receipt.get("projection", {})
    if projection.get("result") != "PASS":
        raise lite.LiteError("VERIFY_RECEIPT_NOT_PASS")
    core = receipt.get("core", {})
    checks = core.get("checks", [])
    if require_final and not any(check.get("phase") == "FINAL" and
                                 check.get("result") == "PASS" for check in checks):
        raise lite.LiteError("VERIFY_FINAL_PHASE_REQUIRED")
    phase = "FINAL" if any(check.get("phase") == "FINAL" for check in checks) else "REVISION"
    canonical_checks, management_basis, policy_coverage, acceptance_trace = \
        _receipt_contract(management, phase, policy, checks)
    if canonical_checks != checks:
        raise lite.LiteError("VERIFY_RECEIPT_ACCEPTANCE_COVERAGE_MISMATCH")
    if (core.get("managementBasis") != management_basis or
            core.get("policyCoverage") != policy_coverage or
            core.get("acceptanceTrace") != acceptance_trace):
        raise lite.LiteError("VERIFY_RECEIPT_MANAGEMENT_COVERAGE_DRIFT")
    expected_core = _sha(_json({
        "workItemId": work_item_id, "candidate": candidate,
        "classifier": classifier, "policy": receipt.get("policy"),
        "checks": checks, "managementBasis": management_basis,
        "policyCoverage": policy_coverage, "acceptanceTrace": acceptance_trace,
        "addressedFindingIds": core.get("addressedFindingIds", []),
        "knownNonBlockingIssueIds": core.get("knownNonBlockingIssueIds", []),
    }))
    if expected_core != receipt.get("coreFingerprint"):
        raise lite.LiteError("VERIFY_RECEIPT_CORE_MISMATCH")
    expected_projection = _sha(_json({
        "coreFingerprint": expected_core,
        "observations": projection.get("observations", []),
        "result": projection.get("result"),
    }))
    if expected_projection != projection.get("receiptFingerprint"):
        raise lite.LiteError("VERIFY_RECEIPT_PROJECTION_MISMATCH")
    return receipt


def status(database, project_root, work_item_id, source, candidate_id=None):
    unused_root, unused_relative, absolute = _regular_source(project_root, source)
    connection = lite.open_database(database)
    try:
        identity = _candidate_identity(
            connection, project_root, work_item_id, source, candidate_id
        )
        management = _management(connection, work_item_id)
        classifier = classify(management, identity, _changed_paths(absolute))
        unused_row, receipt = _latest_receipt(connection, work_item_id)
        valid = None
        if receipt is not None:
            try:
                valid = validate_current_receipt(
                    connection, project_root, work_item_id,
                    receipt.get("receiptId"), require_final=False,
                )
            except lite.LiteError:
                valid = None
    finally:
        connection.close()
    return {
        "protocolVersion": STATUS_PROTOCOL, "operation": "STATUS",
        "status": "PASS", "workItemId": work_item_id,
        "candidate": identity, "classifier": classifier,
        "currentReceipt": valid,
        "nextStep": ({"action": "NONE", "arguments": {}}
                     if valid else {"action": "RUN_VERIFY_FOR_CURRENT_CANDIDATE",
                                    "arguments": {"candidateFingerprint":
                                                  identity["candidateFingerprint"]}}),
    }


def _override_grant(connection, request_id):
    row = connection.execute("SELECT * FROM events WHERE request_id=?", (request_id,)).fetchone()
    if row is None or row["event_type"] != "VERIFY_POLICY_OVERRIDE_GRANTED":
        raise lite.LiteError("VERIFY_OVERRIDE_NOT_FOUND")
    return dict(row), _event_payload(row)


def grant_override(database, project_root, work_item_id, human_id,
                   candidate_fingerprint, scope, reason, expires_at,
                   request_id, requested_policy):
    if scope not in ("REVISION", "FINAL", "POST_PUBLICATION"):
        raise lite.LiteError("verify override scope is invalid")
    if not human_id or not reason or not request_id:
        raise lite.LiteError("verify override requires HUMAN, reason and request-id")
    try:
        expiry = datetime.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise lite.LiteError("verify override expires-at is invalid")
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    if expiry.tzinfo is None or expiry <= now_dt:
        raise lite.LiteError("verify override must expire in the future")
    connection = lite.open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = connection.execute("SELECT * FROM events WHERE request_id=?", (request_id,)).fetchone()
        request_fingerprint = _sha(_json({
            "workItemId": work_item_id, "human": human_id,
            "candidateFingerprint": candidate_fingerprint, "scope": scope,
            "reason": reason, "expiresAt": expires_at,
            "requestedPolicy": requested_policy,
        }))
        if replay is not None:
            payload = _event_payload(replay)
            if (replay["event_type"] != "VERIFY_POLICY_OVERRIDE_GRANTED" or
                    payload.get("requestFingerprint") != request_fingerprint):
                raise lite.LiteError("REQUEST_REPLAY_CONFLICT")
            connection.rollback()
            return payload["resultReceipt"]
        prior = connection.execute(
            "SELECT * FROM events WHERE work_item_id=? AND event_type IN "
            "('VERIFY_OBSERVATION_RECORDED','VERIFY_RECEIPT_RECORDED') "
            "ORDER BY event_id DESC", (work_item_id,),
        ).fetchall()
        basis = None
        for row in prior:
            payload = _event_payload(row)
            value = payload.get("observation") or payload.get("receipt")
            if isinstance(value, dict) and value.get("candidate", {}).get(
                    "candidateFingerprint") == candidate_fingerprint:
                basis = value; break
        if basis is None:
            raise lite.LiteError("VERIFY_OVERRIDE_REQUIRES_CANDIDATE_CLASSIFICATION")
        basis_candidate = basis["candidate"]
        current_candidate = _candidate_identity(
            connection, project_root, work_item_id, basis_candidate.get("source"),
            basis_candidate.get("candidateId") if basis_candidate.get("kind") ==
            "MANAGED_RELEASE" else None, allow_finalized=True,
        )
        unused_root, unused_relative, source_absolute = _regular_source(
            project_root, basis_candidate.get("source")
        )
        current_management = _management(connection, work_item_id)
        classifier = classify(
            current_management, current_candidate, _changed_paths(source_absolute)
        )
        recorded_basis = (basis.get("managementBasis") or
                          basis.get("core", {}).get("managementBasis"))
        if (current_candidate != basis_candidate or
                classifier != basis.get("classifier") or
                _management_basis(current_management) != recorded_basis):
            raise lite.LiteError("VERIFY_CLASSIFIER_DRIFT")
        decision = policy_decision(classifier, requested_policy)
        if not decision["overrideRequired"]:
            raise lite.LiteError("VERIFY_OVERRIDE_NOT_REQUIRED")
        snapshot = lite._kernel_assert(connection, work_item_id, project_root, phase="pre")
        now = lite._now()
        result = {
            "protocolVersion": OVERRIDE_PROTOCOL, "operation": "OVERRIDE",
            "status": "OK", "workItemId": work_item_id,
            "requestId": request_id, "candidateFingerprint": candidate_fingerprint,
            "scope": scope, "oneShot": True, "expiresAt": expires_at,
            "effectivePolicy": decision["effective"],
        }
        payload = {
            "protocolVersion": OVERRIDE_PROTOCOL, "workItemId": work_item_id,
            "candidateFingerprint": candidate_fingerprint,
            "classifierVersion": classifier["version"],
            "riskClass": classifier["riskClass"], "hardFloor": classifier["hardFloor"],
            "recommendedPolicy": classifier["recommendedPolicy"],
            "requestedPolicy": requested_policy, "effectivePolicy": decision["effective"],
            "human": human_id, "reason": reason, "scope": scope,
            "createdAt": now, "expiresAt": expires_at, "oneShot": True,
            "requestFingerprint": request_fingerprint, "resultReceipt": result,
        }
        intent = {
            "operation": "VERIFY_POLICY_OVERRIDE", "workItemId": work_item_id,
            "requestId": request_id, "actorKind": "HUMAN", "actorId": human_id,
            "now": now, "events": ({"requestId": request_id,
                                      "eventType": "VERIFY_POLICY_OVERRIDE_GRANTED",
                                      "payload": payload},),
        }
        lite._kernel_apply(connection, work_item_id, snapshot,
                           workflow_kernel.plan_verify_events(snapshot, intent),
                           project_root, evaluation_time=now)
        connection.commit()
        return result
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


def extend_current_receipt(connection, project_root, work_item_id, phase,
                           request_id, checks, generated_at):
    """Return one same-core receipt projection for an already validated phase."""
    if phase != "POST_PUBLICATION" or not request_id or not checks or any(
            check.get("phase") != phase or check.get("result") != "PASS"
            for check in checks):
        raise lite.LiteError("VERIFY_RECEIPT_EXTENSION_INVALID")
    unused_row, old = _latest_receipt(connection, work_item_id)
    if old is None:
        raise lite.LiteError("VERIFY_POST_REQUIRES_CURRENT_FINAL_RECEIPT")
    validate_current_receipt(
        connection, project_root, work_item_id, old.get("receiptId"),
        require_final=True,
    )
    receipt = json.loads(_json(old))
    observations = list(receipt["projection"].get("observations", []))
    observations.append({
        "phase": phase, "checks": checks, "requestId": request_id,
        "generatedAt": generated_at,
    })
    receipt["projection"] = {
        "observations": observations, "result": "PASS",
        "receiptFingerprint": _sha(_json({
            "coreFingerprint": receipt["coreFingerprint"],
            "observations": observations, "result": "PASS",
        })),
    }
    return receipt


def run(database, project_root, repository_key, work_item_id, source, agent_id,
        phase, request_id, orchestrator_id=None, orchestrator_generation=None,
        requested_policy=None, override_request_id=None,
        addressed_finding_ids=None, known_issue_ids=None, candidate_id=None):
    if phase not in ("REVISION", "FINAL", "POST_PUBLICATION"):
        raise lite.LiteError("verify phase is invalid")
    if phase == "POST_PUBLICATION":
        raise lite.LiteError(
            "POST_PUBLICATION_REQUIRES_MANAGED_PUBLICATION_RUNTIME"
        )
    if not request_id or not agent_id:
        raise lite.LiteError("verify run requires agent and request-id")
    addressed_finding_ids = sorted(set(addressed_finding_ids or []))
    known_issue_ids = sorted(set(known_issue_ids or []))
    unused_root, unused_relative, source_absolute = _regular_source(project_root, source)
    connection = lite.open_database(database)
    try:
        identity = _candidate_identity(
            connection, project_root, work_item_id, source, candidate_id
        )
        management = _management(connection, work_item_id)
        management_basis = _management_basis(management)
        classifier = classify(management, identity, _changed_paths(source_absolute))
        if candidate_id is not None:
            from . import candidate as candidate_module
            candidate_module.assert_self_host_operation(
                connection, project_root, work_item_id,
                "VERIFY_FINAL_RECORD", candidate_id,
            )
            built = candidate_module.release_submission_candidate(
                connection, work_item_id
            )
            unused_manifest, unused_assets = candidate_module._verify_current_build(
                project_root, work_item_id, built
            )
        decision = policy_decision(classifier, requested_policy)
        request_fingerprint = _sha(_json({
            "workItemId": work_item_id, "source": source,
            "candidate": identity, "agent": agent_id, "phase": phase,
            "managementBasis": management_basis, "classifier": classifier,
            "policy": requested_policy or {}, "override": override_request_id,
            "addressedFindingIds": addressed_finding_ids,
            "knownIssueIds": known_issue_ids,
        }))
        replay = connection.execute("SELECT * FROM events WHERE request_id=?", (request_id,)).fetchone()
        if replay is not None:
            payload = _event_payload(replay)
            if (replay["event_type"] not in (
                    "VERIFY_OBSERVATION_RECORDED", "VERIFY_RECEIPT_RECORDED",
                    "VERIFY_RECEIPT_EXTENDED") or
                    payload.get("requestFingerprint") != request_fingerprint):
                raise lite.LiteError("REQUEST_REPLAY_CONFLICT")
            return payload["resultReceipt"]
    finally:
        connection.close()
    if decision["overrideRequired"] and not override_request_id:
        raise lite.LiteError("VERIFY_POLICY_DOWNGRADE_REQUIRES_HUMAN_OVERRIDE")
    checks = _run_registered_checks(
        project_root, database, source_absolute, phase, decision["effective"]
    )
    connection = lite.open_database(database)
    try:
        current_identity = _candidate_identity(
            connection, project_root, work_item_id, source, candidate_id
        )
        current_management = _management(connection, work_item_id)
        current_classifier = classify(
            current_management, current_identity, _changed_paths(source_absolute)
        )
        if current_identity != identity:
            raise lite.LiteError("VERIFY_CANDIDATE_DRIFT")
        if (current_classifier != classifier or
                _management_basis(current_management) != management_basis):
            raise lite.LiteError("VERIFY_CLASSIFIER_DRIFT")
    finally:
        connection.close()
    connection = lite.open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = connection.execute("SELECT * FROM events WHERE request_id=?", (request_id,)).fetchone()
        if replay is not None:
            payload = _event_payload(replay)
            if payload.get("requestFingerprint") != request_fingerprint:
                raise lite.LiteError("REQUEST_REPLAY_CONFLICT")
            connection.rollback(); return payload["resultReceipt"]
        from . import candidate as candidate_module
        candidate_module._runtime_guard(
            connection, work_item_id, agent_id, repository_key,
            orchestrator_id, orchestrator_generation,
        )
        current_identity = _candidate_identity(
            connection, project_root, work_item_id, source, candidate_id
        )
        current_management = _management(connection, work_item_id)
        current_classifier = classify(
            current_management, current_identity, _changed_paths(source_absolute)
        )
        if current_identity != identity:
            raise lite.LiteError("VERIFY_CANDIDATE_DRIFT")
        if (current_classifier != classifier or
                _management_basis(current_management) != management_basis):
            raise lite.LiteError("VERIFY_CLASSIFIER_DRIFT")
        snapshot = lite._kernel_assert(connection, work_item_id, project_root, phase="pre")
        now = lite._now()
        consumed_event = None
        if decision["overrideRequired"]:
            unused_grant_row, grant = _override_grant(connection, override_request_id)
            if (grant.get("candidateFingerprint") != identity["candidateFingerprint"] or
                    grant.get("scope") != phase or
                    grant.get("effectivePolicy") != decision["effective"] or
                    grant.get("hardFloor") != classifier["hardFloor"] or
                    grant.get("oneShot") is not True or
                    grant.get("expiresAt") <= now):
                raise lite.LiteError("VERIFY_OVERRIDE_BINDING_MISMATCH")
            used = connection.execute(
                "SELECT 1 FROM events WHERE work_item_id=? AND event_type="
                "'VERIFY_OVERRIDE_CONSUMED' AND payload_json LIKE ?",
                (work_item_id, '%"overrideRequestId":"' + override_request_id + '"%'),
            ).fetchone()
            if used:
                raise lite.LiteError("VERIFY_OVERRIDE_ALREADY_CONSUMED")
            consumed_event = {
                "requestId": request_id + "-override-consumed",
                "eventType": "VERIFY_OVERRIDE_CONSUMED",
                "payload": {"overrideRequestId": override_request_id,
                            "verifyRequestId": request_id,
                            "candidateFingerprint": identity["candidateFingerprint"],
                            "scope": phase},
            }
        policy = {"requested": decision["requested"],
                  "effective": decision["effective"],
                  "overrideRequestId": override_request_id}
        checks, management_basis, policy_coverage, acceptance_trace = \
            _receipt_contract(current_management, phase, decision["effective"], checks)
        if phase == "REVISION":
            observation = {
                "protocolVersion": "AWB-VERIFY-OBSERVATION-v1",
                "workItemId": work_item_id, "candidate": identity,
                "classifier": classifier, "policy": policy,
                "checks": checks, "managementBasis": management_basis,
                "policyCoverage": policy_coverage,
                "acceptanceTrace": acceptance_trace,
                "addressedFindingIds": addressed_finding_ids,
                "knownNonBlockingIssueIds": known_issue_ids,
                "generatedAt": now, "result": "PASS",
            }
            event_type, operation = "VERIFY_OBSERVATION_RECORDED", "VERIFY_OBSERVATION"
            result = {"protocolVersion": "AWB-VERIFY-OBSERVATION-v1",
                      "operation": "RUN", "phase": phase, "status": "PASS",
                      "workItemId": work_item_id, "observation": observation,
                      "nextStep": {"action": "RUN_FINAL_VERIFY", "arguments": {
                          "candidateFingerprint": identity["candidateFingerprint"]}}}
            payload_value = {"observation": observation}
        else:
            observations = [{"phase": phase, "checks": checks,
                             "requestId": request_id, "generatedAt": now}]
            core_data = {
                "workItemId": work_item_id, "candidate": identity,
                "classifier": classifier, "policy": policy, "checks": checks,
                "managementBasis": management_basis,
                "policyCoverage": policy_coverage,
                "acceptanceTrace": acceptance_trace,
                "addressedFindingIds": addressed_finding_ids,
                "knownNonBlockingIssueIds": known_issue_ids,
            }
            core_fingerprint = _sha(_json(core_data))
            receipt_id = _sha(_json({"workItemId": work_item_id,
                                     "candidate": identity,
                                     "coreFingerprint": core_fingerprint}))
            projection = {"observations": observations, "result": "PASS"}
            projection["receiptFingerprint"] = _sha(_json({
                "coreFingerprint": core_fingerprint,
                "observations": observations, "result": "PASS",
            }))
            receipt = {
                "protocolVersion": RECEIPT_PROTOCOL, "receiptId": receipt_id,
                "workItemId": work_item_id, "candidate": identity,
                "classifier": classifier, "policy": policy,
                "core": {"checks": checks, "managementBasis": management_basis,
                         "policyCoverage": policy_coverage,
                         "acceptanceTrace": acceptance_trace,
                         "addressedFindingIds": addressed_finding_ids,
                         "knownNonBlockingIssueIds": known_issue_ids,
                         "generatedAt": now, "validUntil": None},
                "coreFingerprint": core_fingerprint, "projection": projection,
            }
            event_type = "VERIFY_RECEIPT_RECORDED"
            operation = "VERIFY_RECEIPT"
            result = {"protocolVersion": RECEIPT_PROTOCOL,
                      "operation": "RUN", "phase": phase, "status": "PASS",
                      "workItemId": work_item_id, "receipt": receipt,
                      "nextStep": {"action": "SUBMIT_IMPLEMENTATION", "arguments": {
                                       "receiptId": receipt_id}}}
            payload_value = {"receipt": receipt}
        payload = dict(payload_value)
        payload.update({"requestFingerprint": request_fingerprint,
                        "resultReceipt": result})
        events = [{"requestId": request_id, "eventType": event_type,
                   "payload": payload}]
        if consumed_event:
            events.append(consumed_event)
        intent = {
            "operation": operation, "workItemId": work_item_id,
            "requestId": request_id, "actorKind": "AGENT", "actorId": agent_id,
            "now": now, "events": tuple(events),
        }
        lite._kernel_apply(connection, work_item_id, snapshot,
                           workflow_kernel.plan_verify_events(snapshot, intent),
                           project_root, evaluation_time=now)
        connection.commit()
        return result
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


def _safe_member(name):
    if (not name or "\\" in name or name.startswith("/") or
            any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))):
        raise lite.LiteError("artifact contains an unsafe member path")


def _scan_raw(name, raw):
    packaged_test_source = "/tests/" in "/" + name and name.endswith(".py")
    if (any(marker in raw for marker in _FORBIDDEN_CONTENT) or
            any(pattern.search(raw) for pattern in _FORBIDDEN_PATTERNS) or
            not packaged_test_source and any(
                pattern.search(raw) for pattern in _FORBIDDEN_LOCAL_PATH_PATTERNS
            )):
        raise lite.LiteError("forbidden content in artifact member: " + name)
    return {"path": name, "sha256": _sha(raw), "size": len(raw)}


def scan_artifact(path):
    path = os.path.realpath(os.path.abspath(path))
    if not os.path.isfile(path) or os.path.islink(path):
        raise lite.LiteError("artifact must be a regular non-symlink file")
    members = []
    if path.endswith(".whl"):
        with zipfile.ZipFile(path) as archive:
            seen = set()
            for info in archive.infolist():
                _safe_member(info.filename)
                if info.filename in seen:
                    raise lite.LiteError("artifact contains duplicate members")
                seen.add(info.filename)
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.is_dir():
                    continue
                if stat.S_IFMT(mode) and not stat.S_ISREG(mode):
                    raise lite.LiteError("artifact contains a non-regular member")
                members.append(_scan_raw(info.filename, archive.read(info)))
    elif path.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as archive:
            seen = set()
            for info in archive.getmembers():
                _safe_member(info.name)
                if info.name in seen:
                    raise lite.LiteError("artifact contains duplicate members")
                seen.add(info.name)
                if info.isdir():
                    continue
                if not info.isfile():
                    raise lite.LiteError("artifact contains a non-regular member")
                handle = archive.extractfile(info)
                if handle is None:
                    raise lite.LiteError("artifact member cannot be read")
                members.append(_scan_raw(info.name, handle.read()))
    else:
        raise lite.LiteError("artifact type must be wheel or gzipped sdist")
    if not members:
        raise lite.LiteError("artifact contains no regular members")
    return {"artifact": os.path.basename(path), "sha256": _file_sha(path),
            "memberCount": len(members),
            "members": sorted(members, key=lambda row: row["path"]),
            "privacy": "ALLOWLIST_ONLY"}
