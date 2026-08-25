"""Managed release candidates, pinned builds and publication audit.

This module deliberately stores lifecycle state in the existing immutable event
stream.  It does not add schema and it never performs remote or delete actions.
"""

import datetime
import ast
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import zipfile

from . import lite
from . import verify
from . import workflow as workflow_kernel


CANDIDATE_PROTOCOL = "AWB-MANAGED-CANDIDATE-v1"
BUILD_PROTOCOL = "AWB-PINNED-PYTHON-BUILD-v1"
AUTHORIZATION_PROTOCOL = "AWB-PUBLICATION-AUTHORIZATION-v1"
POSTFLIGHT_PROTOCOL = "AWB-PUBLICATION-POSTFLIGHT-v1"
RETRY_PROTOCOL = "AWB-PUBLICATION-RETRY-v1"
RECEIPT_PROTOCOL = "AWB-MUTATION-RECEIPT-v1"
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_GIT_HEX = re.compile(r"^[0-9a-f]{40}$")
_ALLOWED_ACTIONS = sorted((
    "CREATE_IMMUTABLE_PRERELEASE", "PUSH_EXACT_NON_FORCE_REFS",
    "UPLOAD_EXACT_THREE_ASSETS",
))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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


def _load_json(path, protocol=None):
    if not os.path.isfile(path) or os.path.islink(path):
        raise lite.LiteError("input must be a regular non-symlink file")
    with open(path, "r", encoding="utf-8") as handle:
        try:
            value = json.load(handle)
        except ValueError as exc:
            raise lite.LiteError("input JSON is invalid: {0}".format(exc))
    if not isinstance(value, dict):
        raise lite.LiteError("input JSON must contain an object")
    if protocol and value.get("protocolVersion") != protocol:
        raise lite.LiteError("input protocolVersion is invalid")
    return value


def _slug(value, label):
    if not isinstance(value, str) or not _SLUG.match(value):
        raise lite.LiteError("{0} is invalid".format(label))
    return value


def _project(project_root):
    root = os.path.realpath(os.path.abspath(project_root))
    if not os.path.isdir(root) or os.path.islink(project_root):
        raise lite.LiteError("project root is invalid")
    return root


def _project_file(project_root, path, label):
    root = _project(project_root)
    if not isinstance(path, str) or not path or os.path.isabs(path):
        raise lite.LiteError("{0} must be project-relative".format(label))
    absolute = os.path.realpath(os.path.join(root, path))
    if os.path.commonpath((root, absolute)) != root or not os.path.isfile(absolute) or os.path.islink(absolute):
        raise lite.LiteError("{0} must be a project regular non-symlink file".format(label))
    return absolute


def _managed(project_root, work_item_id):
    root = _project(project_root)
    _slug(work_item_id, "workItemId")
    managed = os.path.join(root, ".awb", "release-candidates", work_item_id, "managed")
    parent = root
    for part in os.path.relpath(managed, root).split(os.sep):
        parent = os.path.join(parent, part)
        if os.path.lexists(parent) and os.path.islink(parent):
            raise lite.LiteError("managed candidate path contains a symlink")
    return root, managed


def _relative(root, path):
    real = os.path.realpath(path)
    if os.path.commonpath((root, real)) != root:
        raise lite.LiteError("managed candidate path escapes project")
    return os.path.relpath(real, root).replace(os.sep, "/")


def _git(repository, *args):
    try:
        return subprocess.check_output(
            ["git"] + list(args), cwd=repository, stderr=subprocess.STDOUT
        ).decode("utf-8").strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        output = getattr(exc, "output", b"")
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        raise lite.LiteError("git verification failed: {0}".format(output.strip() or exc))


def _tree_fingerprint(path):
    digest = hashlib.sha256()
    if not os.path.isdir(path) or os.path.islink(path):
        raise lite.LiteError("candidate tree is missing or unsafe")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)

    def same_member(left, right):
        return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
            right.st_dev, right.st_ino, stat.S_IFMT(right.st_mode)
        )

    def walk(directory_fd, prefix):
        with os.scandir(directory_fd) as iterator:
            entries = sorted(list(iterator), key=lambda entry: entry.name)
        for entry in entries:
            relative = entry.name if not prefix else prefix + "/" + entry.name
            before = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise lite.LiteError("candidate contains a symlink member")
            if stat.S_ISDIR(before.st_mode):
                child_fd = os.open(entry.name, directory_flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if not same_member(before, opened):
                        raise lite.LiteError("candidate directory changed during scan")
                    if entry.name != ".git":
                        walk(child_fd, relative)
                    if not same_member(opened, os.fstat(child_fd)):
                        raise lite.LiteError("candidate directory changed during scan")
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise lite.LiteError("candidate contains a non-regular or hardlinked member")
            file_fd = os.open(entry.name, file_flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(file_fd)
                if (not same_member(before, opened) or not stat.S_ISREG(opened.st_mode) or
                        opened.st_nlink != 1):
                    raise lite.LiteError("candidate file changed during scan")
                digest.update(relative.encode("utf-8") + b"\0")
                digest.update(str(opened.st_mode & 0o777).encode("ascii") + b"\0")
                with os.fdopen(file_fd, "rb") as handle:
                    file_fd = None
                    for block in iter(lambda: handle.read(65536), b""):
                        digest.update(block)
            finally:
                if file_fd is not None:
                    os.close(file_fd)

    before_root = os.lstat(path)
    if not stat.S_ISDIR(before_root.st_mode) or stat.S_ISLNK(before_root.st_mode):
        raise lite.LiteError("candidate tree is missing or unsafe")
    root_fd = os.open(path, directory_flags)
    try:
        opened_root = os.fstat(root_fd)
        if not same_member(before_root, opened_root):
            raise lite.LiteError("candidate root changed during scan")
        walk(root_fd, "")
        if not same_member(opened_root, os.fstat(root_fd)):
            raise lite.LiteError("candidate root changed during scan")
    finally:
        os.close(root_fd)
    return digest.hexdigest()


def _write_json(path, value):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    if os.path.lexists(path):
        raise lite.LiteError("refusing to overwrite managed evidence")
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(_json(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _replace_json(path, value, suffix):
    temporary = path + "." + suffix
    if os.path.lexists(temporary):
        raise lite.LiteError("managed manifest staging path already exists")
    with open(temporary, "x", encoding="utf-8") as handle:
        handle.write(_json(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _events(connection, work_item_id):
    result = []
    for row in connection.execute(
            "SELECT * FROM events WHERE work_item_id=? ORDER BY event_id", (work_item_id,)):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        result.append((dict(row), payload))
    return result


def _latest_event(connection, work_item_id, kinds):
    if isinstance(kinds, str):
        kinds = (kinds,)
    placeholders = ",".join("?" for unused in kinds)
    row = connection.execute(
        "SELECT * FROM events WHERE work_item_id=? AND event_type IN ({0}) "
        "ORDER BY event_id DESC LIMIT 1".format(placeholders),
        (work_item_id,) + tuple(kinds),
    ).fetchone()
    if row is None:
        return None, None
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        payload = {}
    return dict(row), payload


def _runtime_guard(connection, work_item_id, owner, repository_key,
                   orchestrator_id=None, orchestrator_generation=None):
    item = lite._item(connection, work_item_id)
    claim = connection.execute(
        "SELECT * FROM claims WHERE work_item_id=? AND agent_id=? AND role='IMPLEMENTER' "
        "AND status='ACTIVE' AND expires_at>?", (work_item_id, owner, lite._now())
    ).fetchone()
    if claim is None:
        raise lite.LiteError("candidate mutation requires the live Implementer claim")
    writer = connection.execute(
        "SELECT * FROM repository_locks WHERE repository_key=? AND work_item_id=? "
        "AND agent_id=? AND status='ACTIVE' AND expires_at>?",
        (repository_key, work_item_id, owner, lite._now()),
    ).fetchone()
    if writer is None:
        raise lite.LiteError("candidate mutation requires the live repository writer")
    lease_history = connection.execute(
        "SELECT count(*) FROM orchestrator_leases WHERE work_item_id=?", (work_item_id,)
    ).fetchone()[0]
    if lease_history:
        if orchestrator_id is None or orchestrator_generation is None:
            raise lite.LiteError("candidate mutation requires the Orchestrator fence")
        lease = connection.execute(
            "SELECT 1 FROM orchestrator_leases WHERE work_item_id=? AND orchestrator_id=? "
            "AND generation=? AND status='ACTIVE' AND expires_at>?",
            (work_item_id, orchestrator_id, orchestrator_generation, lite._now()),
        ).fetchone()
        if lease is None:
            raise lite.LiteError("candidate Orchestrator fence is stale")
    return item, dict(claim), dict(writer)


def _candidate_events(connection, work_item_id, candidate_id):
    rows = []
    for row, payload in _events(connection, work_item_id):
        if payload.get("candidateId") == candidate_id and row["event_type"].startswith("CANDIDATE_"):
            rows.append((row, payload))
    return rows


def _candidate_head(connection, work_item_id, candidate_id):
    rows = _candidate_events(connection, work_item_id, candidate_id)
    return rows[-1] if rows else (None, None)


def _candidate_manifest(project_root, work_item_id, candidate_id, quarantined=None):
    root, managed = _managed(project_root, work_item_id)
    _slug(candidate_id, "candidateId")
    if quarantined:
        path = os.path.join(managed, "quarantine", candidate_id, quarantined, "candidate.json")
    else:
        path = os.path.join(managed, "active", candidate_id, "candidate.json")
    return root, managed, path


def _receipt(operation, status, work_item_id, event_id=None, row_version=None,
             reason_code=None, next_step=None, extra=None):
    value = {
        "protocolVersion": RECEIPT_PROTOCOL, "operation": operation,
        "status": status, "reasonCode": reason_code, "workItemId": work_item_id,
        "eventId": event_id, "rowVersion": row_version,
        "nextStep": next_step or {"action": "NONE", "arguments": {}},
    }
    if extra:
        value.update(extra)
    return value


def _candidate_materialized(connection, event_type, request_id):
    """Fault-injection seam after complete candidate plan and before commit."""
    return None


def _resolve_commit_outcome(database, work_item_id, request_id, event_type,
                            expected_fields, candidate_id, postflight=False):
    """Resolve an unknown SQLite commit result without changing runtime state."""
    connection = lite.open_database(database)
    try:
        row = connection.execute(
            "SELECT * FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            raise lite.LiteError("COMMIT_OUTCOME_AMBIGUOUS")
        if (row["work_item_id"] != work_item_id or row["event_type"] != event_type or
                any(payload.get(key) != value for key, value in expected_fields.items())):
            raise lite.LiteError("COMMIT_OUTCOME_AMBIGUOUS")
        receipt = payload.get("resultReceipt")
        if (not isinstance(receipt, dict) or receipt.get("eventId") != row["event_id"] or
                receipt.get("rowVersion") != payload.get("resultRowVersion") or
                receipt.get("workItemId") != work_item_id or receipt.get("status") != "OK"):
            raise lite.LiteError("COMMIT_OUTCOME_AMBIGUOUS")
        head_row, head_payload = _candidate_head(connection, work_item_id, candidate_id)
        if postflight:
            finalized = connection.execute(
                "SELECT * FROM events WHERE request_id=?",
                (request_id + "-candidate-finalized",),
            ).fetchone()
            item = lite._item(connection, work_item_id)
            if (finalized is None or finalized["event_type"] != "CANDIDATE_FINALIZED" or
                    head_row is None or head_row["event_id"] != finalized["event_id"] or
                    head_payload.get("candidateId") != candidate_id or
                    not (item["state"] == "FINAL_ACCEPTANCE_APPROVED" or
                         item["queue_state"] == "WAITING_HUMAN")):
                raise lite.LiteError("COMMIT_OUTCOME_AMBIGUOUS")
        elif head_row is None or head_row["event_id"] != row["event_id"]:
            raise lite.LiteError("COMMIT_OUTCOME_AMBIGUOUS")
        return receipt
    finally:
        connection.close()


def _commit_journal(journal, prepared, request_id):
    """Advance only this request's exact PREPARED journal to COMMITTED."""
    committed = dict(prepared)
    committed["status"] = "COMMITTED"
    current = _load_json(journal, CANDIDATE_PROTOCOL)
    if current == committed:
        return
    if (current != prepared or current.get("requestId") != request_id or
            current.get("status") != "PREPARED"):
        raise lite.LiteError(
            "COMMIT_OUTCOME_CONFIRMED: journal recovery required"
        )
    try:
        _replace_json(journal, committed,
                      "committed-" + _sha(request_id)[:16])
    except Exception as exc:
        raise lite.LiteError(
            "COMMIT_OUTCOME_CONFIRMED: journal recovery required: {0}".format(exc)
        )


def _mark_journal_recovery(journal, prepared, request_id):
    """Mark only this request's exact PREPARED journal as recoverable."""
    recovery = dict(prepared)
    recovery["status"] = "RECOVERY_REQUIRED"
    current = _load_json(journal, CANDIDATE_PROTOCOL)
    if current == recovery:
        return
    if (current != prepared or current.get("requestId") != request_id or
            current.get("status") != "PREPARED"):
        raise lite.LiteError("BUILD_RECOVERY_REQUIRED: journal ownership drift")
    _replace_json(journal, recovery, "recovery-" + _sha(request_id)[:16])


def _rename_exclusive(source, target):
    """Atomically rename a directory without replacing a raced destination."""
    if os.name == "nt":
        os.rename(source, target)
        return
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(source), os.fsencode(target), 0x00000004)
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p,
                           ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    else:
        raise lite.LiteError("exclusive managed rename is unavailable")
    if result:
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise lite.LiteError("managed destination already exists")
        raise OSError(error, os.strerror(error), target)


def _validate_base(base):
    required = {"packageVersion", "sourceCommit", "sourceTree", "sourceTag", "wheelSha256"}
    if not isinstance(base, dict) or set(base) != required:
        raise lite.LiteError("base identity fields are invalid")
    if not _GIT_HEX.match(base["sourceCommit"]) or not _GIT_HEX.match(base["sourceTree"]):
        raise lite.LiteError("base Git identity is invalid")
    if not _HEX.match(base["wheelSha256"]):
        raise lite.LiteError("base wheel identity is invalid")
    return base


def _validate_target(target):
    required = {"version", "tag", "releaseBranch", "title", "assetNames"}
    if not isinstance(target, dict) or set(target) != required:
        raise lite.LiteError("target identity fields are invalid")
    assets = target.get("assetNames")
    if (not isinstance(assets, list) or len(assets) != 3 or len(set(assets)) != 3 or
            "SHA256SUMS" not in assets or
            sum(name.endswith(".whl") for name in assets) != 1 or
            sum(name.endswith(".tar.gz") for name in assets) != 1):
        raise lite.LiteError("target must declare exactly wheel, sdist and SHA256SUMS")
    return target


def prepare(database, project_root, repository_key, work_item_id, candidate_id,
            source, base_file, target_file, owner, request_id,
            orchestrator_id=None, orchestrator_generation=None):
    _slug(candidate_id, "candidateId")
    _slug(request_id, "requestId")
    base_file = _project_file(project_root, base_file, "base file")
    target_file = _project_file(project_root, target_file, "target file")
    base = _validate_base(_load_json(base_file))
    target = _validate_target(_load_json(target_file))
    source = os.path.realpath(os.path.abspath(source))
    if not os.path.isdir(source) or os.path.islink(source):
        raise lite.LiteError("candidate source must be a regular Git directory")
    if _git(source, "status", "--porcelain"):
        raise lite.LiteError("candidate source must be clean")
    if _git(source, "rev-parse", "HEAD") != base["sourceCommit"]:
        raise lite.LiteError("candidate source commit drift")
    if _git(source, "rev-parse", "HEAD^{tree}") != base["sourceTree"]:
        raise lite.LiteError("candidate source tree drift")
    if _git(source, "cat-file", "-t", base["sourceTag"]) != "tag":
        raise lite.LiteError("base tag must be annotated")
    if _git(source, "rev-parse", base["sourceTag"] + "^{commit}") != base["sourceCommit"]:
        raise lite.LiteError("base tag drift")
    root, managed = _managed(project_root, work_item_id)
    active = os.path.join(managed, "active", candidate_id)
    staging = os.path.join(managed, "staging", candidate_id, request_id)
    journal = os.path.join(managed, "journal", candidate_id, request_id + ".json")
    for sibling in ("active", "staging", "quarantine", "journal"):
        os.makedirs(os.path.join(managed, sibling), exist_ok=True)
    devices = {os.stat(os.path.join(managed, sibling)).st_dev for sibling in
               ("active", "staging", "quarantine", "journal")}
    if len(devices) != 1:
        raise lite.LiteError("managed candidate roots must share one filesystem")
    connection = lite.open_database(database)
    try:
        replay = connection.execute("SELECT * FROM events WHERE request_id=?", (request_id,)).fetchone()
        fingerprint = _sha(_json({"operation": "PREPARE", "workItemId": work_item_id,
                                  "candidateId": candidate_id, "owner": owner,
                                  "base": base, "target": target}))
        if replay is not None:
            payload = json.loads(replay["payload_json"])
            if replay["event_type"] != "CANDIDATE_PREPARED" or payload.get("requestFingerprint") != fingerprint:
                raise lite.LiteError("request_id was already used with different content")
            item = lite._item(connection, work_item_id)
            return _receipt("CANDIDATE_PREPARED", "OK", work_item_id,
                            replay["event_id"], item["row_version"], extra={"candidateId": candidate_id})
        _runtime_guard(connection, work_item_id, owner, repository_key,
                       orchestrator_id, orchestrator_generation)
    finally:
        connection.close()
    if os.path.lexists(active) or os.path.lexists(staging) or os.path.lexists(journal):
        raise lite.LiteError("managed candidate destination already exists")
    os.makedirs(staging)
    clone = os.path.join(staging, "source")
    try:
        subprocess.check_call(["git", "clone", "--no-hardlinks", "--no-checkout", source, clone])
        subprocess.check_call(["git", "checkout", "--detach", base["sourceCommit"]], cwd=clone)
        alternates = os.path.join(clone, ".git", "objects", "info", "alternates")
        if os.path.exists(alternates):
            raise lite.LiteError("candidate clone must not use Git alternates")
        manifest = {
            "protocolVersion": CANDIDATE_PROTOCOL, "workflowKind": "RELEASE",
            "lifecycle": "PREPARED", "candidateId": candidate_id,
            "workItemId": work_item_id, "repository": repository_key,
            "ownerAgentId": owner, "base": base, "target": target,
            "managedRoot": _relative(root, managed),
            "sourcePath": _relative(root, os.path.join(active, "source")),
            "requestId": request_id,
        }
        manifest["candidateFingerprint"] = _sha(_json(manifest))
        _write_json(os.path.join(staging, "candidate.json"), manifest)
    except Exception:
        quarantine = os.path.join(managed, "quarantine", candidate_id, request_id)
        if os.path.isdir(staging) and not os.path.lexists(quarantine):
            failed_hash = _tree_fingerprint(staging)
            _write_json(journal, {
                "protocolVersion": CANDIDATE_PROTOCOL, "operation": "PREPARE",
                "kind": "WHOLE_CANDIDATE", "workItemId": work_item_id,
                "candidateId": candidate_id, "owner": owner,
                "requestId": request_id, "fingerprint": failed_hash,
                "beforePath": _relative(root, staging),
                "afterPath": _relative(root, quarantine),
                "beforeTreeSha256": failed_hash,
                "afterTreeSha256": failed_hash,
                "priorLifecycle": None, "status": "RECOVERY_REQUIRED",
            })
            os.makedirs(os.path.dirname(quarantine), exist_ok=True)
            os.replace(staging, quarantine)
        raise
    tree_hash = _tree_fingerprint(staging)
    journal_value = {
        "protocolVersion": CANDIDATE_PROTOCOL, "operation": "PREPARE",
        "kind": "WHOLE_CANDIDATE", "workItemId": work_item_id,
        "candidateId": candidate_id, "owner": owner, "requestId": request_id,
        "fingerprint": tree_hash, "beforePath": _relative(root, staging),
        "afterPath": _relative(root, active), "beforeTreeSha256": tree_hash,
        "afterTreeSha256": tree_hash, "status": "PREPARED",
    }
    _write_json(journal, journal_value)
    os.replace(staging, active)
    try:
        connection = lite.open_database(database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            item, unused_claim, unused_writer = _runtime_guard(
                connection, work_item_id, owner, repository_key,
                orchestrator_id, orchestrator_generation,
            )
            lite._kernel_assert(connection, work_item_id, project_root,
                                phase="pre")
            if _tree_fingerprint(active) != tree_hash:
                raise lite.LiteError("candidate bytes changed before event commit")
            payload = dict(manifest)
            payload.update({"requestFingerprint": fingerprint, "treeSha256": tree_hash})
            now = lite._now()
            snapshot = lite._kernel_snapshot(connection, work_item_id, project_root,
                                             evaluation_time=now)
            event = connection.execute(
                "SELECT coalesce(max(event_id),0)+1 FROM events"
            ).fetchone()[0]
            result = _receipt("CANDIDATE_PREPARED", "OK", work_item_id, event,
                              item["row_version"] + 1,
                              extra={"candidateId": candidate_id,
                                     "candidateFingerprint": manifest["candidateFingerprint"]})
            payload.update({"resultRowVersion": result["rowVersion"],
                            "resultReceipt": result})
            intent = {"operation": "CANDIDATE_PREPARE", "workItemId": work_item_id,
                      "eventType": "CANDIDATE_PREPARED", "payload": payload,
                      "eventId": event, "actorKind": "AGENT", "actorId": owner,
                      "requestId": request_id, "now": now, "receipt": result}
            plan = workflow_kernel.plan_audit_touch(snapshot, intent)
            lite._kernel_apply(connection, work_item_id, snapshot, plan, project_root,
                               evaluation_time=now)
            _candidate_materialized(connection, "CANDIDATE_PREPARED", request_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    except Exception:
        if os.path.isdir(active) and not os.path.lexists(staging) and _tree_fingerprint(active) == tree_hash:
            os.makedirs(os.path.dirname(staging), exist_ok=True)
            os.replace(active, staging)
        raise
    journal_value["status"] = "COMMITTED"
    _replace_json(journal, journal_value,
                  "committed-" + _sha(request_id)[:16])
    return result


def status(database, project_root, work_item_id, candidate_id):
    root, managed, manifest_path = _candidate_manifest(project_root, work_item_id, candidate_id)
    connection = lite.open_database(database)
    try:
        row, payload = _candidate_head(connection, work_item_id, candidate_id)
        item = lite._item(connection, work_item_id)
    finally:
        connection.close()
    if row is None:
        return _receipt("CANDIDATE_STATUS", "REFUSED", work_item_id,
                        row_version=item["row_version"], reason_code="CANDIDATE_NOT_REGISTERED",
                        next_step={"action": "PREPARE_CANDIDATE", "arguments": {}})
    lifecycle = payload.get("lifecycle") or row["event_type"].replace("CANDIDATE_", "")
    path = manifest_path
    if lifecycle == "QUARANTINED":
        path = os.path.join(managed, payload["quarantinePath"].split("managed/", 1)[-1], "candidate.json")
    if not os.path.isfile(path) or os.path.islink(path):
        return _receipt("CANDIDATE_STATUS", "REFUSED", work_item_id,
                        row["event_id"], item["row_version"], "CANDIDATE_RECOVERY_REQUIRED",
                        {"action": "RESTORE_EXACT_CANDIDATE", "arguments": {"candidate": candidate_id}})
    manifest = _load_json(path, CANDIDATE_PROTOCOL)
    if manifest.get("candidateId") != candidate_id or manifest.get("ownerAgentId") != payload.get("ownerAgentId"):
        return _receipt("CANDIDATE_STATUS", "REFUSED", work_item_id,
                        row["event_id"], item["row_version"], "CANDIDATE_MANIFEST_DRIFT",
                        {"action": "HUMAN_INSPECT_CANDIDATE", "arguments": {}})
    return _receipt("CANDIDATE_STATUS", "OK", work_item_id, row["event_id"],
                    item["row_version"], extra={"candidateId": candidate_id,
                    "lifecycle": lifecycle, "candidateFingerprint": payload.get("candidateFingerprint"),
                    "treeSha256": payload.get("treeSha256")})


def freeze(database, project_root, repository_key, work_item_id, candidate_id,
           allowlist_file, owner, request_id, orchestrator_id=None,
           orchestrator_generation=None):
    allowlist_file = _project_file(project_root, allowlist_file, "allowlist file")
    allow = _load_json(allowlist_file)
    if set(allow) != {"paths"} or not isinstance(allow["paths"], list) or not allow["paths"]:
        raise lite.LiteError("allowlist must contain only non-empty paths")
    paths = sorted(allow["paths"])
    if len(paths) != len(set(paths)) or any(not isinstance(path, str) or path.startswith("/") or ".." in path.split("/") for path in paths):
        raise lite.LiteError("candidate allowlist is invalid")
    root, managed, manifest_path = _candidate_manifest(project_root, work_item_id, candidate_id)
    manifest = _load_json(manifest_path, CANDIDATE_PROTOCOL)
    if (manifest.get("lifecycle") not in ("PREPARED", "FROZEN") or
            manifest.get("ownerAgentId") != owner):
        raise lite.LiteError("candidate is not owned PREPARED state")
    source = os.path.join(os.path.dirname(manifest_path), "source")
    if _git(source, "status", "--porcelain"):
        raise lite.LiteError("frozen candidate must be clean")
    head = _git(source, "rev-parse", "HEAD")
    parents = _git(source, "rev-list", "--parents", "-n", "1", head).split()
    if len(parents) != 2 or parents[1] != manifest["base"]["sourceCommit"]:
        raise lite.LiteError("frozen candidate must be direct single-parent successor")
    changed = sorted(filter(None, _git(source, "diff", "--name-only", parents[1], head).splitlines()))
    if changed != paths:
        raise lite.LiteError("candidate changed paths differ from exact allowlist")
    tag = manifest["target"]["tag"]
    if _git(source, "cat-file", "-t", tag) != "tag" or _git(source, "rev-parse", tag + "^{commit}") != head:
        raise lite.LiteError("target tag must be the unique annotated candidate tag")
    tags = sorted(filter(None, _git(source, "tag", "--points-at", "HEAD").splitlines()))
    if tags != [tag]:
        raise lite.LiteError("candidate HEAD must have exactly the target tag")
    frozen = {
        "headCommit": head, "headTree": _git(source, "rev-parse", "HEAD^{tree}"),
        "parentCommit": parents[1], "tag": tag,
        "tagObject": _git(source, "rev-parse", tag), "tagPeel": head,
        "changedPaths": paths, "changedPathsSha256": _sha(_json(paths)),
        "sourceTreeSha256": _tree_fingerprint(source),
    }
    fingerprint = _sha(_json(frozen))
    request_fingerprint = _sha(_json({"candidate": candidate_id,
                                      "fingerprint": fingerprint,
                                      "owner": owner}))
    replay_connection = lite.open_database(database)
    try:
        replay = replay_connection.execute(
            "SELECT * FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if replay is not None:
            payload = json.loads(replay["payload_json"])
            if (replay["event_type"] != "CANDIDATE_FROZEN" or
                    payload.get("candidateFingerprint") != fingerprint or
                    payload.get("requestFingerprint") != request_fingerprint or
                    not isinstance(payload.get("resultReceipt"), dict)):
                raise lite.LiteError("request_id was already used with different content")
            return payload["resultReceipt"]
    finally:
        replay_connection.close()
    if manifest.get("lifecycle") != "PREPARED":
        raise lite.LiteError("candidate is not owned PREPARED state")
    original_manifest = dict(manifest)
    manifest.update({"lifecycle": "FROZEN", "frozen": frozen,
                     "candidateFingerprint": fingerprint,
                     "freezeRequestId": request_id})
    filesystem_mutated = False
    commit_attempted = False
    connection = lite.open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item, unused_claim, unused_writer = _runtime_guard(connection, work_item_id, owner,
                                                            repository_key, orchestrator_id,
                                                            orchestrator_generation)
        snapshot = lite._kernel_assert(
            connection, work_item_id, project_root, phase="pre"
        )
        replay = connection.execute("SELECT * FROM events WHERE request_id=?", (request_id,)).fetchone()
        if replay:
            payload = json.loads(replay["payload_json"])
            if (replay["event_type"] != "CANDIDATE_FROZEN" or
                    payload.get("candidateFingerprint") != fingerprint or
                    payload.get("requestFingerprint") != request_fingerprint or
                    not isinstance(payload.get("resultReceipt"), dict)):
                raise lite.LiteError("request_id was already used with different content")
            connection.rollback()
            return payload["resultReceipt"]
        head_row, head_payload = _candidate_head(connection, work_item_id, candidate_id)
        if head_row is None or head_row["event_type"] != "CANDIDATE_PREPARED" or head_payload.get("ownerAgentId") != owner:
            raise lite.LiteError("candidate runtime head is not PREPARED")
        current_manifest = _load_json(manifest_path, CANDIDATE_PROTOCOL)
        if current_manifest != original_manifest:
            raise lite.LiteError("candidate manifest drifted before freeze commit")
        _replace_json(manifest_path, manifest, "freeze-" + _sha(request_id)[:16])
        filesystem_mutated = True
        event_payload = dict(head_payload)
        event_payload.update({"lifecycle": "FROZEN", "frozen": frozen,
                              "candidateFingerprint": fingerprint,
                              "freezeRequestId": request_id,
                              "requestFingerprint": request_fingerprint})
        event = connection.execute(
            "SELECT coalesce(max(event_id),0)+1 FROM events"
        ).fetchone()[0]
        result = _receipt(
            "CANDIDATE_FROZEN", "OK", work_item_id, event,
            item["row_version"] + 1,
            extra={"candidateId": candidate_id,
                   "candidateFingerprint": fingerprint},
        )
        now = lite._now()
        event_payload.update({"resultRowVersion": result["rowVersion"],
                              "resultReceipt": result})
        snapshot = lite._kernel_snapshot(connection, work_item_id, project_root,
                                         evaluation_time=now)
        intent = {"operation": "CANDIDATE_FREEZE", "workItemId": work_item_id,
                  "eventType": "CANDIDATE_FROZEN", "payload": event_payload,
                  "eventId": event, "actorKind": "AGENT", "actorId": owner,
                  "requestId": request_id, "now": now, "receipt": result}
        plan = workflow_kernel.plan_audit_touch(snapshot, intent)
        lite._kernel_apply(connection, work_item_id, snapshot, plan, project_root,
                           evaluation_time=now)
        _candidate_materialized(connection, "CANDIDATE_FROZEN", request_id)
        commit_attempted = True
        connection.commit()
    except Exception:
        connection.rollback()
        outcome = (_resolve_commit_outcome(
            database, work_item_id, request_id, "CANDIDATE_FROZEN",
            {"candidateId": candidate_id,
             "candidateFingerprint": fingerprint,
             "freezeRequestId": request_id,
             "requestFingerprint": request_fingerprint},
            candidate_id,
        ) if commit_attempted else None)
        if outcome is not None:
            return outcome
        if filesystem_mutated:
            try:
                current_manifest = _load_json(manifest_path, CANDIDATE_PROTOCOL)
                if (current_manifest == manifest and
                        current_manifest.get("freezeRequestId") == request_id):
                    _replace_json(manifest_path, original_manifest,
                                  "freeze-compensate-" + _sha(request_id)[:16])
                elif current_manifest != original_manifest:
                    raise lite.LiteError("freeze compensation found manifest drift")
            except Exception as compensation:
                raise lite.LiteError("freeze recovery required: {0}".format(compensation))
        raise
    finally:
        connection.close()
    return result


scan_artifact = verify.scan_artifact


def _artifact_build_identities(wheel_path, sdist_path):
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            raw = archive.read("agent_workboard/_build.py").decode("utf-8")
            wheel_identity = ast.literal_eval(raw.split("=", 1)[1].strip())
        with tarfile.open(sdist_path, "r:gz") as archive:
            matches = [member for member in archive.getmembers()
                       if member.isfile() and
                       member.name.endswith("/.awb-release-identity.json")]
            if len(matches) != 1:
                raise ValueError("sdist identity envelope is not unique")
            handle = archive.extractfile(matches[0])
            if handle is None:
                raise ValueError("sdist identity envelope is unreadable")
            sdist_identity = json.loads(handle.read().decode("utf-8"))
    except Exception as exc:
        raise lite.LiteError("locked build identity is invalid: {0}".format(exc))
    return wheel_identity, sdist_identity


def build(database, project_root, repository_key, work_item_id, candidate_id,
          toolchain_file, owner, request_id, orchestrator_id=None,
          orchestrator_generation=None):
    toolchain_file = _project_file(project_root, toolchain_file, "toolchain file")
    toolchain = _load_json(toolchain_file, BUILD_PROTOCOL)
    if set(toolchain) != {"protocolVersion", "toolchainId", "python", "packages", "backend", "environment"}:
        raise lite.LiteError("toolchain fields are invalid")
    python = toolchain["python"]
    packages = toolchain["packages"]
    environment = toolchain["environment"]
    if (set(python) != {"canonicalExecutable", "executableSha256", "implementation", "version"} or
            set(packages) != {"setuptools", "wheel"} or
            python["implementation"] != "CPython" or
            toolchain["backend"] != "SETUPTOOLS_BDIST_WHEEL_SDIST" or
            set(environment) != {"TZ", "LC_ALL", "SOURCE_DATE_EPOCH"} or
            environment["TZ"] != "UTC" or environment["LC_ALL"] != "C" or
            environment["SOURCE_DATE_EPOCH"] != "COMMIT_EPOCH" or
            any("*" in str(value) for value in list(packages.values()) + [python["version"]])):
        raise lite.LiteError("toolchain contract is invalid")
    _slug(toolchain["toolchainId"], "toolchainId")
    toolchain_fingerprint = _sha(_json(toolchain))
    executable = os.path.realpath(python["canonicalExecutable"])
    root, managed, manifest_path = _candidate_manifest(
        project_root, work_item_id, candidate_id
    )
    manifest = _load_json(manifest_path, CANDIDATE_PROTOCOL)
    staging = os.path.join(managed, "staging", candidate_id, request_id)
    target = os.path.join(managed, "active", candidate_id, "builds", request_id)
    quarantine = os.path.join(managed, "quarantine", candidate_id, request_id)
    source = os.path.join(staging, "source")
    assets = os.path.realpath(os.path.join(staging, "assets"))
    if (assets != os.path.join(staging, "assets") or
            os.path.commonpath((managed, assets)) != managed):
        raise lite.LiteError("managed build assets path is invalid")
    frozen_identity = manifest.get("frozen")
    commit_epoch = (_git(
        os.path.join(os.path.dirname(manifest_path), "source"),
        "show", "-s", "--format=%ct", frozen_identity["headCommit"],
    ) if isinstance(frozen_identity, dict) else "UNFROZEN")
    env = {key: value for key, value in os.environ.items()
           if not (key.upper().startswith("PIP_") or "PROXY" in key.upper() or
                   "TOKEN" in key.upper() or "SECRET" in key.upper() or
                   "PASSWORD" in key.upper())}
    env.update({"TZ": "UTC", "LC_ALL": "C", "SOURCE_DATE_EPOCH": commit_epoch})
    build_command = [
        executable, "setup.py", "bdist_wheel", "--dist-dir", assets,
        "sdist", "--dist-dir", assets,
    ]
    request_fingerprint = _sha(_json({
        "candidateId": candidate_id, "owner": owner,
        "toolchainFingerprint": toolchain_fingerprint,
        "argv": build_command, "cwd": source, "environment": env,
        "assetsPath": assets,
    }))
    replay_connection = lite.open_database(database)
    try:
        replay = replay_connection.execute(
            "SELECT * FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if replay is not None:
            payload = json.loads(replay["payload_json"])
            if (replay["event_type"] != "CANDIDATE_BUILT" or
                    payload.get("candidateId") != candidate_id or
                    payload.get("ownerAgentId") != owner or
                    payload.get("toolchainFingerprint") != toolchain_fingerprint or
                    payload.get("requestFingerprint") != request_fingerprint or
                    not isinstance(payload.get("resultReceipt"), dict)):
                raise lite.LiteError("request_id was already used with different content")
            return payload["resultReceipt"]
    finally:
        replay_connection.close()
    if not os.path.isfile(executable) or os.path.islink(python["canonicalExecutable"]):
        raise lite.LiteError("TOOLCHAIN_NOT_READY")
    if _file_sha(executable) != python["executableSha256"]:
        raise lite.LiteError("TOOLCHAIN_NOT_READY")
    probe = subprocess.check_output([executable, "-c",
        "import json,platform,setuptools,wheel; print(json.dumps([platform.python_implementation(),platform.python_version(),setuptools.__version__,wheel.__version__]))"],
        stderr=subprocess.STDOUT).decode("utf-8")
    actual = json.loads(probe)
    if actual != [python["implementation"], python["version"], packages["setuptools"], packages["wheel"]]:
        raise lite.LiteError("TOOLCHAIN_NOT_READY")
    if manifest.get("lifecycle") != "FROZEN" or manifest.get("ownerAgentId") != owner:
        raise lite.LiteError("build requires an owned FROZEN candidate")
    if any(os.path.lexists(path) for path in (staging, target, quarantine)):
        raise lite.LiteError("build destination already exists")
    connection = lite.open_database(database)
    try:
        _runtime_guard(connection, work_item_id, owner, repository_key,
                       orchestrator_id, orchestrator_generation)
    finally:
        connection.close()
    os.makedirs(staging)
    subprocess.check_call(["git", "clone", "--no-hardlinks", "--no-checkout",
                           os.path.join(os.path.dirname(manifest_path), "source"), source])
    subprocess.check_call(["git", "checkout", "--detach", manifest["frozen"]["headCommit"]], cwd=source)
    alternates = os.path.join(source, ".git", "objects", "info", "alternates")
    if (os.path.exists(alternates) or _git(source, "status", "--porcelain") or
            _git(source, "rev-parse", "HEAD") != manifest["frozen"]["headCommit"] or
            _git(source, "rev-parse", "HEAD^{tree}") != manifest["frozen"]["headTree"] or
            _git(source, "cat-file", "-t", manifest["target"]["tag"]) != "tag" or
            _git(source, "rev-parse", manifest["target"]["tag"] + "^{commit}") !=
            manifest["frozen"]["headCommit"] or
            sorted(_git(source, "tag", "--points-at", "HEAD").splitlines()) !=
            [manifest["target"]["tag"]] or
            sorted(filter(None, _git(source, "diff", "--name-only",
                                     manifest["frozen"]["parentCommit"], "HEAD").splitlines())) !=
            manifest["frozen"]["changedPaths"]):
        raise lite.LiteError("locked build source drifted from frozen identity")
    os.makedirs(assets)
    try:
        subprocess.check_call(build_command, cwd=source, env=env)
        unexpected = []
        for directory, unused_subdirectories, files in os.walk(staging):
            if os.path.commonpath((assets, directory)) == assets:
                continue
            for name in files:
                if name.endswith((".whl", ".tar.gz")):
                    unexpected.append(os.path.join(directory, name))
        if os.path.lexists(os.path.join(source, "dist")) or unexpected:
            raise lite.LiteError("locked build produced artifacts outside managed assets")
        names = sorted(os.listdir(assets))
        if len(names) != 2 or sum(name.endswith(".whl") for name in names) != 1 or sum(name.endswith(".tar.gz") for name in names) != 1:
            raise lite.LiteError("locked build must produce exactly wheel and sdist")
        scans = [scan_artifact(os.path.join(assets, name)) for name in names]
        expected_names = sorted(name for name in manifest["target"]["assetNames"] if name != "SHA256SUMS")
        if names != expected_names:
            raise lite.LiteError("locked build asset names drifted from target")
        wheel_name = next(name for name in names if name.endswith(".whl"))
        sdist_name = next(name for name in names if name.endswith(".tar.gz"))
        wheel_identity, sdist_identity = _artifact_build_identities(
            os.path.join(assets, wheel_name), os.path.join(assets, sdist_name)
        )
        expected_identity = {
            "packageVersion": manifest["target"]["version"],
            "sourceCommit": manifest["frozen"]["headCommit"],
            "sourceTree": manifest["frozen"]["headTree"],
            "sourceTag": manifest["target"]["tag"],
        }
        if wheel_identity != expected_identity or sdist_identity != expected_identity:
            raise lite.LiteError("wheel and sdist identity drifted from frozen candidate")
        sums = "".join("{0}  {1}\n".format(scan["sha256"], scan["artifact"])
                       for scan in sorted(scans, key=lambda row: row["artifact"]))
        with open(os.path.join(assets, "SHA256SUMS"), "x", encoding="ascii") as handle:
            handle.write(sums); handle.flush(); os.fsync(handle.fileno())
        artifacts = [{"name": name, "size": os.path.getsize(os.path.join(assets, name)),
                      "sha256": _file_sha(os.path.join(assets, name))}
                     for name in sorted(os.listdir(assets))]
        if len(artifacts) != 3:
            raise lite.LiteError("locked build must retain exactly three assets")
    except Exception:
        os.makedirs(os.path.dirname(quarantine), exist_ok=True)
        if os.path.isdir(staging) and not os.path.lexists(quarantine):
            failure_hash = _tree_fingerprint(staging)
            failure_journal = os.path.join(
                managed, "journal", candidate_id, request_id + ".json"
            )
            _write_json(failure_journal, {
                "protocolVersion": CANDIDATE_PROTOCOL, "operation": "BUILD",
                "kind": "BUILD_STAGING", "workItemId": work_item_id,
                "candidateId": candidate_id, "owner": owner,
                "requestId": request_id, "fingerprint": failure_hash,
                "beforePath": _relative(root, staging),
                "afterPath": _relative(root, quarantine),
                "beforeTreeSha256": failure_hash,
                "afterTreeSha256": failure_hash,
                "status": "RECOVERY_REQUIRED",
            })
            os.replace(staging, quarantine)
        raise
    build_fingerprint = _sha(_json({"candidateFingerprint": manifest["candidateFingerprint"],
                                    "toolchainFingerprint": toolchain_fingerprint,
                                    "artifacts": artifacts}))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tree_hash = _tree_fingerprint(staging)
    journal = os.path.join(managed, "journal", candidate_id, request_id + ".json")
    journal_value = {
        "protocolVersion": CANDIDATE_PROTOCOL, "operation": "BUILD",
        "kind": "BUILD_STAGING", "workItemId": work_item_id,
        "candidateId": candidate_id, "owner": owner, "requestId": request_id,
        "fingerprint": tree_hash, "beforePath": _relative(root, staging),
        "afterPath": _relative(root, target), "beforeTreeSha256": tree_hash,
        "afterTreeSha256": tree_hash, "status": "PREPARED",
    }
    _write_json(journal, journal_value)
    original_manifest = dict(manifest)
    manifest.update({"lifecycle": "BUILT", "buildRequestId": request_id,
                     "buildFingerprint": build_fingerprint,
                     "toolchainFingerprint": toolchain_fingerprint,
                     "artifacts": artifacts, "buildPath": _relative(root, target)})
    filesystem_mutated = False
    commit_attempted = False
    connection = lite.open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item, unused_claim, unused_writer = _runtime_guard(connection, work_item_id, owner,
                                                            repository_key, orchestrator_id,
                                                            orchestrator_generation)
        lite._kernel_assert(connection, work_item_id, project_root, phase="pre")
        head_row, head_payload = _candidate_head(connection, work_item_id, candidate_id)
        if (head_row is None or head_row["event_type"] != "CANDIDATE_FROZEN" or
                head_payload.get("candidateFingerprint") !=
                manifest["candidateFingerprint"]):
            raise lite.LiteError("candidate runtime head drifted before build commit")
        if _load_json(manifest_path, CANDIDATE_PROTOCOL) != original_manifest:
            raise lite.LiteError("candidate manifest drifted before build commit")
        os.replace(staging, target)
        filesystem_mutated = True
        _replace_json(manifest_path, manifest, "build-" + _sha(request_id)[:16])
        payload = dict(head_payload)
        payload.update({"lifecycle": "BUILT", "buildRequestId": request_id,
                        "toolchainFingerprint": toolchain_fingerprint,
                        "requestFingerprint": request_fingerprint,
                        "buildFingerprint": build_fingerprint, "artifacts": artifacts,
                        "scans": scans, "buildPath": _relative(root, target)})
        event = connection.execute(
            "SELECT coalesce(max(event_id),0)+1 FROM events"
        ).fetchone()[0]
        result = _receipt(
            "CANDIDATE_BUILT", "OK", work_item_id, event,
            item["row_version"] + 1,
            extra={"candidateId": candidate_id,
                   "candidateFingerprint": manifest["candidateFingerprint"],
                   "buildFingerprint": build_fingerprint,
                   "artifacts": artifacts},
        )
        now = lite._now()
        payload.update({"resultRowVersion": result["rowVersion"],
                        "resultReceipt": result})
        snapshot = lite._kernel_snapshot(connection, work_item_id, project_root,
                                         evaluation_time=now)
        intent = {"operation": "CANDIDATE_BUILD", "workItemId": work_item_id,
                  "eventType": "CANDIDATE_BUILT", "payload": payload,
                  "eventId": event, "actorKind": "AGENT", "actorId": owner,
                  "requestId": request_id, "now": now, "receipt": result}
        plan = workflow_kernel.plan_audit_touch(snapshot, intent)
        lite._kernel_apply(connection, work_item_id, snapshot, plan, project_root,
                           evaluation_time=now)
        _candidate_materialized(connection, "CANDIDATE_BUILT", request_id)
        lite._kernel_assert(connection, work_item_id, project_root, phase="post")
        commit_attempted = True
        connection.commit()
    except Exception:
        connection.rollback()
        outcome = (_resolve_commit_outcome(
            database, work_item_id, request_id, "CANDIDATE_BUILT",
            {"candidateId": candidate_id, "ownerAgentId": owner,
             "candidateFingerprint": manifest["candidateFingerprint"],
             "toolchainFingerprint": toolchain_fingerprint,
             "buildFingerprint": build_fingerprint,
             "requestFingerprint": request_fingerprint},
            candidate_id,
        ) if commit_attempted else None)
        if outcome is not None:
            _commit_journal(journal, journal_value, request_id)
            return outcome
        recovery_reason = None
        if filesystem_mutated:
            try:
                current_journal = _load_json(journal, CANDIDATE_PROTOCOL)
                if current_journal != journal_value:
                    recovery_reason = "JOURNAL_OWNERSHIP_DRIFT"
                elif not os.path.isdir(target) or os.path.islink(target):
                    recovery_reason = "TARGET_PATH_DRIFT"
                elif _tree_fingerprint(target) != journal_value["afterTreeSha256"]:
                    recovery_reason = "TARGET_TREE_DRIFT"
                elif os.path.lexists(staging):
                    recovery_reason = "STAGING_DESTINATION_CONFLICT"
                else:
                    os.makedirs(os.path.dirname(staging), exist_ok=True)
                    try:
                        _rename_exclusive(target, staging)
                    except Exception:
                        recovery_reason = "STAGING_DESTINATION_RACE"
                if recovery_reason:
                    _mark_journal_recovery(journal, journal_value, request_id)
            except Exception as compensation:
                raise lite.LiteError(
                    "BUILD_RECOVERY_REQUIRED: {0}".format(compensation)
                )
        if filesystem_mutated:
            try:
                current_manifest = _load_json(manifest_path, CANDIDATE_PROTOCOL)
                if (current_manifest == manifest and
                        current_manifest.get("buildRequestId") == request_id):
                    _replace_json(manifest_path, original_manifest,
                                  "build-compensate-" + _sha(request_id)[:16])
                elif current_manifest != original_manifest:
                    raise lite.LiteError("build compensation found manifest drift")
            except Exception as compensation:
                raise lite.LiteError("build recovery required: {0}".format(compensation))
        if recovery_reason:
            raise lite.LiteError("BUILD_RECOVERY_REQUIRED: {0}".format(recovery_reason))
        raise
    finally:
        connection.close()
    _commit_journal(journal, journal_value, request_id)
    return result


def quarantine(database, project_root, repository_key, work_item_id, candidate_id,
               owner, request_id, restore=False, orchestrator_id=None,
               orchestrator_generation=None, source_request_id=None):
    root, managed, manifest_path = _candidate_manifest(project_root, work_item_id, candidate_id)
    _slug(request_id, "requestId")
    operation = "RESTORE" if restore else "QUARANTINE"
    event_type = "CANDIDATE_RESTORED" if restore else "CANDIDATE_QUARANTINED"
    kind = "WHOLE_CANDIDATE"
    prior_lifecycle = None
    if restore and not source_request_id:
        raise lite.LiteError("restore requires the exact quarantine request-id")
    if source_request_id:
        _slug(source_request_id, "quarantine requestId")
    request_fingerprint = _sha(_json({
        "operation": operation, "workItemId": work_item_id,
        "candidateId": candidate_id, "owner": owner,
        "sourceRequestId": source_request_id,
    }))
    replay_connection = lite.open_database(database)
    try:
        replay = replay_connection.execute(
            "SELECT * FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if replay is not None:
            payload = json.loads(replay["payload_json"])
            if (replay["event_type"] != event_type or
                    payload.get("requestFingerprint") != request_fingerprint or
                    not isinstance(payload.get("resultReceipt"), dict)):
                raise lite.LiteError("request_id was already used with different content")
            return payload["resultReceipt"]
    finally:
        replay_connection.close()
    if restore:
        source_journal = _load_json(os.path.join(
            managed, "journal", candidate_id, source_request_id + ".json"
        ), CANDIDATE_PROTOCOL)
        if (source_journal.get("candidateId") != candidate_id or
                source_journal.get("owner") != owner or
                source_journal.get("kind") not in ("WHOLE_CANDIDATE", "BUILD_STAGING")):
            raise lite.LiteError("restore journal identity is invalid")
        kind = source_journal["kind"]
        source = os.path.realpath(os.path.join(root, source_journal["afterPath"]))
        target = (os.path.join(managed, "active", candidate_id) if kind == "WHOLE_CANDIDATE"
                  else os.path.join(managed, "staging", candidate_id, source_request_id))
        prior_lifecycle = source_journal.get("priorLifecycle")
        if prior_lifecycle is None and kind == "BUILD_STAGING":
            active_manifest = os.path.join(
                managed, "active", candidate_id, "candidate.json"
            )
            prior_lifecycle = _load_json(
                active_manifest, CANDIDATE_PROTOCOL
            ).get("lifecycle")
        if (_relative(root, source) != source_journal["afterPath"] or
                _relative(root, target) != source_journal["beforePath"]):
            raise lite.LiteError("restore journal paths are invalid")
    else:
        source = os.path.join(managed, "active", candidate_id)
        target = os.path.join(managed, "quarantine", candidate_id, request_id)
        manifest = _load_json(manifest_path, CANDIDATE_PROTOCOL)
        if manifest.get("ownerAgentId") != owner:
            raise lite.LiteError("candidate quarantine owner drift")
        prior_lifecycle = manifest.get("lifecycle")
    if not os.path.isdir(source) or os.path.islink(source) or os.path.lexists(target):
        raise lite.LiteError("quarantine source or destination is unsafe")
    before_hash = _tree_fingerprint(source)
    if restore and before_hash != source_journal.get("afterTreeSha256"):
        raise lite.LiteError("restore source bytes drifted from journal")
    journal = os.path.join(managed, "journal", candidate_id, request_id + ".json")
    journal_value = {
        "protocolVersion": CANDIDATE_PROTOCOL, "operation": operation,
        "kind": kind, "workItemId": work_item_id, "candidateId": candidate_id,
        "owner": owner, "requestId": request_id, "fingerprint": before_hash,
        "beforePath": _relative(root, source), "afterPath": _relative(root, target),
        "beforeTreeSha256": before_hash, "afterTreeSha256": before_hash,
        "priorLifecycle": prior_lifecycle, "status": "PREPARED",
    }
    _write_json(journal, journal_value)
    filesystem_mutated = False
    commit_attempted = False
    connection = lite.open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item, unused_claim, unused_writer = _runtime_guard(connection, work_item_id, owner,
                                                            repository_key, orchestrator_id,
                                                            orchestrator_generation)
        lite._kernel_assert(connection, work_item_id, project_root, phase="pre")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(source, target)
        filesystem_mutated = True
        payload = {"protocolVersion": CANDIDATE_PROTOCOL, "candidateId": candidate_id,
                   "ownerAgentId": owner,
                   "lifecycle": prior_lifecycle if restore else "QUARANTINED",
                   "priorLifecycle": prior_lifecycle, "kind": kind,
                   "treeSha256": before_hash, "beforePath": _relative(root, source),
                   "afterPath": _relative(root, target),
                   "quarantinePath": _relative(root, source if restore else target),
                   "requestFingerprint": request_fingerprint}
        event = connection.execute(
            "SELECT coalesce(max(event_id),0)+1 FROM events"
        ).fetchone()[0]
        result = _receipt(
            event_type, "OK", work_item_id, event, item["row_version"] + 1,
            extra={"candidateId": candidate_id, "treeSha256": before_hash},
        )
        now = lite._now()
        payload.update({"resultRowVersion": result["rowVersion"],
                        "resultReceipt": result})
        snapshot = lite._kernel_snapshot(connection, work_item_id, project_root,
                                         evaluation_time=now)
        intent = {"operation": "CANDIDATE_QUARANTINE", "workItemId": work_item_id,
                  "eventType": event_type, "payload": payload,
                  "eventId": event, "actorKind": "AGENT", "actorId": owner,
                  "requestId": request_id, "now": now, "receipt": result}
        plan = workflow_kernel.plan_audit_touch(snapshot, intent)
        lite._kernel_apply(connection, work_item_id, snapshot, plan, project_root,
                           evaluation_time=now)
        _candidate_materialized(connection, event_type, request_id)
        commit_attempted = True
        connection.commit()
    except Exception:
        connection.rollback()
        outcome = (_resolve_commit_outcome(
            database, work_item_id, request_id, event_type,
            {"candidateId": candidate_id, "ownerAgentId": owner,
             "requestFingerprint": request_fingerprint,
             "treeSha256": before_hash},
            candidate_id,
        ) if commit_attempted else None)
        if outcome is not None:
            _commit_journal(journal, journal_value, request_id)
            return outcome
        if (filesystem_mutated and os.path.isdir(target) and
                not os.path.lexists(source) and
                _tree_fingerprint(target) == before_hash):
            os.makedirs(os.path.dirname(source), exist_ok=True)
            os.replace(target, source)
        raise
    finally:
        connection.close()
    _commit_journal(journal, journal_value, request_id)
    return result


def finalize(database, project_root, repository_key, work_item_id, candidate_id,
             owner, request_id, orchestrator_id=None, orchestrator_generation=None):
    root, managed, manifest_path = _candidate_manifest(project_root, work_item_id, candidate_id)
    manifest = _load_json(manifest_path, CANDIDATE_PROTOCOL)
    request_fingerprint = _sha(_json({"candidateId": candidate_id,
                                      "owner": owner}))
    replay_connection = lite.open_database(database)
    try:
        replay = replay_connection.execute(
            "SELECT * FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if replay is not None:
            payload = json.loads(replay["payload_json"])
            if (replay["event_type"] != "CANDIDATE_FINALIZED" or
                    payload.get("candidateId") != candidate_id or
                    payload.get("ownerAgentId") != owner or
                    payload.get("requestFingerprint") != request_fingerprint or
                    not isinstance(payload.get("resultReceipt"), dict)):
                raise lite.LiteError("request_id was already used with different content")
            return payload["resultReceipt"]
    finally:
        replay_connection.close()
    if manifest.get("ownerAgentId") != owner or manifest.get("lifecycle") not in ("FROZEN", "BUILT"):
        raise lite.LiteError("candidate cannot be finalized")
    original_manifest = dict(manifest)
    manifest["lifecycle"] = "FINALIZED"
    manifest["bytesRetained"] = True
    manifest["finalizeRequestId"] = request_id
    filesystem_mutated = False
    commit_attempted = False
    connection = lite.open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item, unused_claim, unused_writer = _runtime_guard(connection, work_item_id, owner,
                                                            repository_key, orchestrator_id,
                                                            orchestrator_generation)
        lite._kernel_assert(connection, work_item_id, project_root, phase="pre")
        head_row, head_payload = _candidate_head(connection, work_item_id, candidate_id)
        if (head_row is None or head_row["event_type"] not in
                ("CANDIDATE_FROZEN", "CANDIDATE_BUILT") or
                head_payload.get("candidateFingerprint") !=
                manifest.get("candidateFingerprint")):
            raise lite.LiteError("candidate runtime head drifted before finalize commit")
        if _load_json(manifest_path, CANDIDATE_PROTOCOL) != original_manifest:
            raise lite.LiteError("candidate manifest drifted before finalize commit")
        _replace_json(manifest_path, manifest, "finalize-" + _sha(request_id)[:16])
        filesystem_mutated = True
        payload = {"protocolVersion": CANDIDATE_PROTOCOL, "candidateId": candidate_id,
                   "ownerAgentId": owner, "candidateFingerprint": manifest["candidateFingerprint"],
                   "lifecycle": "FINALIZED", "bytesRetained": True,
                   "finalizeRequestId": request_id,
                   "requestFingerprint": request_fingerprint}
        event = connection.execute(
            "SELECT coalesce(max(event_id),0)+1 FROM events"
        ).fetchone()[0]
        result = _receipt(
            "CANDIDATE_FINALIZED", "OK", work_item_id, event,
            item["row_version"] + 1,
            extra={"candidateId": candidate_id, "bytesRetained": True},
        )
        now = lite._now()
        payload.update({"resultRowVersion": result["rowVersion"],
                        "resultReceipt": result})
        snapshot = lite._kernel_snapshot(connection, work_item_id, project_root,
                                         evaluation_time=now)
        intent = {"operation": "CANDIDATE_FINALIZE", "workItemId": work_item_id,
                  "eventType": "CANDIDATE_FINALIZED", "payload": payload,
                  "eventId": event, "actorKind": "AGENT", "actorId": owner,
                  "requestId": request_id, "now": now, "receipt": result}
        plan = workflow_kernel.plan_audit_touch(snapshot, intent)
        lite._kernel_apply(connection, work_item_id, snapshot, plan, project_root,
                           evaluation_time=now)
        _candidate_materialized(connection, "CANDIDATE_FINALIZED", request_id)
        commit_attempted = True
        connection.commit()
    except Exception:
        connection.rollback()
        outcome = (_resolve_commit_outcome(
            database, work_item_id, request_id, "CANDIDATE_FINALIZED",
            {"candidateId": candidate_id, "ownerAgentId": owner,
             "candidateFingerprint": manifest["candidateFingerprint"],
             "finalizeRequestId": request_id,
             "requestFingerprint": request_fingerprint},
            candidate_id,
        ) if commit_attempted else None)
        if outcome is not None:
            return outcome
        if filesystem_mutated:
            try:
                current_manifest = _load_json(manifest_path, CANDIDATE_PROTOCOL)
                if (current_manifest == manifest and
                        current_manifest.get("finalizeRequestId") == request_id):
                    _replace_json(manifest_path, original_manifest,
                                  "finalize-compensate-" + _sha(request_id)[:16])
                elif current_manifest != original_manifest:
                    raise lite.LiteError("finalize compensation found manifest drift")
            except Exception as compensation:
                raise lite.LiteError("finalize recovery required: {0}".format(compensation))
        raise
    finally:
        connection.close()
    return result


def release_submission_candidate(connection, work_item_id):
    row, payload = _latest_event(connection, work_item_id, "CANDIDATE_BUILT")
    if row is None:
        return None
    later, unused = _latest_event(connection, work_item_id,
                                  ("CANDIDATE_QUARANTINED", "CANDIDATE_FINALIZED"))
    if later and later["event_id"] > row["event_id"] and json.loads(later["payload_json"]).get("candidateId") == payload.get("candidateId"):
        return None
    return {"candidateId": payload["candidateId"],
            "candidateFingerprint": payload["candidateFingerprint"],
            "buildFingerprint": payload["buildFingerprint"],
            "changedPaths": payload["frozen"]["changedPaths"]}


def _verify_current_build(project_root, work_item_id, built):
    root, managed, manifest_path = _candidate_manifest(
        project_root, work_item_id, built["candidateId"]
    )
    manifest = _load_json(manifest_path, CANDIDATE_PROTOCOL)
    if (manifest.get("lifecycle") not in ("BUILT", "FINALIZED") or
            (manifest.get("lifecycle") == "FINALIZED" and
             manifest.get("bytesRetained") is not True) or
            manifest.get("candidateFingerprint") != built.get("candidateFingerprint") or
            manifest.get("buildFingerprint") != built.get("buildFingerprint")):
        raise lite.LiteError("managed candidate manifest drifted from runtime")
    build_path = os.path.realpath(os.path.join(root, manifest["buildPath"]))
    expected_build = os.path.join(
        managed, "active", built["candidateId"], "builds",
        manifest.get("buildRequestId", ""),
    )
    if build_path != expected_build or not os.path.isdir(build_path):
        raise lite.LiteError("managed build path is invalid")
    source = os.path.join(managed, "active", built["candidateId"], "source")
    frozen = manifest.get("frozen", {})
    if (not os.path.isdir(source) or os.path.islink(source) or
            _git(source, "status", "--porcelain") or
            _git(source, "rev-parse", "HEAD") != frozen.get("headCommit") or
            _git(source, "rev-parse", "HEAD^{tree}") != frozen.get("headTree") or
            _git(source, "cat-file", "-t", frozen.get("tag", "")) != "tag" or
            _git(source, "rev-parse", frozen.get("tag", "") + "^{commit}") !=
            frozen.get("tagPeel") or _tree_fingerprint(source) !=
            frozen.get("sourceTreeSha256")):
        raise lite.LiteError("managed frozen source bytes drifted")
    assets_path = os.path.join(build_path, "assets")
    actual_names = sorted(os.listdir(assets_path)) if os.path.isdir(assets_path) else []
    expected = sorted(manifest["artifacts"], key=lambda row: row["name"])
    if actual_names != [row["name"] for row in expected]:
        raise lite.LiteError("managed build asset set drifted")
    actual = []
    for row in expected:
        path = os.path.join(assets_path, row["name"])
        if not os.path.isfile(path) or os.path.islink(path):
            raise lite.LiteError("managed build asset is unsafe")
        actual.append({"name": row["name"], "size": os.path.getsize(path),
                       "sha256": _file_sha(path)})
    if actual != expected:
        raise lite.LiteError("managed build asset bytes drifted")
    return manifest, actual


def _executing_wheel():
    """Return the exact wheel supplying this module, never an ambient source."""
    marker = ".whl" + os.sep
    module_path = os.path.abspath(__file__)
    position = module_path.find(marker)
    if position >= 0:
        path = module_path[:position + 4]
        return path if os.path.isfile(path) and not os.path.islink(path) else None
    try:
        from .project import _direct_wheel
        direct = _direct_wheel()
    except Exception:
        direct = None
    if (not isinstance(direct, tuple) or not direct or
            not os.path.isfile(direct[0]) or os.path.islink(direct[0])):
        return None
    if direct[1] is not None and _file_sha(direct[0]) != direct[1]:
        return None
    return os.path.realpath(direct[0])


def assert_self_host_operation(connection, project_root, work_item_id, operation,
                               candidate_id=None):
    """Enforce the exact b8-on-b7 bootstrap closed set at library boundaries."""
    from . import __version__
    from ._build import BUILD_IDENTITY
    root = _project(project_root)
    config_path = os.path.join(root, ".awb", "config.json")
    if not os.path.exists(config_path):
        return "UNMANAGED"
    config = _load_json(config_path)
    if config.get("runtimeMode") == "development":
        return "DEVELOPMENT"
    if config.get("requiredPackageVersion") == __version__:
        return "INSTALLED"
    exact_b7 = {
        "requiredPackageVersion": "0.3.1b7",
        "requiredSourceCommit": "c7380024f9b0efcd3167cebcd9915b2f1d85d13a",
        "requiredSourceTree": "102cc5ef820b9f7d42166012c67dfd8708e61419",
        "requiredSourceTag": "v0.3.1b7",
    }
    if (__version__ != "0.3.1b8" or
            any(config.get(key) != value for key, value in exact_b7.items()) or
            config.get("usagePolicy") != "OFF" or
            config.get("projectId") !=
            "project-372e08efb9354d13bbc57fb2f3405e94" or
            config.get("repositoryKey") != "agent-workboard-ops" or
            config.get("database") != ".awb/workboard.db" or
            work_item_id != "AWB-027"):
        raise lite.LiteError("SELF_HOST_BOOTSTRAP_REFUSED")
    databases = connection.execute("PRAGMA database_list").fetchall()
    main_paths = [os.path.realpath(row[2]) for row in databases if row[1] == "main"]
    if main_paths != [os.path.realpath(os.path.join(root, config["database"]))]:
        raise lite.LiteError("SELF_HOST_BOOTSTRAP_REFUSED")
    try:
        workflow_kernel.assert_self_host_bootstrap_operation(operation)
    except ValueError:
        raise lite.LiteError("SELF_HOST_BOOTSTRAP_REFUSED")
    built = release_submission_candidate(connection, work_item_id)
    if built is None or (candidate_id is not None and
                         built.get("candidateId") != candidate_id):
        raise lite.LiteError("SELF_HOST_BOOTSTRAP_REFUSED")
    manifest, assets = _verify_current_build(project_root, work_item_id, built)
    frozen = manifest.get("frozen", {})
    target = manifest.get("target", {})
    if (target.get("version") != "0.3.1b8" or
            target.get("tag") != "v0.3.1b8" or
            BUILD_IDENTITY != {
                "packageVersion": "0.3.1b8",
                "sourceCommit": frozen.get("headCommit"),
                "sourceTree": frozen.get("headTree"),
                "sourceTag": frozen.get("tag"),
            }):
        raise lite.LiteError("SELF_HOST_BOOTSTRAP_REFUSED")
    wheels = [row for row in assets if row["name"].endswith(".whl")]
    executing = _executing_wheel()
    if (len(wheels) != 1 or executing is None or
            os.path.basename(executing) != wheels[0]["name"] or
            os.path.getsize(executing) != wheels[0]["size"] or
            _file_sha(executing) != wheels[0]["sha256"]):
        raise lite.LiteError("SELF_HOST_BOOTSTRAP_REFUSED")
    return ("PUBLIC" if operation == "PUBLICATION_POSTFLIGHT" else
            "CANDIDATE")


def publication_authorize(database, project_root, work_item_id, human_id,
                          candidate_id, candidate_fingerprint, authorization_file,
                          request_id):
    authorization_file = _project_file(project_root, authorization_file,
                                       "authorization file")
    authorization = _load_json(authorization_file, AUTHORIZATION_PROTOCOL)
    required = {"protocolVersion", "repository", "version", "tag", "releaseBranch",
                "title", "candidateId", "candidateFingerprint", "assets", "allowedActions",
                "creationDecisionActor", "prepublicationAttestationSha256",
                "formalReview", "readyFingerprint", "buildFingerprint"}
    if (not required.issubset(authorization) or
            set(authorization) != required or
            authorization.get("candidateId") != candidate_id or
            authorization.get("candidateFingerprint") != candidate_fingerprint):
        raise lite.LiteError("publication authorization identity is invalid")
    if sorted(authorization.get("allowedActions", [])) != _ALLOWED_ACTIONS:
        raise lite.LiteError("publication authorization actions are invalid")
    assets = authorization.get("assets")
    if not isinstance(assets, list) or len(assets) != 3 or len({row.get("name") for row in assets if isinstance(row, dict)}) != 3:
        raise lite.LiteError("publication authorization assets are invalid")
    connection = lite.open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = lite._item(connection, work_item_id)
        now = lite._now()
        snapshot = lite._kernel_assert(
            connection, work_item_id, project_root, phase="pre",
            evaluation_time=now,
        )
        built = release_submission_candidate(connection, work_item_id)
        if built is None or built["candidateId"] != candidate_id or built["candidateFingerprint"] != candidate_fingerprint:
            raise lite.LiteError("authorization requires exact FROZEN+BUILT candidate")
        ready_row, ready = _latest_event(connection, work_item_id,
                                         "PUBLICATION_READY")
        formal = authorization.get("formalReview")
        if (not authorization.get("creationDecisionActor") or
                not isinstance(authorization.get("prepublicationAttestationSha256"), str) or
                len(authorization["prepublicationAttestationSha256"]) != 64 or
                not isinstance(formal, dict) or
                formal != {"reviewId": ready.get("reviewId") if ready else None,
                           "reviewEventId": ready.get("reviewEventId") if ready else None,
                           "reviewRequestId": ready.get("reviewRequestId") if ready else None} or
                not ready_row or
                authorization.get("readyFingerprint") != ready.get("readyFingerprint") or
                authorization.get("buildFingerprint") != built.get("buildFingerprint")):
            raise lite.LiteError("publication authorization bindings are invalid")
        unused_manifest, actual_assets = _verify_current_build(project_root, work_item_id, {
            "candidateId": built["candidateId"],
            "candidateFingerprint": built["candidateFingerprint"],
            "buildFingerprint": built["buildFingerprint"],
        })
        built_row, built_payload = _latest_event(connection, work_item_id, "CANDIDATE_BUILT")
        target = built_payload["target"]
        if (authorization["repository"] != built_payload["repository"] or
                authorization["version"] != target["version"] or
                authorization["tag"] != target["tag"] or
                authorization["releaseBranch"] != target["releaseBranch"] or
                authorization["title"] != target["title"] or
                sorted(authorization["assets"], key=lambda row: row["name"]) != actual_assets):
            raise lite.LiteError("publication authorization drifts from exact built target")
        fingerprint = _sha(_json(authorization))
        replay = connection.execute("SELECT * FROM events WHERE request_id=?", (request_id,)).fetchone()
        if replay:
            payload = json.loads(replay["payload_json"])
            if replay["event_type"] != "PUBLICATION_AUTHORIZED" or payload.get("authorizationFingerprint") != fingerprint or replay["actor_id"] != human_id:
                raise lite.LiteError("request_id was already used with different content")
            connection.rollback()
            return _receipt("PUBLICATION_AUTHORIZED", "OK", work_item_id, replay["event_id"], item["row_version"],
                            extra={"candidateFingerprint": candidate_fingerprint,
                                   "authorizationFingerprint": fingerprint})
        payload = {"protocolVersion": AUTHORIZATION_PROTOCOL, "candidateId": candidate_id,
                   "candidateFingerprint": candidate_fingerprint,
                   "authorization": authorization, "authorizationFingerprint": fingerprint,
                   "riskActionAuthorized": True}
        now = lite._now()
        intent = {
            "operation": "PUBLICATION_AUTHORIZE", "workItemId": work_item_id,
            "requestId": request_id, "actorKind": "HUMAN", "actorId": human_id,
            "now": now, "eventType": "PUBLICATION_AUTHORIZED",
            "payload": payload,
        }
        lite._kernel_apply(
            connection, work_item_id, snapshot,
            workflow_kernel.plan_audit_touch(snapshot, intent),
            project_root=project_root, evaluation_time=now,
        )
        _candidate_materialized(connection, "PUBLICATION_AUTHORIZED", request_id)
        connection.commit()
        event = connection.execute("SELECT event_id FROM events WHERE request_id=?", (request_id,)).fetchone()[0]
        row_version = lite._item(connection, work_item_id)["row_version"]
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()
    return _receipt("PUBLICATION_AUTHORIZED", "OK", work_item_id, event, row_version,
                    extra={"candidateFingerprint": candidate_fingerprint,
                           "authorizationFingerprint": fingerprint})


def _postflight_bindings_are_exact(evidence, ready, auth_row, auth):
    required = {"evidenceCore", "evidenceCoreSha256", "formalReview", "ready",
                "authorization", "workspace", "repair", "remoteStatus"}
    if not required.issubset(evidence):
        return False
    core = evidence.get("evidenceCore")
    if (not isinstance(core, dict) or
            evidence.get("evidenceCoreSha256") != _sha(_json(core)) or
            evidence.get("remoteStatus") != "EXACT_ALREADY_PUBLISHED" or
            evidence.get("formalReview") != {
                "reviewId": ready.get("reviewId"),
                "reviewEventId": ready.get("reviewEventId"),
                "reviewRequestId": ready.get("reviewRequestId"),
            } or evidence.get("ready") != {
                "readyFingerprint": ready.get("readyFingerprint"),
                "candidateFingerprint": ready.get("candidateFingerprint"),
            } or evidence.get("authorization") != {
                "requestId": auth_row["request_id"],
                "authorizationFingerprint": auth.get("authorizationFingerprint"),
            }):
        return False
    if any(not isinstance(evidence.get(name), dict) or
           evidence[name].get("status") not in ("PASS", "NONE")
           for name in ("workspace", "repair")):
        return False
    return (core.get("candidateFingerprint") == ready.get("candidateFingerprint") and
            core.get("readyFingerprint") == ready.get("readyFingerprint") and
            core.get("authorizationFingerprint") ==
            auth.get("authorizationFingerprint"))


def publication_status(database, project_root, work_item_id, evidence_file=None):
    connection = lite.open_database(database)
    try:
        item = lite._item(connection, work_item_id)
        ready_row, ready = _latest_event(connection, work_item_id, "PUBLICATION_READY")
        auth_row, auth = _latest_event(connection, work_item_id, "PUBLICATION_AUTHORIZED")
        submit_row, submit = _latest_event(connection, work_item_id, "SUBMIT_IMPLEMENTATION")
        invalid_row, invalid = _latest_event(connection, work_item_id, "PUBLICATION_READY_INVALIDATED")
        material_row = connection.execute(
            "SELECT event_id,event_type FROM events WHERE work_item_id=? AND event_type IN "
            "('CANDIDATE_PREPARED','CANDIDATE_FROZEN','CANDIDATE_BUILT',"
            "'SUBMIT_IMPLEMENTATION','WORK_ITEM_MANAGEMENT_AMENDED','PLAN_DEVIATION') "
            "ORDER BY event_id DESC LIMIT 1", (work_item_id,),
        ).fetchone()
    finally:
        connection.close()
    if (item["state"] == "FINAL_ACCEPTANCE_APPROVED" or not ready_row or
            invalid_row and invalid_row["event_id"] > ready_row["event_id"] or
            material_row and material_row["event_id"] > ready_row["event_id"] or
            not submit_row or submit_row["event_id"] != ready.get("submissionEventId")):
        return {"protocolVersion": "AWB-PUBLICATION-v1", "status": "NOT_READY",
                "reasonCode": "IMPLEMENTATION_REVIEW_REQUIRED", "workItemId": work_item_id,
                "rowVersion": item["row_version"], "nextStep": {"action": "COMPLETE_IMPLEMENTATION_REVIEW", "arguments": {}}}
    if not auth_row:
        return {"protocolVersion": "AWB-PUBLICATION-v1", "status": "WAITING_HUMAN",
                "reasonCode": "EXACT_REMOTE_AUTHORIZATION_REQUIRED", "workItemId": work_item_id,
                "rowVersion": item["row_version"], "nextStep": {"action": "HUMAN_AUTHORIZE_EXACT_PUBLICATION", "arguments": {"readyFingerprint": ready["readyFingerprint"]}}}
    fingerprint = ready.get("candidateFingerprint")
    reviewed = (submit.get("reviewedCandidate") or {}) if submit else {}
    if auth.get("candidateFingerprint") != fingerprint or reviewed.get("candidateFingerprint") != fingerprint:
        return {"protocolVersion": "AWB-PUBLICATION-v1", "status": "REFUSED",
                "reasonCode": "PUBLICATION_IDENTITY_DRIFT", "workItemId": work_item_id,
                "rowVersion": item["row_version"], "nextStep": {"action": "HUMAN_INSPECT_PUBLICATION_IDENTITY", "arguments": {}}}
    try:
        _verify_current_build(project_root, work_item_id, {
            "candidateId": ready["candidateId"],
            "candidateFingerprint": ready["candidateFingerprint"],
            "buildFingerprint": ready["buildFingerprint"],
        })
    except lite.LiteError:
        return {"protocolVersion": "AWB-PUBLICATION-v1", "status": "REFUSED",
                "reasonCode": "PUBLICATION_CANDIDATE_BYTES_DRIFT", "workItemId": work_item_id,
                "rowVersion": item["row_version"], "nextStep": {"action": "HUMAN_INSPECT_PUBLICATION_IDENTITY", "arguments": {}}}
    next_action = "EXECUTE_EXACT_AUTHORIZED_PUBLICATION"
    risk_class = "REMOTE"
    if evidence_file is not None:
        evidence_path = _project_file(project_root, evidence_file,
                                      "postflight evidence file")
        evidence = _load_json(evidence_path, POSTFLIGHT_PROTOCOL)
        required = {"protocolVersion", "repository", "version", "tag", "refs",
                    "release", "assets", "partialState"}
        allowed = required | {"evidenceCore", "evidenceCoreSha256", "formalReview",
                              "ready", "authorization", "workspace", "repair",
                              "remoteStatus"}
        authz = auth.get("authorization", {})
        manifest, current_assets = _verify_current_build(project_root, work_item_id, {
            "candidateId": ready["candidateId"],
            "candidateFingerprint": ready["candidateFingerprint"],
            "buildFingerprint": ready["buildFingerprint"],
        })
        frozen = manifest["frozen"]
        exact = (
            required.issubset(evidence) and set(evidence).issubset(allowed) and
            evidence.get("partialState") == "NONE" and
            evidence.get("repository") == authz.get("repository") and
            evidence.get("version") == authz.get("version") and
            evidence.get("tag") == authz.get("tag") and
            evidence.get("refs") == {"branchOid": frozen["headCommit"],
                                      "tagObject": frozen["tagObject"],
                                      "tagPeel": frozen["tagPeel"]} and
            evidence.get("release", {}).get("immutable") is True and
            evidence.get("release", {}).get("prerelease") is True and
            sorted(evidence.get("assets", []), key=lambda row: row.get("name", "")) ==
            current_assets and
            _postflight_bindings_are_exact(evidence, ready, auth_row, auth)
        )
        if not exact:
            return {"protocolVersion": "AWB-PUBLICATION-v1",
                    "status": "WAITING_HUMAN",
                    "reasonCode": "PUBLICATION_REMOTE_EVIDENCE_MISMATCH",
                    "workItemId": work_item_id, "rowVersion": item["row_version"],
                    "nextStep": {"action": "HUMAN_INSPECT_PARTIAL_PUBLICATION",
                                 "arguments": {}}}
        next_action = "RUN_POST_PUBLICATION_VERIFY"
        risk_class = "LOCAL_SAFE"
    return {"protocolVersion": "AWB-PUBLICATION-v1", "status": "READY",
            "reasonCode": None, "workItemId": work_item_id, "rowVersion": item["row_version"],
            "nextStep": {"action": next_action,
                         "riskClass": risk_class, "arguments": {
                             "readyFingerprint": ready["readyFingerprint"],
                             "authorizationRequestId": auth_row["request_id"],
                             "candidateFingerprint": fingerprint}}}


def _public_postflight_context(connection, project_root, work_item_id,
                               ready_fingerprint, authorization_request_id,
                               evidence):
    item = lite._item(connection, work_item_id)
    ready_row, ready = _latest_event(connection, work_item_id, "PUBLICATION_READY")
    auth_row = connection.execute(
        "SELECT * FROM events WHERE request_id=?", (authorization_request_id,)
    ).fetchone()
    if (not ready_row or ready.get("readyFingerprint") != ready_fingerprint or
            not auth_row or auth_row["event_type"] != "PUBLICATION_AUTHORIZED"):
        raise lite.LiteError("PUBLIC_POSTFLIGHT_REFUSED")
    auth = json.loads(auth_row["payload_json"])
    if not _postflight_bindings_are_exact(evidence, ready, auth_row, auth):
        raise lite.LiteError("PUBLIC_POSTFLIGHT_REFUSED")
    expected = auth["authorization"]
    manifest, current_assets = _verify_current_build(project_root, work_item_id, {
        "candidateId": ready["candidateId"],
        "candidateFingerprint": ready["candidateFingerprint"],
        "buildFingerprint": ready["buildFingerprint"],
    })
    frozen = manifest["frozen"]
    assets = evidence.get("assets")
    if (evidence["partialState"] != "NONE" or
            evidence.get("remoteStatus") != "EXACT_ALREADY_PUBLISHED" or
            evidence["repository"] != expected["repository"] or
            evidence["version"] != expected["version"] or
            evidence["tag"] != expected["tag"] or
            evidence.get("release", {}).get("immutable") is not True or
            evidence.get("release", {}).get("draft") is not False or
            evidence.get("release", {}).get("prerelease") is not True or
            evidence.get("release", {}).get("version") != expected["version"] or
            evidence.get("release", {}).get("tag") != expected["tag"] or
            evidence.get("release", {}).get("title") != expected["title"] or
            evidence.get("refs") != {"branchOid": frozen["headCommit"],
                                     "tagObject": frozen["tagObject"],
                                     "tagPeel": frozen["tagPeel"]} or
            sorted(assets or [], key=lambda row: row["name"]) != current_assets or
            current_assets != sorted(expected["assets"],
                                     key=lambda row: row["name"])):
        raise lite.LiteError("PUBLIC_POSTFLIGHT_REFUSED")
    return item, ready_row, ready, auth_row, auth, manifest, current_assets


def publication_postflight(database, project_root, work_item_id, operator,
                           ready_fingerprint, authorization_request_id,
                           remote_facts, request_id):
    if not isinstance(remote_facts, dict):
        raise lite.LiteError("publication postflight requires structured remote facts")
    evidence = json.loads(_json(remote_facts))
    if evidence.get("protocolVersion") != POSTFLIGHT_PROTOCOL:
        raise lite.LiteError("publication postflight remote facts protocol is invalid")
    required = {"protocolVersion", "repository", "version", "tag", "refs",
                "release", "assets", "partialState", "evidenceCore",
                "evidenceCoreSha256", "formalReview", "ready", "authorization",
                "workspace", "repair", "remoteStatus"}
    allowed = required
    if (not required.issubset(evidence) or not set(evidence).issubset(allowed) or
            evidence.get("partialState") not in ("NONE", "PRESENT", "UNKNOWN")):
        raise lite.LiteError("publication postflight evidence is invalid")
    if "evidenceCore" in evidence and evidence.get("evidenceCoreSha256") != _sha(
            _json(evidence["evidenceCore"])):
        raise lite.LiteError("publication postflight evidence core drifted")
    evidence_fingerprint = _sha(_json(evidence))
    request_fingerprint = _sha(_json({
        "operator": operator, "readyFingerprint": ready_fingerprint,
        "authorizationRequestId": authorization_request_id,
        "evidenceFingerprint": evidence_fingerprint,
    }))
    manifest_path = None
    original_manifest = None
    finalized_manifest = None
    commit_attempted = False
    outcome_candidate_id = None
    outcome_candidate_fingerprint = None
    connection = lite.open_database(database)
    try:
        unused_ready_row, guarded_ready = _latest_event(
            connection, work_item_id, "PUBLICATION_READY"
        )
        lane = assert_self_host_operation(
            connection, project_root, work_item_id,
            "PUBLICATION_POSTFLIGHT",
            guarded_ready.get("candidateId") if guarded_ready else None,
        )
        if lane == "PUBLIC":
            _public_postflight_context(
                connection, project_root, work_item_id, ready_fingerprint,
                authorization_request_id, evidence,
            )
        replay = connection.execute(
            "SELECT * FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if replay is not None:
            payload = json.loads(replay["payload_json"])
            receipt = payload.get("resultReceipt")
            if (replay["event_type"] != "PUBLICATION_POSTFLIGHT_ACCEPTED" or
                    replay["actor_kind"] != "AGENT" or
                    replay["actor_id"] != operator or
                    payload.get("requestFingerprint") != request_fingerprint or
                    payload.get("readyFingerprint") != ready_fingerprint or
                    payload.get("authorizationRequestId") != authorization_request_id or
                    payload.get("evidenceFingerprint") != evidence_fingerprint or
                    not isinstance(receipt, dict) or
                    receipt.get("eventId") != replay["event_id"]):
                raise lite.LiteError("request_id was already used with different content")
            return receipt
        connection.execute("BEGIN IMMEDIATE")
        item, ready_row, ready, auth_row, auth, manifest, current_assets = \
            _public_postflight_context(
                connection, project_root, work_item_id, ready_fingerprint,
                authorization_request_id, evidence,
            )
        now = lite._now()
        snapshot = lite._kernel_assert(
            connection, work_item_id, project_root, phase="pre",
            evaluation_time=now,
        )
        expected = auth["authorization"]
        unused_root, unused_managed, manifest_path = _candidate_manifest(
            project_root, work_item_id, ready["candidateId"]
        )
        original_manifest = dict(manifest)
        outcome_candidate_id = ready["candidateId"]
        outcome_candidate_fingerprint = ready["candidateFingerprint"]
        finalized_manifest = dict(manifest)
        finalized_manifest.update({"lifecycle": "FINALIZED", "bytesRetained": True,
                                   "publicationPostflightRequestId": request_id})
        _replace_json(manifest_path, finalized_manifest,
                      "postflight-" + _sha(request_id)[:16])
        verify_request_id = request_id + "-verify-extension"
        post_checks = [{
            "checkId": "publication-remote-facts", "phase": "POST_PUBLICATION",
            "result": "PASS", "resultDigest": evidence_fingerprint,
            "covers": ["HC-4", "HC-5", "VP-2"],
        }]
        extended_receipt = verify.extend_current_receipt(
            connection, project_root, work_item_id, "POST_PUBLICATION",
            verify_request_id, post_checks, now,
        )
        event = connection.execute(
            "SELECT coalesce(max(event_id),0)+1 FROM events"
        ).fetchone()[0]
        payload = {"protocolVersion": POSTFLIGHT_PROTOCOL, "readyFingerprint": ready_fingerprint,
                   "authorizationRequestId": authorization_request_id,
                   "candidateId": ready["candidateId"],
                   "candidateFingerprint": ready["candidateFingerprint"],
                   "operatorAgentId": operator,
                   "requestFingerprint": request_fingerprint,
                   "evidenceFingerprint": evidence_fingerprint, "evidence": evidence,
                   "resultRowVersion": item["row_version"] + 1}
        payload["resultReceipt"] = _receipt(
            "PUBLICATION_POSTFLIGHT_ACCEPTED", "OK", work_item_id, event,
            payload["resultRowVersion"], extra={
                "candidateId": ready["candidateId"],
                "candidateFingerprint": ready["candidateFingerprint"],
            }
        )
        events = [{"requestId": request_id, "eventId": event,
                   "eventType": "PUBLICATION_POSTFLIGHT_ACCEPTED",
                   "payload": payload},
                  {"requestId": verify_request_id,
                   "eventId": event + 1,
                   "eventType": "VERIFY_RECEIPT_EXTENDED",
                   "payload": {
                       "receipt": extended_receipt,
                       "requestFingerprint": _sha(_json({
                           "workItemId": work_item_id,
                           "candidateFingerprint": ready["candidateFingerprint"],
                           "phase": "POST_PUBLICATION",
                           "evidenceFingerprint": evidence_fingerprint,
                       })),
                       "resultReceipt": {
                           "protocolVersion": verify.RECEIPT_PROTOCOL,
                           "operation": "RUN", "phase": "POST_PUBLICATION",
                           "status": "PASS", "workItemId": work_item_id,
                           "receipt": extended_receipt,
                       },
                   }},
                  {"requestId": request_id + "-candidate-finalized",
                   "eventId": event + 2, "eventType": "CANDIDATE_FINALIZED",
                   "actorKind": "SYSTEM", "actorId": "publication-gate",
                   "payload": {
                       "protocolVersion": CANDIDATE_PROTOCOL,
                       "candidateId": ready["candidateId"],
                       "candidateFingerprint": ready["candidateFingerprint"],
                       "ownerAgentId": manifest["ownerAgentId"],
                       "lifecycle": "FINALIZED", "bytesRetained": True,
                   }}]
        auto_events = []
        auto_enabled = item["human_gate_policy"] == "AUTO_ON_PASS"
        if auto_enabled:
            reviewer_claim = lite._validated_final_reviewer_claim(connection, work_item_id)
            lite._approved_gate_preconditions(
                connection, item, "FINAL", pending_publication_postflight=True
            )
            resources = lite._terminal_resource_projection(snapshot, reviewer_claim)
            auto_events.append({
                "requestId": request_id + "-terminal-activity",
                "eventId": event + 3,
                "eventType": "TERMINAL_ACTIVITY_RECONCILED",
                "actorKind": "SYSTEM", "actorId": "terminal-reconciler",
                "payload": {"triggerRequestId": request_id + "-terminal-activity",
                            "resources": resources},
            })
            review_row = connection.execute("SELECT * FROM reviews WHERE work_item_id=? AND stage='FINAL' ORDER BY rowid DESC LIMIT 1", (work_item_id,)).fetchone()
            auto = {"policy": "AUTO_ON_PASS", "stage": "FINAL",
                    "reviewId": review_row["review_id"], "reviewRound": lite._decoded_review(review_row)["round"],
                    "reviewRequestId": ready["reviewRequestId"], "reviewEventId": ready["reviewEventId"],
                    "idempotencyRequestId": request_id, "riskActionAuthorized": False}
            auto.update(lite._auto_gate_context(connection, work_item_id))
            auto_events.append({
                "requestId": "auto-gate-" + _sha(request_id + ":FINAL"),
                "eventId": event + 4, "eventType": "AUTO_GATE_APPROVED",
                "actorKind": "SYSTEM", "actorId": "auto-gate",
                "payload": auto,
            })
        intent = {
            "operation": "PUBLICATION_POSTFLIGHT", "workItemId": work_item_id,
            "requestId": request_id, "actorKind": "AGENT", "actorId": operator,
            "now": now, "auto": auto_enabled,
            "baseEvents": events, "autoEvents": auto_events,
        }
        lite._kernel_apply(
            connection, work_item_id, snapshot,
            workflow_kernel.plan_publication_postflight(snapshot, intent),
            project_root=project_root, evaluation_time=now,
        )
        _candidate_materialized(connection, "PUBLICATION_POSTFLIGHT", request_id)
        commit_attempted = True
        connection.commit()
        row = lite._item(connection, work_item_id)
    except Exception:
        connection.rollback()
        outcome = (_resolve_commit_outcome(
            database, work_item_id, request_id,
            "PUBLICATION_POSTFLIGHT_ACCEPTED",
            {"candidateId": outcome_candidate_id,
             "candidateFingerprint": outcome_candidate_fingerprint,
             "operatorAgentId": operator,
             "requestFingerprint": request_fingerprint,
             "readyFingerprint": ready_fingerprint,
             "authorizationRequestId": authorization_request_id,
             "evidenceFingerprint": evidence_fingerprint},
            outcome_candidate_id, postflight=True,
        ) if commit_attempted else None)
        if outcome is not None:
            return outcome
        if (manifest_path and original_manifest is not None and
                finalized_manifest is not None):
            try:
                current = _load_json(manifest_path, CANDIDATE_PROTOCOL)
                if current == finalized_manifest:
                    _replace_json(manifest_path, original_manifest,
                                  "postflight-compensate-" + _sha(request_id)[:16])
                elif current != original_manifest:
                    raise lite.LiteError("publication postflight compensation found manifest drift")
            except Exception as compensation:
                raise lite.LiteError("publication postflight recovery required: {0}".format(compensation))
        raise
    finally:
        connection.close()
    if row["row_version"] != payload["resultRowVersion"]:
        raise lite.LiteError("publication postflight rowVersion drifted after commit")
    return payload["resultReceipt"]


def retry_review_route(history):
    """Return the sole fresh ordinary round, or None without mutating history."""
    reviews = [entry[1] if isinstance(entry, tuple) else entry for entry in history]
    if len(reviews) not in (1, 2):
        return None
    for index, review in enumerate(reviews, 1):
        if (not isinstance(review, dict) or review.get("round") != index or
                review.get("reviewerMode") != "ORDINARY"):
            return None
    if reviews[-1].get("result") != "PASS":
        return None
    return {"expectedNextReviewRound": len(reviews) + 1,
            "expectedNextReviewerMode": "ORDINARY"}


def publication_retry(database, project_root, work_item_id, human_id,
                      ready_fingerprint, retry_file, request_id):
    retry_file = _project_file(project_root, retry_file, "publication retry file")
    retry = _load_json(retry_file, RETRY_PROTOCOL)
    required = {
        "protocolVersion", "candidateId", "candidateFingerprint",
        "readyFingerprint", "authorizationFingerprint", "reason",
        "remoteActionStarted", "partialState",
    }
    if (set(retry) != required or
            retry.get("readyFingerprint") != ready_fingerprint or
            not isinstance(retry.get("reason"), str) or not retry["reason"].strip() or
            retry.get("remoteActionStarted") is not False or retry.get("partialState") != "NONE"):
        raise lite.LiteError("publication retry is not a proven no-remote-action retry")
    request_fingerprint = _sha(_json({
        "workItemId": work_item_id, "humanId": human_id, "retry": retry,
    }))
    connection = lite.open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = lite._item(connection, work_item_id)
        replay = connection.execute(
            "SELECT * FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if replay is not None:
            payload = json.loads(replay["payload_json"])
            if (replay["event_type"] != "PUBLICATION_READY_INVALIDATED" or
                    replay["actor_kind"] != "HUMAN" or replay["actor_id"] != human_id or
                    payload.get("requestFingerprint") != request_fingerprint):
                raise lite.LiteError("request_id was already used with different content")
            connection.rollback()
            return _receipt(
                "PUBLICATION_RETRY", "OK", work_item_id, replay["event_id"],
                item["row_version"],
                next_step={"action": "BEGIN_IMPLEMENTATION", "arguments": {}},
                extra={"expectedNextReviewRound": payload["expectedNextReviewRound"],
                       "expectedNextReviewerMode": "ORDINARY"},
            )
        now = lite._now()
        snapshot = lite._kernel_assert(
            connection, work_item_id, project_root, phase="pre",
            evaluation_time=now,
        )
        ready_row, ready = _latest_event(connection, work_item_id, "PUBLICATION_READY")
        auth_row, auth = _latest_event(connection, work_item_id, "PUBLICATION_AUTHORIZED")
        invalid_row, unused_invalid = _latest_event(
            connection, work_item_id, "PUBLICATION_READY_INVALIDATED"
        )
        accepted_row, unused_accepted = _latest_event(
            connection, work_item_id, "PUBLICATION_POSTFLIGHT_ACCEPTED"
        )
        if (not ready_row or ready.get("readyFingerprint") != ready_fingerprint or
                item["state"] != "IMPLEMENTATION_COMPLETED" or
                item["queue_state"] != "WAITING_HUMAN" or
                invalid_row and invalid_row["event_id"] > ready_row["event_id"] or
                accepted_row and accepted_row["event_id"] > ready_row["event_id"]):
            raise lite.LiteError("publication retry ready identity is stale")
        if (not auth_row or
                auth.get("candidateFingerprint") != ready.get("candidateFingerprint") or
                auth.get("authorizationFingerprint") != retry["authorizationFingerprint"] or
                retry["candidateId"] != ready.get("candidateId") or
                retry["candidateFingerprint"] != ready.get("candidateFingerprint")):
            raise lite.LiteError("publication retry authorization identity is stale")
        review_event = connection.execute(
            "SELECT * FROM events WHERE event_id=? AND work_item_id=? "
            "AND event_type='AGENT_FINAL_REVIEW'",
            (ready.get("reviewEventId"), work_item_id),
        ).fetchone()
        if review_event is None:
            raise lite.LiteError("publication retry review identity is stale")
        review_payload = json.loads(review_event["payload_json"])
        if (review_payload.get("review", {}).get("result") != "PASS" or
                review_payload.get("review", {}).get("round") != ready.get("reviewRound") or
                review_payload.get("review", {}).get("reviewerMode") != "ORDINARY"):
            raise lite.LiteError("publication retry review identity is stale")
        plan_projection = lite._review_stage_projection(
            lite._review_history(connection, work_item_id, "PLAN")
        )
        plan_gate = connection.execute(
            "SELECT event_id FROM events WHERE work_item_id=? AND event_type IN "
            "('AUTO_GATE_APPROVED','HUMAN_PLAN_GATE') ORDER BY event_id DESC LIMIT 1",
            (work_item_id,),
        ).fetchone()
        if (plan_projection["latestResult"] != "PASS" or
                plan_projection["openFindings"] or plan_gate is None):
            raise lite.LiteError("publication retry PLAN history is not approved")
        history = lite._review_history(connection, work_item_id, "FINAL")
        route = retry_review_route(history)
        if route is None:
            connection.rollback()
            return _receipt("PUBLICATION_RETRY", "WAITING_HUMAN", work_item_id,
                            row_version=item["row_version"],
                            reason_code="IMPLEMENTATION_REVIEW_BUDGET_NO_FRESH_ORDINARY",
                            next_step={"action": "HUMAN_AMEND_MANAGEMENT_AND_CREATE_SUCCESSOR_WORKITEM", "arguments": {}})
        expected_round = route["expectedNextReviewRound"]
        tasks = connection.execute(
            "SELECT * FROM tasks WHERE work_item_id=? AND required=1 "
            "AND owner_role IN ('IMPLEMENTER','REVIEWER') ORDER BY seq",
            (work_item_id,),
        ).fetchall()
        if (len(tasks) != 2 or {row["owner_role"] for row in tasks} !=
                {"IMPLEMENTER", "REVIEWER"} or
                any(row["status"] != "COMPLETED" for row in tasks)):
            raise lite.LiteError("publication retry task state drifted")
        payload = {"protocolVersion": RETRY_PROTOCOL, "candidateId": ready["candidateId"],
                   "candidateFingerprint": ready["candidateFingerprint"],
                   "readyFingerprint": ready_fingerprint, "reason": retry.get("reason"),
                   "authorizationEventId": auth_row["event_id"],
                   "authorizationFingerprint": auth["authorizationFingerprint"],
                   "priorReviewRound": len(history), "expectedNextReviewRound": expected_round,
                   "expectedNextReviewerMode": "ORDINARY", "reviewRoundsPreserved": True,
                   "requestFingerprint": request_fingerprint,
                   "taskReset": [{"taskId": row["task_id"], "from": "COMPLETED",
                                  "to": "NOT_STARTED"} for row in tasks]}
        intent = {
            "operation": "PUBLICATION_RETRY", "workItemId": work_item_id,
            "requestId": request_id, "actorKind": "HUMAN", "actorId": human_id,
            "now": now, "payload": payload,
            "tasks": [{"taskId": row["task_id"],
                       "ownerRole": row["owner_role"]} for row in tasks],
        }
        lite._kernel_apply(
            connection, work_item_id, snapshot,
            workflow_kernel.plan_publication_retry(snapshot, intent),
            project_root=project_root, evaluation_time=now,
        )
        _candidate_materialized(connection, "PUBLICATION_RETRY", request_id)
        connection.commit()
        event = connection.execute("SELECT event_id FROM events WHERE request_id=?", (request_id,)).fetchone()[0]
        row_version = lite._item(connection, work_item_id)["row_version"]
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()
    return _receipt("PUBLICATION_RETRY", "OK", work_item_id, event, row_version,
                    next_step={"action": "BEGIN_IMPLEMENTATION", "arguments": {}},
                    extra={"expectedNextReviewRound": expected_round,
                           "expectedNextReviewerMode": "ORDINARY"})
