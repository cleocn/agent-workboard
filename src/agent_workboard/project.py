"""Project lifecycle helpers for the standalone, local-only RC.

This module intentionally has no network or third-party dependency.  It does
not publish, configure remotes, or alter an existing project's AWB setup.
"""

import ast
import base64
import csv
import datetime
import hashlib
import json
import os
import pkgutil
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from urllib.parse import quote, unquote, urlparse

from . import __version__
from ._build import BUILD_IDENTITY
from .lite import LiteError, SCHEMA_VERSION, initialize_database, open_database
from .usage import USAGE_SCHEMA_VERSION, schema_installed, usage_schema_sql


CONFIG_VERSION = 1
MANAGED = ("config.json", "project.md", ".gitignore", "requirements-awb.txt")
TABLES = ("work_items", "tasks", "claims", "repository_locks", "reviews",
          "human_gates", "events")
USAGE_TABLES = ("usage_events",)
UPGRADE_PROTOCOL = "AWB-UPGRADE-v1"
ROLLBACK_PROTOCOL = "AWB-ROLLBACK-v1"
RELEASE_0_2_1_IDENTITY = {
    "packageVersion": "0.2.1",
    "sourceCommit": "c7d210db9cb4fb59d8e263277c74d0c040b40d7c",
    "sourceTree": "d24b1e77adff3ed01d26a6d99e7ea11d51263588",
    "sourceTag": "v0.2.1",
}
SUPPORTED_UPGRADE_SOURCES = (RELEASE_0_2_1_IDENTITY,)
IDENTITY_KEYS = ("packageVersion", "sourceCommit", "sourceTree", "sourceTag")


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


def _file_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def _resource(name):
    data = pkgutil.get_data("agent_workboard", "resources/" + name)
    if data is None:
        raise LiteError("required package resource is missing: {0}".format(name))
    return data


def _project_root(path):
    return os.path.realpath(os.path.abspath(path))


def _direct_wheel():
    """Return the local wheel recorded by pip's direct-url installation data.

    Older pip versions (including the supported Python 3.7 one) omit
    ``archive_info.hash`` for a local wheel.  The local artifact is still a
    trustworthy binding input only after this module re-hashes it and checks
    that it is the wheel that produced the currently loaded package.
    """
    try:
        try:
            from importlib import metadata
            direct = metadata.distribution("agent-workboard").read_text("direct_url.json")
        except ImportError:
            import pkg_resources
            direct = open(os.path.join(pkg_resources.get_distribution("agent-workboard").egg_info,
                                       "direct_url.json"), encoding="utf-8").read()
    except Exception:
        return None
    if not direct:
        return None
    try:
        data = json.loads(direct)
        parsed = urlparse(data["url"])
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            return False
        declared = data.get("archive_info", {}).get("hash")
        if declared:
            algorithm, separator, digest = declared.partition("=")
            if algorithm != "sha256" or separator != "=" or len(digest) != 64:
                return False
        else:
            digest = None
        return unquote(parsed.path), digest
    except Exception:
        return False


def _wheel_identity(path):
    try:
        with zipfile.ZipFile(path) as archive:
            statement = archive.read("agent_workboard/_build.py").decode("utf-8").split("=", 1)[1].strip()
        return ast.literal_eval(statement)
    except Exception:
        return None


def _requirement_lock(wheel_path, digest):
    # pip 18.1 (the Python 3.7 bootstrap pip) does not support PEP 508's
    # ``name @ file://`` form in requirements files.  Its URL+egg form is
    # also accepted by current pip and still binds the exact local artifact.
    return "--require-hashes\nfile://{0}#egg=agent-workboard --hash=sha256:{1}\n".format(quote(wheel_path), digest)


def _empty_package_cache_record(parts, encoded_hash, size):
    """Return whether a validated package-owned RECORD row is generated cache."""
    return (not encoded_hash and not size and len(parts) >= 3 and
            parts[0] == "agent_workboard" and parts[-2] == "__pycache__" and
            parts[-1].endswith((".pyc", ".pyo")))


def _installed_wheel_rebuild():
    """Build a deterministic wheel from a verified non-editable installation.

    pip 18.1 on Python 3.7 does not create direct_url.json for a local wheel.
    This deliberately narrow fallback is only for that case: source files are
    copied from the loaded installed package, environment-generated metadata
    is excluded, and RECORD is regenerated from the bytes written.
    """
    import agent_workboard
    if _is_editable() or not BUILD_IDENTITY["sourceCommit"] or not BUILD_IDENTITY["sourceTree"]:
        raise LiteError("init cannot rebuild an editable or unverified installed package; pass --wheel or set AWB_WHEEL")
    package = os.path.realpath(os.path.dirname(agent_workboard.__file__))
    installation = os.path.realpath(os.path.dirname(package))
    if os.path.islink(package) or os.path.commonpath((installation, package)) != installation:
        raise LiteError("init cannot rebuild an unsafe installed package; pass --wheel or set AWB_WHEEL")
    prefix = "agent_workboard-{0}.dist-info".format(__version__)
    metadata = os.path.join(installation, prefix)
    if not os.path.isdir(metadata) or os.path.islink(metadata):
        raise LiteError("init cannot locate installed package metadata; pass --wheel or set AWB_WHEEL")
    try:
        with open(os.path.join(metadata, "METADATA"), encoding="utf-8") as handle:
            headers = handle.read().split("\n\n", 1)[0].splitlines()
        values = dict(line.split(": ", 1) for line in headers if ": " in line)
    except (IOError, ValueError):
        raise LiteError("init cannot validate installed package metadata; pass --wheel or set AWB_WHEEL")
    if values.get("Name", "").lower().replace("_", "-") != "agent-workboard" or values.get("Version") != __version__:
        raise LiteError("init cannot validate installed package name/version; pass --wheel or set AWB_WHEEL")
    record_path = os.path.join(metadata, "RECORD")
    try:
        with open(record_path, newline="", encoding="utf-8") as handle:
            record_rows = list(csv.reader(handle))
    except (IOError, csv.Error):
        raise LiteError("init cannot validate installed package RECORD; pass --wheel or set AWB_WHEEL")
    approved = {}
    record_name = prefix + "/RECORD"
    ignored_metadata = set((prefix + "/INSTALLER", prefix + "/REQUESTED", prefix + "/direct_url.json"))
    for row in record_rows:
        if len(row) != 3:
            raise LiteError("init rejects malformed installed package RECORD; pass --wheel or set AWB_WHEEL")
        name, encoded_hash, size = row
        # pip records the generated console script outside site-packages.
        # It is not package input and is never copied into a rebuilt wheel.
        if name == "../../../bin/awb":
            continue
        parts = name.split("/")
        if not name or "\\" in name or os.path.isabs(name) or any(part in ("", ".", "..") for part in parts):
            raise LiteError("init rejects unsafe installed package RECORD path; pass --wheel or set AWB_WHEEL")
        if not (name.startswith("agent_workboard/") or name.startswith(prefix + "/")) or name in approved:
            raise LiteError("init rejects unowned or duplicate installed package RECORD path; pass --wheel or set AWB_WHEEL")
        # pip 18 records import-generated bytecode with empty hashes.  Ignore
        # only package-owned cache rows, after path and ownership validation.
        if _empty_package_cache_record(parts, encoded_hash, size):
            approved[name] = None
            continue
        if name == record_name:
            if encoded_hash or size:
                raise LiteError("init rejects nonstandard installed package RECORD entry; pass --wheel or set AWB_WHEEL")
            approved[name] = None
            continue
        if name in ignored_metadata:
            # pip's environment facts must be absent from the rebuilt wheel.
            approved[name] = None
            continue
        if not encoded_hash.startswith("sha256=") or not size.isdecimal():
            raise LiteError("init rejects incomplete installed package RECORD entry; pass --wheel or set AWB_WHEEL")
        encoded_digest = encoded_hash[len("sha256="):]
        if len(encoded_digest) != 43 or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for character in encoded_digest):
            raise LiteError("init rejects malformed installed package RECORD hash; pass --wheel or set AWB_WHEEL")
        try:
            expected = base64.urlsafe_b64decode(encoded_digest + "=")
        except Exception:
            raise LiteError("init rejects malformed installed package RECORD hash; pass --wheel or set AWB_WHEEL")
        if len(expected) != 32 or base64.urlsafe_b64encode(expected).decode("ascii").rstrip("=") != encoded_digest:
            raise LiteError("init rejects unsupported installed package RECORD hash; pass --wheel or set AWB_WHEEL")
        source = os.path.realpath(os.path.join(installation, *parts))
        if os.path.commonpath((installation, source)) != installation or os.path.islink(source) or not os.path.isfile(source):
            raise LiteError("init rejects unsafe installed package RECORD file; pass --wheel or set AWB_WHEEL")
        with open(source, "rb") as handle:
            raw = handle.read()
        if hashlib.sha256(raw).digest() != expected or len(raw) != int(size):
            raise LiteError("init rejects modified installed package RECORD file; pass --wheel or set AWB_WHEEL")
        approved[name] = source
    if record_name not in approved:
        raise LiteError("init rejects installed package without RECORD; pass --wheel or set AWB_WHEEL")
    actual = set()
    for directory, prefix_name, ignore in ((package, "agent_workboard", set()), (metadata, prefix, ignored_metadata)):
        for base, directories, names in os.walk(directory):
            if any(os.path.islink(os.path.join(base, name)) for name in directories + names):
                raise LiteError("init rejects installed package symbolic links; pass --wheel or set AWB_WHEEL")
            directories[:] = sorted(name for name in directories if name != "__pycache__")
            for name in names:
                if name.endswith((".pyc", ".pyo")) or name == "RECORD" or prefix_name + "/" + os.path.relpath(os.path.join(base, name), directory).replace(os.sep, "/") in ignore:
                    continue
                actual.add(prefix_name + "/" + os.path.relpath(os.path.join(base, name), directory).replace(os.sep, "/"))
    allowed = set(name for name, source in approved.items() if source is not None)
    if actual != allowed:
        raise LiteError("init rejects installed package files outside RECORD; pass --wheel or set AWB_WHEEL")
    files = sorted((name, source) for name, source in approved.items() if source is not None)
    if not files or prefix + "/WHEEL" not in approved:
        raise LiteError("init cannot rebuild incomplete installed package; pass --wheel or set AWB_WHEEL")
    temporary = tempfile.mkdtemp(prefix="awb-installed-wheel-")
    artifact = os.path.join(temporary, "agent_workboard-{0}-py3-none-any.whl".format(__version__))
    try:
        records = []
        with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for archive_name, source in sorted(files):
                with open(source, "rb") as handle:
                    raw = handle.read()
                info = zipfile.ZipInfo(archive_name, (2000, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, raw)
                records.append("{0},sha256={1},{2}".format(
                    archive_name, base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("="), len(raw)))
            record_name = prefix + "/RECORD"
            record = "\n".join(sorted(records) + [record_name + ",,"]) + "\n"
            info = zipfile.ZipInfo(record_name, (2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, record.encode("utf-8"))
        if _wheel_identity(artifact) != BUILD_IDENTITY:
            raise LiteError("init rebuilt wheel identity mismatch; pass --wheel or set AWB_WHEEL")
        return temporary, artifact, _file_sha(artifact)
    except Exception:
        shutil.rmtree(temporary)
        raise


def _wheel_binding(wheel_path=None):
    explicit = wheel_path or os.environ.get("AWB_WHEEL")
    direct = None if explicit else _direct_wheel()
    if direct is False:
        raise LiteError("init cannot trust installed direct-url metadata; pass --wheel or set AWB_WHEEL")
    if direct:
        wheel_path, expected = direct
    else:
        wheel_path = explicit or os.path.join(os.getcwd(), "dist", "agent_workboard-{0}-py3-none-any.whl".format(__version__))
        expected = None
    wheel_path = os.path.realpath(os.path.abspath(wheel_path))
    if not os.path.isfile(wheel_path):
        if not explicit and direct is None:
            temporary, artifact, digest = _installed_wheel_rebuild()
            return {"requirements": None, "temporary": temporary, "artifact": artifact, "sha256": digest}
        raise LiteError("init cannot discover a verified installed wheel; pass --wheel or set AWB_WHEEL")
    actual = _file_sha(wheel_path)
    if expected and actual != expected:
        raise LiteError("installed wheel metadata hash does not match its artifact")
    basename = os.path.basename(wheel_path)
    expected_prefix = "agent_workboard-{0}-".format(__version__)
    if not explicit and (not basename.startswith(expected_prefix) or not basename.endswith(".whl")):
        raise LiteError("init cannot verify the installed wheel name/version; pass --wheel or set AWB_WHEEL")
    if not explicit and _wheel_identity(wheel_path) != BUILD_IDENTITY:
        raise LiteError("init cannot verify the current wheel; pass --wheel or set AWB_WHEEL")
    return {"requirements": _requirement_lock(wheel_path, actual), "temporary": None}


def _awb(root):
    return os.path.join(root, ".awb")


def _config_path(root):
    return os.path.join(_awb(root), "config.json")


def _load_config(root):
    try:
        with open(_config_path(root), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (IOError, ValueError) as exc:
        raise LiteError("invalid or missing .awb/config.json: {0}".format(exc))
    required = ("configVersion", "projectId", "repositoryKey", "database",
                "runtimeMode", "requiredPackageVersion", "requiredSourceCommit",
                "requiredSourceTree", "requiredSourceTag")
    if any(key not in data for key in required) or data["configVersion"] != CONFIG_VERSION:
        raise LiteError("AWB configuration is incomplete or unsupported")
    if data["runtimeMode"] not in ("stable", "development"):
        raise LiteError("AWB runtimeMode is invalid")
    database = os.path.realpath(os.path.join(root, data["database"]))
    if os.path.commonpath((root, database)) != root:
        raise LiteError("AWB database escapes project root")
    return data, database


def _validate_project_contract(root):
    requirements = os.path.join(_awb(root), "requirements-awb.txt")
    try:
        with open(requirements, "r", encoding="utf-8") as handle:
            locked = handle.read()
    except IOError:
        raise LiteError("required .awb/requirements-awb.txt is missing")
    lines = [line.strip() for line in locked.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    package_lines = [line for line in lines if (line.startswith("file://") or line.startswith("https://")) and "#egg=agent-workboard " in line]
    if "--require-hashes" not in lines or len(package_lines) != 1 or "--hash=sha256:" not in package_lines[0]:
        raise LiteError("requirements-awb.txt is not a bound hash lock")
    package, digest = package_lines[0].rsplit("--hash=sha256:", 1)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise LiteError("requirements-awb.txt has an invalid wheel hash")
    wheel_url = package.split("#egg=agent-workboard", 1)[0].strip()
    parsed = urlparse(wheel_url)
    if parsed.scheme == "file":
        wheel_path = unquote(parsed.path)
        if not os.path.isfile(wheel_path):
            raise LiteError("requirements-awb.txt wheel is unavailable")
        actual = _file_sha(wheel_path)
        if actual != digest:
            raise LiteError("requirements-awb.txt wheel hash does not match")
    elif (parsed.scheme != "https" or parsed.netloc != "github.com" or
          not any(parsed.path.startswith("/cleocn/agent-workboard/releases/download/{0}/".format(tag))
                  for tag in ("v0.1.0", "v0.2.0", "v0.2.1", "v0.3.0b1"))):
        raise LiteError("requirements-awb.txt is not an approved release wheel URL")
    try:
        with open(os.path.join(_awb(root), "project.md"), "r", encoding="utf-8") as handle:
            project = handle.read()
    except IOError:
        raise LiteError("required .awb/project.md is missing")
    headings = ("项目身份", "WorkItem 路由表", "权威库/文档位置", "角色映射", "默认授权与禁止的远程动作")
    for heading in headings:
        marker = "## " + heading
        start = project.find(marker)
        if start < 0:
            raise LiteError("project.md is missing required section: " + heading)
        following = project.find("\n## ", start + len(marker))
        if not project[start + len(marker):following if following >= 0 else len(project)].strip():
            raise LiteError("project.md has empty required section: " + heading)


def _is_editable():
    def loaded_outside(location):
        import agent_workboard
        try:
            return os.path.commonpath((os.path.realpath(location),
                                       os.path.realpath(agent_workboard.__file__))) != os.path.realpath(location)
        except ValueError:
            return True
    try:
        import importlib_metadata
    except ImportError:
        try:
            from importlib import metadata as importlib_metadata
        except ImportError:
            try:
                import pkg_resources
                dist = pkg_resources.get_distribution("agent-workboard")
                if str(getattr(dist, "egg_info", "")).endswith(".egg-info"):
                    return True
                for entry in list(__import__("sys").path):
                    if os.path.isfile(os.path.join(entry, "agent-workboard.egg-link")):
                        return True
                return not bool(dist.location) or loaded_outside(dist.location)
            except Exception:
                return True
    try:
        dist = importlib_metadata.distribution("agent-workboard")
        direct = dist.read_text("direct_url.json")
    except Exception:
        return True
    if not direct:
        return loaded_outside(str(dist.locate_file("")))
    try:
        return bool(json.loads(direct).get("dir_info", {}).get("editable")) or loaded_outside(str(dist.locate_file("")))
    except ValueError:
        return True


def _validate_identity(config, database, require_database=True):
    if config["requiredPackageVersion"] != __version__:
        raise LiteError("installed package version does not match project lock")
    for key in ("sourceCommit", "sourceTree", "sourceTag"):
        if config["required" + key[0].upper() + key[1:]] != BUILD_IDENTITY[key]:
            raise LiteError("package build identity does not match project lock")
    mode = config["runtimeMode"]
    if mode == "stable":
        if not BUILD_IDENTITY["sourceCommit"] or not BUILD_IDENTITY["sourceTree"]:
            raise LiteError("stable mode requires a wheel built from a frozen commit and tree")
        if _is_editable():
            raise LiteError("stable mode refuses editable or unverified package source")
        if require_database and not os.path.isfile(database):
            raise LiteError("stable database is missing; run awb bootstrap")
    else:
        awb_root = os.path.realpath(os.path.join(os.path.dirname(database), ".."))
        expected = os.path.join(awb_root, "dev")
        if os.path.commonpath((expected, database)) != expected:
            raise LiteError("development database must stay under .awb/dev")
        stable_database = os.path.join(awb_root, "workboard.db")
        if os.path.exists(stable_database) and os.path.samefile(stable_database, database):
            raise LiteError("development database aliases stable database")


def config_for(root, development=False):
    mode = "development" if development else "stable"
    db = ".awb/dev/workboard.db" if development else ".awb/workboard.db"
    return {
        "configVersion": CONFIG_VERSION,
        "projectId": "project-" + uuid.uuid4().hex,
        "repositoryKey": os.path.basename(root) or "project",
        "database": db,
        "runtimeMode": mode,
        "requiredPackageVersion": __version__,
        "requiredSourceCommit": BUILD_IDENTITY["sourceCommit"],
        "requiredSourceTree": BUILD_IDENTITY["sourceTree"],
        "requiredSourceTag": BUILD_IDENTITY["sourceTag"],
    }


def _project_markdown(config):
    return """# Agent Workboard project contract

> Generated by the installed Agent Workboard package.

## 项目身份

- projectId: `{projectId}`
- repositoryKey: `{repositoryKey}`

## WorkItem 路由表

TI、FE、R、WA 和 AWB 是兼容 MVP-LITE-v1 的中性顶层类型。项目可在本文件补充自己的业务路由。

## 权威库/文档位置

数据库由 `.awb/config.json` 的相对路径指定；项目文档由项目自己维护。

## 角色映射

ORCHESTRATOR、PLANNER、IMPLEMENTER、REVIEWER 与 HUMAN 依照活动规格执行。

## 默认授权与禁止的远程动作

默认只允许本地工作。远程仓库、push、部署、发布和远程数据写入都需要项目的明确人工授权。
""".format(**config)


def init_project(path, with_codex=False, development=False, wheel_path=None):
    root = _project_root(path)
    awb = _awb(root)
    config = config_for(root, development)
    targets = [awb]
    if with_codex:
        targets.append(os.path.join(root, ".codex"))
    if any(os.path.lexists(target) for target in targets):
        raise LiteError("init refuses existing managed target")
    binding = _wheel_binding(wheel_path)
    created = []
    try:
        os.makedirs(awb)
        created.append(awb)
        with open(_config_path(root), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        with open(os.path.join(awb, "project.md"), "w", encoding="utf-8") as handle:
            handle.write(_project_markdown(config))
        with open(os.path.join(awb, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write("workboard.db\nworkboard.db-*\n*.backup.db\nartifacts/\nbackups/\n")
        requirements = binding["requirements"]
        if binding["temporary"]:
            artifacts = os.path.join(awb, "artifacts")
            os.makedirs(artifacts)
            artifact = os.path.join(artifacts, os.path.basename(binding["artifact"]))
            incomplete = artifact + ".tmp-" + uuid.uuid4().hex
            shutil.copyfile(binding["artifact"], incomplete)
            if _file_sha(incomplete) != binding["sha256"]:
                raise LiteError("init rebuilt wheel hash changed before installation")
            os.replace(incomplete, artifact)
            requirements = _requirement_lock(artifact, binding["sha256"])
        with open(os.path.join(awb, "requirements-awb.txt"), "w", encoding="utf-8") as handle:
            handle.write(requirements)
        database = os.path.join(root, config["database"])
        initialize_database(database)
        if with_codex:
            codex_install(root)
        return {"status": "ok", "project": root, "database": database, "runtimeMode": config["runtimeMode"]}
    except Exception:
        for target in reversed(created):
            if os.path.isdir(target):
                shutil.rmtree(target)
        raise
    finally:
        if binding["temporary"]:
            shutil.rmtree(binding["temporary"])


def bootstrap(path):
    root = _project_root(path)
    config, database = _load_config(root)
    _validate_project_contract(root)
    _validate_identity(config, database, require_database=False)
    for name in MANAGED:
        if not os.path.isfile(os.path.join(_awb(root), name)):
            raise LiteError("bootstrap requires existing .awb/{0}".format(name))
    if os.path.lexists(database):
        try:
            open_database(database).close()
        except LiteError:
            raise LiteError("bootstrap refuses invalid existing database")
        return {"status": "no-op", "database": database}
    parent = os.path.dirname(database)
    os.makedirs(parent, exist_ok=True)
    temporary = database + ".bootstrap-" + uuid.uuid4().hex
    try:
        initialize_database(temporary)
        open_database(temporary).close()
        os.replace(temporary, database)
    finally:
        for suffix in ("", "-wal", "-shm"):
            if os.path.lexists(temporary + suffix):
                os.unlink(temporary + suffix)
    return {"status": "ok", "database": database}


def doctor(path):
    root = _project_root(path)
    config, database = _load_config(root)
    _validate_project_contract(root)
    _validate_identity(config, database)
    connection = open_database(database)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        active_claims = connection.execute("SELECT count(*) FROM claims WHERE status='ACTIVE'").fetchone()[0]
        writers = connection.execute("SELECT count(*) FROM repository_locks WHERE status='ACTIVE'").fetchone()[0]
        usage_version = (USAGE_SCHEMA_VERSION if schema_installed(connection) else None)
        usage_triggers = (connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('usage_events_no_update','usage_events_no_delete')").fetchone()[0]
                          if usage_version else 0)
    finally:
        connection.close()
    if integrity != "ok" or foreign_keys or (usage_version and usage_triggers != 2):
        raise LiteError("database integrity or foreign key check failed")
    return {"status": "ok", "database": database, "schemaVersion": SCHEMA_VERSION,
            "usageSchemaVersion": usage_version, "buildIdentity": BUILD_IDENTITY,
            "activeClaims": active_claims, "activeWriters": writers}


def backup(path):
    root = _project_root(path)
    config, database = _load_config(root)
    _validate_project_contract(root)
    _validate_identity(config, database)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    target = database + "." + stamp + ".backup.db"
    source = open_database(database)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return {"status": "ok", "backup": target, "sha256": _file_sha(target)}


def _locked_wheel_from(requirements, fallback_directory):
    with open(requirements, "r", encoding="utf-8") as handle:
        lines = [line.strip() for line in handle if line.strip() and not line.lstrip().startswith("#")]
    package_lines = [line for line in lines if (line.startswith("file://") or line.startswith("https://")) and
                     "#egg=agent-workboard " in line]
    if len(package_lines) != 1 or "--hash=sha256:" not in package_lines[0]:
        raise LiteError("upgrade requires an available hash-locked old wheel")
    package, digest = package_lines[0].rsplit("--hash=sha256:", 1)
    parsed = urlparse(package.split("#egg=agent-workboard", 1)[0])
    wheel = unquote(parsed.path) if parsed.scheme == "file" else os.path.join(fallback_directory,
                                                                               os.path.basename(parsed.path))
    if not os.path.isfile(wheel) or _file_sha(wheel) != digest:
        raise LiteError("upgrade old wheel is unavailable or its hash changed")
    return wheel, digest


def _locked_wheel(root, fallback_directory):
    return _locked_wheel_from(os.path.join(_awb(root), "requirements-awb.txt"), fallback_directory)


def _wheel_resource(wheel, resource):
    try:
        with zipfile.ZipFile(wheel) as archive:
            return archive.read("agent_workboard/resources/" + resource)
    except Exception:
        raise LiteError("upgrade cannot verify old package resource: " + resource)


def _atomic_bytes(path, value):
    temporary = path + ".upgrade-" + uuid.uuid4().hex
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(temporary, "wb") as handle:
            handle.write(value)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _identity_from_config(config):
    return {
        "packageVersion": config["requiredPackageVersion"],
        "sourceCommit": config["requiredSourceCommit"],
        "sourceTree": config["requiredSourceTree"],
        "sourceTag": config["requiredSourceTag"],
    }


def _next_step(action, **arguments):
    return {"action": action, "arguments": arguments}


def _upgrade_envelope(operation, status, root, from_identity=None, to_identity=None,
                      applicability="UNSUPPORTED", evidence=None, risks=None, paths=None,
                      rollback=None, next_step=None, reason=None):
    result = {
        "protocolVersion": UPGRADE_PROTOCOL,
        "operation": operation,
        "status": status,
        "project": root,
        "from": from_identity,
        "to": to_identity,
        "applicability": {"status": applicability},
        "evidence": evidence or [],
        "risks": risks or [],
        "paths": paths or {"backup": [], "replaced": [], "created": [], "untouched": []},
        "rollback": rollback or {"manifest": None, "actions": []},
        "nextStep": next_step or _next_step("STOP"),
    }
    if reason:
        result["reason"] = reason
    return result


def _upgrade_refused(operation, root, reason, from_identity=None, to_identity=None,
                      evidence=None, rollback=None, status="REFUSED", next_step=None):
    pair = (from_identity, to_identity)
    supported = pair in tuple((source, BUILD_IDENTITY) for source in SUPPORTED_UPGRADE_SOURCES) + \
        tuple((BUILD_IDENTITY, source) for source in SUPPORTED_UPGRADE_SOURCES)
    return _upgrade_envelope(
        operation, status, root, from_identity, to_identity,
        applicability="SUPPORTED" if supported else "UNSUPPORTED",
        evidence=evidence, risks=[{"id": "UPGRADE-RISK-001", "detail": reason}],
        rollback=rollback, reason=reason,
        next_step=next_step or _next_step("STOP_AND_RETAIN_EVIDENCE", project=root),
    )


def _forward_refusal_step(reason, root, wheel, with_codex):
    arguments = {"project": root, "wheel": os.path.realpath(os.path.abspath(wheel)) if wheel else None,
                 "withCodex": bool(with_codex)}
    if "active claim or repository writer" in reason:
        action = "RELEASE_ACTIVE_USE_AND_RECHECK_UPGRADE"
    elif "backup" in reason and ("symbolic" in reason or "directory" in reason or "escapes" in reason):
        action = "RESTORE_SAFE_BACKUP_ROOT_AND_RECHECK"
    elif "customized or unowned Codex" in reason:
        action = "RESTORE_PACKAGE_OWNED_CODEX_AND_RECHECK"
    elif ("outside the explicit" in reason or "outside the exact" in reason or
          "does not match the running" in reason):
        action = "STOP_UNSUPPORTED_UPGRADE_PAIR"
    else:
        action = "FIX_REPORTED_REASON_AND_RECHECK_UPGRADE"
    return _next_step(action, **arguments)


def _rollback_refusal_step(reason, root, manifest_path):
    arguments = {"project": root, "rollbackManifest": os.path.abspath(manifest_path)
                 if manifest_path else None}
    if "active claim or repository writer" in reason:
        action = "RELEASE_ACTIVE_USE_AND_RECHECK_ROLLBACK"
    elif "already consumed" in reason or "replay" in reason:
        action = "STOP_MANIFEST_REPLAY"
    else:
        action = "STOP_AND_RETAIN_ROLLBACK_EVIDENCE"
    return _next_step(action, **arguments)


def _regular_project_path(root, path, label, allow_missing=False):
    absolute = os.path.abspath(path)
    try:
        if os.path.commonpath((root, absolute)) != root:
            raise LiteError(label + " escapes project root")
    except ValueError:
        raise LiteError(label + " escapes project root")
    relative = os.path.relpath(absolute, root)
    cursor = root
    for component in relative.split(os.sep):
        cursor = os.path.join(cursor, component)
        if os.path.lexists(cursor) and os.path.islink(cursor):
            raise LiteError(label + " contains a symbolic link")
    if os.path.lexists(absolute):
        if not os.path.isfile(absolute):
            raise LiteError(label + " is not a regular file")
    elif not allow_missing:
        raise LiteError(label + " is missing")
    return absolute


def _validate_backup_root(root):
    awb_root = os.path.join(root, ".awb")
    if os.path.islink(awb_root) or not os.path.isdir(awb_root):
        raise LiteError("upgrade backup parent is symbolic or not a directory")
    backups = os.path.join(awb_root, "backups")
    if os.path.lexists(backups):
        if os.path.islink(backups) or not os.path.isdir(backups):
            raise LiteError("upgrade backup root is symbolic or not a directory")
        try:
            if os.path.commonpath((os.path.realpath(awb_root), os.path.realpath(backups))) != os.path.realpath(awb_root):
                raise LiteError("upgrade backup root escapes its project parent")
        except ValueError:
            raise LiteError("upgrade backup root escapes its project parent")
    return backups


def _usage_schema_state(connection):
    marker = connection.execute(
        "SELECT value FROM schema_meta WHERE key='usage_schema_version'"
    ).fetchone()
    expected = {
        "usage_events", "usage_events_work_item", "usage_events_stream",
        "usage_source_snapshot_once", "usage_events_no_update", "usage_events_no_delete",
    }
    present = set(row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'usage_%'"
    ))
    if marker is None and not present:
        return "ABSENT"
    columns = [row[1] for row in connection.execute("PRAGMA table_info(usage_events)")]
    expected_columns = [
        "usage_event_id", "request_id", "event_type", "work_item_id", "task_id",
        "claim_id", "provider", "source_session_id", "source_snapshot_key",
        "actor_kind", "actor_id", "payload_json", "observed_at", "created_at",
    ]
    if (marker and marker[0] == USAGE_SCHEMA_VERSION and present == expected and
            columns == expected_columns):
        return "INSTALLED"
    return "INVALID"


def _database_preflight(database):
    if os.path.islink(database) or not os.path.isfile(database):
        raise LiteError("upgrade database is missing or symbolic")
    with tempfile.TemporaryDirectory(prefix="awb-upgrade-check-") as temporary:
        snapshot = os.path.join(temporary, "workboard.db")
        for suffix in ("", "-wal", "-shm"):
            source = database + suffix
            if os.path.isfile(source):
                shutil.copyfile(source, snapshot + suffix)
        connection = sqlite3.connect(snapshot)
        try:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            claims = connection.execute("SELECT count(*) FROM claims WHERE status='ACTIVE'").fetchone()[0]
            writers = connection.execute("SELECT count(*) FROM repository_locks WHERE status='ACTIVE'").fetchone()[0]
            usage_state = _usage_schema_state(connection)
        except sqlite3.Error as exc:
            raise LiteError("upgrade cannot read database: {0}".format(exc))
        finally:
            connection.close()
    if not version or version[0] != SCHEMA_VERSION or integrity != "ok" or foreign_keys:
        raise LiteError("upgrade refuses an invalid database")
    if claims or writers:
        raise LiteError("upgrade refuses active claim or repository writer")
    if usage_state == "INVALID":
        raise LiteError("upgrade refuses an invalid or unexpected usage extension")
    return {"activeClaims": claims, "activeWriters": writers, "integrity": integrity,
            "usageSchemaState": usage_state}


def _wheel_resource_optional(wheel, resource):
    try:
        with zipfile.ZipFile(wheel) as archive:
            return archive.read("agent_workboard/resources/" + resource)
    except KeyError:
        return None
    except Exception:
        raise LiteError("upgrade cannot verify old package resource: " + resource)


def _path_entry(root, path, before, after, action=None):
    entry = {"path": os.path.relpath(path, root).replace(os.sep, "/")}
    entry["beforeSha256"] = _sha(before) if before is not None else None
    entry["afterSha256"] = _sha(after) if after is not None else None
    if action:
        entry["action"] = action
    return entry


def _upgrade_preflight(path, wheel_path, with_codex, operation):
    root = _project_root(path)
    evidence = []
    try:
        _validate_backup_root(root)
        _regular_project_path(root, _config_path(root), "project config")
        config, database = _load_config(root)
        current_identity = _identity_from_config(config)
        if config["runtimeMode"] != "stable":
            raise LiteError("upgrade requires a stable project")
        for name in MANAGED:
            _regular_project_path(root, os.path.join(_awb(root), name), "managed path " + name)
        _validate_project_contract(root)
        evidence.append({"id": "PROJECT_CONTRACT", "status": "PASS"})
        original_wheel = os.path.abspath(wheel_path)
        if os.path.islink(original_wheel) or not os.path.isfile(original_wheel):
            raise LiteError("upgrade target wheel is missing or symbolic")
        target_wheel = os.path.realpath(original_wheel)
        target_identity = _wheel_identity(target_wheel)
        if (target_identity != BUILD_IDENTITY or
                BUILD_IDENTITY.get("packageVersion") != "0.3.0b1" or
                BUILD_IDENTITY.get("sourceTag") != "v0.3.0b1"):
            raise LiteError("upgrade target wheel does not match the running 0.3.0b1 Preview release")
        target_digest = _file_sha(target_wheel)
        evidence.append({"id": "TARGET_WHEEL", "status": "PASS", "sha256": target_digest})
        database_status = _database_preflight(database)
        evidence.append(dict({"id": "DATABASE", "status": "PASS"}, **database_status))

        if current_identity == target_identity:
            if database_status["usageSchemaState"] != "INSTALLED":
                raise LiteError("same-identity 0.3.0b1 project is missing the usage schema")
            result = _upgrade_envelope(
                operation, "NO_OP", root, current_identity, target_identity,
                applicability="NO_OP", evidence=evidence,
                paths={"backup": [], "replaced": [], "created": [],
                       "untouched": [{"path": os.path.relpath(database, root).replace(os.sep, "/")}]},
                next_step=_next_step("NONE"),
            )
            return {"result": result, "root": root, "config": config, "database": database}

        if current_identity not in SUPPORTED_UPGRADE_SOURCES:
            raise LiteError("upgrade source identity is outside the exact 0.2.1 to 0.3.0b1 matrix")
        if database_status["usageSchemaState"] != "ABSENT":
            raise LiteError("upgrade refuses a 0.2.1 database with an unexpected usage extension")
        old_wheel, old_digest = _locked_wheel(root, os.path.dirname(target_wheel))
        old_identity = _wheel_identity(old_wheel)
        if old_identity != current_identity:
            raise LiteError("upgrade old wheel identity is outside the explicit release matrix")
        evidence.append({"id": "SOURCE_WHEEL", "status": "PASS", "sha256": old_digest})

        codex_changes = []
        codex_untouched = []
        for target, resource in _codex_targets(root).items():
            relative = os.path.relpath(target, root).replace(os.sep, "/")
            if not with_codex:
                codex_untouched.append({"path": relative})
                continue
            _regular_project_path(root, target, "Codex path " + relative, allow_missing=True)
            replacement = _resource(resource)
            old_resource = _wheel_resource_optional(old_wheel, resource)
            if os.path.exists(target):
                current = _file_bytes(target)
                if old_resource is None or current != old_resource:
                    raise LiteError("upgrade refuses customized or unowned Codex file: " + relative)
            codex_changes.append((target, replacement))
        evidence.append({"id": "CODEX_OWNERSHIP", "status": "PASS",
                         "checked": bool(with_codex)})

        updated = dict(config)
        updated["requiredPackageVersion"] = target_identity["packageVersion"]
        updated["requiredSourceCommit"] = target_identity["sourceCommit"]
        updated["requiredSourceTree"] = target_identity["sourceTree"]
        updated["requiredSourceTag"] = target_identity["sourceTag"]
        ignore_path = os.path.join(_awb(root), ".gitignore")
        ignore_lines = _file_bytes(ignore_path).rstrip(b"\n").splitlines()
        if b"backups/" not in ignore_lines:
            ignore_lines.append(b"backups/")
        replacements = [
            (_config_path(root), (json.dumps(updated, ensure_ascii=False, sort_keys=True,
                                              indent=2) + "\n").encode("utf-8")),
            (os.path.join(_awb(root), "requirements-awb.txt"),
             _requirement_lock(target_wheel, target_digest).encode("utf-8")),
            (ignore_path, b"\n".join(ignore_lines) + b"\n"),
        ] + codex_changes
        changed = []
        created = []
        untouched = [{"path": os.path.relpath(os.path.join(_awb(root), "project.md"), root).replace(os.sep, "/")}] + codex_untouched
        effective = []
        for target, replacement in replacements:
            before = _file_bytes(target) if os.path.exists(target) else None
            if before == replacement:
                untouched.append(_path_entry(root, target, before, replacement))
                continue
            action = "RESTORE" if before is not None else "REMOVE_CREATED"
            entry = _path_entry(root, target, before, replacement, action)
            (changed if before is not None else created).append(entry)
            effective.append((target, replacement, before, action))
        backup = [{"path": os.path.relpath(os.path.join(_awb(root), name), root).replace(os.sep, "/")}
                  for name in MANAGED]
        backup.append({"path": os.path.relpath(database, root).replace(os.sep, "/"),
                       "kind": "DATABASE_BACKUP"})
        backup += [{"path": os.path.relpath(target, root).replace(os.sep, "/")}
                   for target, unused, before, unused_action in effective if before is not None and
                   target not in [os.path.join(_awb(root), name) for name in MANAGED]]
        database_entry = {
            "path": os.path.relpath(database, root).replace(os.sep, "/"),
            "beforeSha256": _file_sha(database), "afterSha256": None,
            "action": "RESTORE",
        }
        projected = changed + created + [database_entry]
        paths = {"backup": sorted(backup, key=lambda item: item["path"]),
                 "replaced": sorted(changed + [database_entry], key=lambda item: item["path"]),
                 "created": sorted(created, key=lambda item: item["path"]),
                 "untouched": sorted(untouched, key=lambda item: item["path"])}
        next_step = (_next_step("EXECUTE_UPGRADE", project=root, wheel=target_wheel,
                                withCodex=with_codex) if operation == "CHECK" else
                     _next_step("RUN_POST_UPGRADE_VALIDATION", project=root,
                                withCodex=with_codex))
        result = _upgrade_envelope(
            operation, "READY" if operation == "CHECK" else "OK", root,
            current_identity, target_identity, applicability="SUPPORTED", evidence=evidence,
            paths=paths, rollback={"manifest": None, "actions": projected}, next_step=next_step,
        )
        return {"result": result, "root": root, "config": config, "database": database,
                "targetWheel": target_wheel, "targetDigest": target_digest,
                "oldWheel": old_wheel, "oldDigest": old_digest,
                "replacements": effective, "paths": paths, "withCodex": with_codex}
    except LiteError as exc:
        return {"result": _upgrade_refused(operation, root, str(exc),
                                            locals().get("current_identity"),
                                            locals().get("target_identity"), evidence=evidence,
                                            next_step=_forward_refusal_step(
                                                str(exc), root, wheel_path, with_codex))}


def _database_backup(database, destination):
    source = open_database(database)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _restore_database(database, backup_path):
    for suffix in ("-wal", "-shm"):
        sidecar = database + suffix
        if os.path.lexists(sidecar):
            if os.path.islink(sidecar) or not os.path.isfile(sidecar):
                raise LiteError("database sidecar is unsafe")
            os.unlink(sidecar)
    _atomic_bytes(database, _file_bytes(backup_path))


def _write_upgrade(plan):
    root = plan["root"]
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    upgrade_id = "upgrade-" + uuid.uuid4().hex
    backups = _validate_backup_root(root)
    backup_root = os.path.join(backups, "upgrade-" + stamp)
    os.makedirs(backup_root)
    backup_files = {}
    database_backup = None
    created_targets = [target for target, unused, before, unused_action in plan["replacements"]
                       if before is None]
    try:
        managed_paths = [os.path.join(_awb(root), name) for name in MANAGED]
        managed_paths += [target for target, unused, before, unused_action in plan["replacements"]
                          if before is not None and target not in managed_paths]
        for source in managed_paths:
            relative = os.path.relpath(source, root)
            destination = os.path.join(backup_root, relative)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copyfile(source, destination)
            backup_files[source] = destination
        database_backup = os.path.join(backup_root, ".awb", "workboard.db")
        os.makedirs(os.path.dirname(database_backup), exist_ok=True)
        _database_backup(plan["database"], database_backup)

        actions = []
        for target, replacement, before, action in plan["replacements"]:
            relative = os.path.relpath(target, root).replace(os.sep, "/")
            saved = (os.path.relpath(backup_files[target], backup_root).replace(os.sep, "/")
                     if before is not None else None)
            actions.append({
                "action": action,
                "path": relative,
                "before": {"exists": before is not None,
                           "sha256": _sha(before) if before is not None else None,
                           "backup": saved},
                "after": {"exists": True, "sha256": _sha(replacement)},
            })
        for target, replacement, unused_before, unused_action in plan["replacements"]:
            _atomic_bytes(target, replacement)
        connection = open_database(plan["database"])
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + usage_schema_sql() + "\nCOMMIT;")
            if (_usage_schema_state(connection) != "INSTALLED" or
                    connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or
                    connection.execute("PRAGMA foreign_key_check").fetchall()):
                raise LiteError("usage migration validation failed")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        database_relative = os.path.relpath(plan["database"], root).replace(os.sep, "/")
        actions.append({
            "action": "RESTORE",
            "path": database_relative,
            "before": {"exists": True, "sha256": _file_sha(database_backup),
                       "backup": os.path.relpath(database_backup, backup_root).replace(os.sep, "/")},
            "after": {"exists": True, "sha256": _file_sha(plan["database"])},
        })
        manifest_path = os.path.join(backup_root, "rollback.json")
        manifest = {
            "protocolVersion": ROLLBACK_PROTOCOL,
            "state": "ACTIVE",
            "upgradeId": upgrade_id,
            "createdAt": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "projectId": plan["config"]["projectId"],
            "repositoryKey": plan["config"]["repositoryKey"],
            "projectRoot": root,
            "database": os.path.relpath(plan["database"], root).replace(os.sep, "/"),
            "backupRoot": backup_root,
            "from": _identity_from_config(plan["config"]),
            "to": dict(BUILD_IDENTITY),
            "withCodex": plan["withCodex"],
            "actions": actions,
            "retained": [],
            "untouched": [dict({"action": "UNTOUCHED"}, **entry) for entry in plan["paths"]["untouched"]],
        }
        _atomic_bytes(manifest_path, (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                                 indent=2) + "\n").encode("utf-8"))
    except Exception:
        for target, saved in backup_files.items():
            shutil.copyfile(saved, target)
        if database_backup and os.path.isfile(database_backup):
            _restore_database(plan["database"], database_backup)
        for target in created_targets:
            if os.path.isfile(target) and not os.path.islink(target):
                os.unlink(target)
        raise
    result = dict(plan["result"])
    result["status"] = "OK"
    result["paths"] = dict(plan["paths"])
    result["paths"]["backup"] = [
        {"path": os.path.relpath(path, root).replace(os.sep, "/"),
         "backupPath": os.path.relpath(saved, root).replace(os.sep, "/"),
         "sha256": _file_sha(saved)} for path, saved in sorted(backup_files.items())
    ]
    result["paths"]["backup"].append(
        {"path": os.path.relpath(plan["database"], root).replace(os.sep, "/"),
         "backupPath": os.path.relpath(database_backup, root).replace(os.sep, "/"),
         "sha256": _file_sha(database_backup), "kind": "DATABASE_BACKUP"}
    )
    result["paths"]["replaced"] = sorted(
        [entry for entry in result["paths"]["replaced"]
         if entry["path"] != os.path.relpath(plan["database"], root).replace(os.sep, "/")] +
        [{"path": os.path.relpath(plan["database"], root).replace(os.sep, "/"),
          "beforeSha256": _file_sha(database_backup),
          "afterSha256": _file_sha(plan["database"]), "action": "RESTORE"}],
        key=lambda item: item["path"],
    )
    result["rollback"] = {"manifest": manifest_path, "databaseBackup": database_backup,
                          "actions": actions}
    result["nextStep"] = _next_step("RUN_POST_UPGRADE_VALIDATION", project=root,
                                      withCodex=plan["withCodex"])
    return result


def _manifest_path(root, path):
    backups = _validate_backup_root(root)
    supplied = os.path.abspath(path)
    if os.path.islink(supplied) or not os.path.isfile(supplied):
        raise LiteError("rollback manifest is missing or symbolic")
    canonical = os.path.realpath(supplied)
    backups = os.path.realpath(backups)
    if (os.path.dirname(os.path.dirname(canonical)) != backups or
            not os.path.basename(os.path.dirname(canonical)).startswith("upgrade-") or
            os.path.basename(canonical) != "rollback.json"):
        raise LiteError("rollback manifest is outside the bound backup root")
    return canonical


def _manifest_target(base, relative, label, allow_missing=False):
    if (not isinstance(relative, str) or not relative or "\\" in relative or
            os.path.isabs(relative) or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise LiteError(label + " has an unsafe relative path")
    return _regular_project_path(base, os.path.join(base, *relative.split("/")), label,
                                 allow_missing=allow_missing)


def _expected_rollback_material(root, database, manifest, backup_root):
    """Reconstruct the only mutations the bounded 0.3.0b1 upgrade can make."""
    if not isinstance(manifest.get("withCodex"), bool):
        raise LiteError("rollback manifest Codex selection is invalid")
    managed_backups = {}
    for name in MANAGED:
        relative = ".awb/" + name
        managed_backups[name] = _manifest_target(backup_root, relative,
                                                  "managed rollback backup")

    target_wheel, target_digest = _locked_wheel(root, backup_root)
    if _wheel_identity(target_wheel) != manifest["to"]:
        raise LiteError("rollback target wheel identity drift")
    old_wheel, unused_old_digest = _locked_wheel_from(
        managed_backups["requirements-awb.txt"], os.path.dirname(target_wheel))
    if _wheel_identity(old_wheel) != manifest["from"]:
        raise LiteError("rollback source wheel identity drift")

    try:
        with open(managed_backups["config.json"], "r", encoding="utf-8") as handle:
            old_config = json.load(handle)
    except (IOError, ValueError, TypeError):
        raise LiteError("rollback backup hash drift or config backup is invalid")
    updated = dict(old_config)
    updated["requiredPackageVersion"] = manifest["to"]["packageVersion"]
    updated["requiredSourceCommit"] = manifest["to"]["sourceCommit"]
    updated["requiredSourceTree"] = manifest["to"]["sourceTree"]
    updated["requiredSourceTag"] = manifest["to"]["sourceTag"]
    ignore_before = _file_bytes(managed_backups[".gitignore"])
    ignore_lines = ignore_before.rstrip(b"\n").splitlines()
    if b"backups/" not in ignore_lines:
        ignore_lines.append(b"backups/")
    replacements = [
        (_config_path(root), _file_bytes(managed_backups["config.json"]),
         (json.dumps(updated, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")),
        (os.path.join(_awb(root), "requirements-awb.txt"),
         _file_bytes(managed_backups["requirements-awb.txt"]),
         _requirement_lock(target_wheel, target_digest).encode("utf-8")),
        (os.path.join(_awb(root), ".gitignore"), ignore_before,
         b"\n".join(ignore_lines) + b"\n"),
    ]
    codex_untouched = []
    for target, resource in _codex_targets(root).items():
        relative = os.path.relpath(target, root).replace(os.sep, "/")
        if not manifest["withCodex"]:
            codex_untouched.append({"path": relative})
            continue
        before = _wheel_resource_optional(old_wheel, resource)
        after = _wheel_resource(target_wheel, resource)
        replacements.append((target, before, after))

    expected_actions = []
    untouched = [
        {"path": os.path.relpath(os.path.join(_awb(root), "project.md"), root).replace(os.sep, "/")},
    ] + codex_untouched
    for target, before, after in replacements:
        if before == after:
            untouched.append(_path_entry(root, target, before, after))
            continue
        action = "RESTORE" if before is not None else "REMOVE_CREATED"
        relative = os.path.relpath(target, root).replace(os.sep, "/")
        expected_actions.append({
            "action": action,
            "path": relative,
            "before": {"exists": before is not None,
                       "sha256": _sha(before) if before is not None else None,
                       "backup": relative if before is not None else None},
            "after": {"exists": True, "sha256": _sha(after)},
        })
    database_relative = os.path.relpath(database, root).replace(os.sep, "/")
    database_backup = _manifest_target(backup_root, database_relative,
                                       "database rollback backup")
    if (_database_preflight(database_backup)["usageSchemaState"] != "ABSENT" or
            _database_preflight(database)["usageSchemaState"] != "INSTALLED"):
        raise LiteError("rollback database schema state is outside the exact migration pair")
    expected_actions.append({
        "action": "RESTORE",
        "path": database_relative,
        "before": {"exists": True, "sha256": _file_sha(database_backup),
                   "backup": database_relative},
        "after": {"exists": True, "sha256": _file_sha(database)},
    })
    expected_retained = []
    expected_untouched = [dict({"action": "UNTOUCHED"}, **entry)
                          for entry in sorted(untouched, key=lambda item: item["path"])]
    if (manifest["actions"] != expected_actions or
            manifest["retained"] != expected_retained or
            manifest["untouched"] != expected_untouched):
        raise LiteError("rollback manifest differs from the closed managed action universe")
    return {"oldWheel": old_wheel, "targetWheel": target_wheel}


def _load_rollback(path, manifest_path, operation):
    root = _project_root(path)
    evidence = []
    try:
        manifest_path = _manifest_path(root, manifest_path)
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if not isinstance(manifest, dict):
            raise LiteError("rollback manifest structure is invalid")
        required = {"protocolVersion", "state", "upgradeId", "createdAt", "projectId",
                    "repositoryKey", "projectRoot", "database", "backupRoot", "from", "to",
                    "withCodex", "actions", "retained", "untouched"}
        allowed = required | ({"consumedAt"} if manifest.get("state") == "CONSUMED" else set())
        if set(manifest) != allowed or manifest["protocolVersion"] != ROLLBACK_PROTOCOL:
            raise LiteError("rollback manifest structure is invalid")
        if manifest["state"] not in ("ACTIVE", "CONSUMED"):
            raise LiteError("rollback manifest state is invalid")
        backup_root = os.path.dirname(manifest_path)
        config, database = _load_config(root)
        if (manifest["projectRoot"] != root or manifest["backupRoot"] != backup_root or
                manifest["projectId"] != config["projectId"] or
                manifest["repositoryKey"] != config["repositoryKey"] or
                manifest["database"] != os.path.relpath(database, root).replace(os.sep, "/")):
            raise LiteError("rollback manifest is bound to a different project")
        if (set(manifest["from"]) != set(IDENTITY_KEYS) or set(manifest["to"]) != set(IDENTITY_KEYS) or
                manifest["from"] not in SUPPORTED_UPGRADE_SOURCES or manifest["to"] != BUILD_IDENTITY):
            raise LiteError("rollback manifest identity is outside the explicit release matrix")
        _validate_project_contract(root)
        database_status = _database_preflight(database)
        evidence.append(dict({"id": "DATABASE", "status": "PASS"}, **database_status))
        if manifest["state"] == "CONSUMED":
            raise LiteError("rollback manifest was already consumed")

        wheels = _expected_rollback_material(root, database, manifest, backup_root)

        actions = []
        targets = set()
        states = []
        for raw in manifest["actions"]:
            if (not isinstance(raw, dict) or set(raw) != {"action", "path", "before", "after"} or
                    raw["action"] not in ("RESTORE", "REMOVE_CREATED") or
                    not isinstance(raw["before"], dict) or not isinstance(raw["after"], dict)):
                raise LiteError("rollback action structure is invalid")
            target = _manifest_target(root, raw["path"], "rollback target", allow_missing=True)
            if target in targets:
                raise LiteError("rollback manifest contains duplicate targets")
            targets.add(target)
            before = raw["before"]
            after = raw["after"]
            if set(before) != {"exists", "sha256", "backup"} or set(after) != {"exists", "sha256"}:
                raise LiteError("rollback action hashes are incomplete")
            if (after.get("exists") is not True or not isinstance(after.get("sha256"), str) or
                    len(after["sha256"]) != 64 or
                    any(character not in "0123456789abcdef" for character in after["sha256"])):
                raise LiteError("rollback after-state is invalid")
            backup = None
            if raw["action"] == "RESTORE":
                if (before.get("exists") is not True or not isinstance(before.get("sha256"), str) or
                        len(before["sha256"]) != 64 or
                        any(character not in "0123456789abcdef" for character in before["sha256"])):
                    raise LiteError("rollback restore before-state is invalid")
                backup = _manifest_target(backup_root, before.get("backup"), "rollback backup")
                if _file_sha(backup) != before["sha256"]:
                    raise LiteError("rollback backup hash drift")
            elif before != {"exists": False, "sha256": None, "backup": None}:
                raise LiteError("rollback created-file proof is invalid")
            if os.path.exists(target):
                current = _file_sha(target)
                state = "AFTER" if current == after["sha256"] else (
                    "BEFORE" if before["exists"] and current == before["sha256"] else "DRIFT")
            else:
                state = "BEFORE" if not before["exists"] else "DRIFT"
            states.append(state)
            actions.append({"raw": raw, "target": target, "backup": backup})
        if not actions:
            raise LiteError("rollback manifest has no mutations")

        staging = os.path.join(backup_root, "rollback-staging")
        if os.path.lexists(staging):
            if os.path.islink(staging) or not os.path.isdir(staging):
                raise LiteError("rollback recovery staging is unsafe")
            result = _upgrade_refused(operation, root, "rollback compensation recovery is required",
                                       manifest["from"], manifest["to"], evidence=evidence,
                                       rollback={"manifest": manifest_path, "actions": manifest["actions"]},
                                       status="BLOCKED",
                                       next_step=_next_step("EXECUTE_RECOVERY", project=root,
                                                            rollbackManifest=manifest_path))
            return {"result": result, "root": root, "database": database, "manifest": manifest,
                    "manifestPath": manifest_path, "actions": actions, "staging": staging,
                    "oldWheel": wheels["oldWheel"]}
        if all(state == "BEFORE" for state in states):
            raise LiteError("rollback manifest is a replay against an already restored project")
        if not all(state == "AFTER" for state in states):
            raise LiteError("rollback target hash drift or mixed state")
        evidence.append({"id": "ROLLBACK_BINDING", "status": "PASS",
                         "actionCount": len(actions)})
        status = "READY" if operation == "CHECK" else "OK"
        next_step = (_next_step("EXECUTE_ROLLBACK", project=root,
                                rollbackManifest=manifest_path)
                     if operation == "CHECK" else
                     _next_step("INSTALL_OLD_WHEEL_AND_VERIFY", project=root,
                                wheel=wheels["oldWheel"], withCodex=manifest["withCodex"],
                                rollbackManifest=manifest_path))
        result = _upgrade_envelope(
            operation, status, root, manifest["to"], manifest["from"],
            applicability="SUPPORTED", evidence=evidence,
            paths={"backup": [],
                   "replaced": [raw["raw"] for raw in actions if raw["raw"]["action"] == "RESTORE"],
                   "created": [raw["raw"] for raw in actions if raw["raw"]["action"] == "REMOVE_CREATED"],
                   "untouched": manifest["untouched"] + manifest["retained"]},
            rollback={"manifest": manifest_path, "actions": manifest["actions"]},
            next_step=next_step,
        )
        return {"result": result, "root": root, "database": database, "manifest": manifest,
                "manifestPath": manifest_path, "actions": actions, "staging": staging,
                "oldWheel": wheels["oldWheel"]}
    except (LiteError, IOError, ValueError, TypeError, AttributeError) as exc:
        candidate = locals().get("manifest")
        candidate = candidate if isinstance(candidate, dict) else {}
        return {"result": _upgrade_refused(operation, root, str(exc),
                                            candidate.get("to"), candidate.get("from"), evidence=evidence,
                                            rollback={"manifest": locals().get("manifest_path"), "actions": []},
                                            next_step=_rollback_refusal_step(
                                                str(exc), root, locals().get("manifest_path") or manifest_path))}


def _stage_rollback(plan):
    os.makedirs(plan["staging"])
    staged = []
    try:
        for index, action in enumerate(plan["actions"]):
            relative = "{0:03d}.post".format(index)
            destination = os.path.join(plan["staging"], relative)
            shutil.copyfile(action["target"], destination)
            staged.append({"path": action["raw"]["path"], "staged": relative,
                           "sha256": action["raw"]["after"]["sha256"]})
            if _file_sha(destination) != action["raw"]["after"]["sha256"]:
                raise LiteError("rollback staging hash mismatch")
        recovery = {"protocolVersion": ROLLBACK_PROTOCOL,
                    "upgradeId": plan["manifest"]["upgradeId"], "files": staged}
        _atomic_bytes(os.path.join(plan["staging"], "recovery.json"),
                      (json.dumps(recovery, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    except Exception:
        shutil.rmtree(plan["staging"], ignore_errors=True)
        raise


def _validate_staging(plan):
    recovery_path = os.path.join(plan["staging"], "recovery.json")
    try:
        with open(recovery_path, "r", encoding="utf-8") as handle:
            recovery = json.load(handle)
    except (IOError, ValueError):
        raise LiteError("rollback recovery staging is incomplete")
    if (set(recovery) != {"protocolVersion", "upgradeId", "files"} or
            recovery["protocolVersion"] != ROLLBACK_PROTOCOL or
            recovery["upgradeId"] != plan["manifest"]["upgradeId"] or
            len(recovery["files"]) != len(plan["actions"])):
        raise LiteError("rollback recovery staging binding is invalid")
    staged = []
    for index, (record, action) in enumerate(zip(recovery["files"], plan["actions"])):
        expected = {"path": action["raw"]["path"], "staged": "{0:03d}.post".format(index),
                    "sha256": action["raw"]["after"]["sha256"]}
        if record != expected:
            raise LiteError("rollback recovery staging action drift")
        source = _manifest_target(plan["staging"], record["staged"], "rollback staged file")
        if _file_sha(source) != record["sha256"]:
            raise LiteError("rollback recovery staging hash drift")
        staged.append(source)
    return staged


def _apply_rollback_action(action, database):
    if action["raw"]["action"] == "RESTORE":
        if action["target"] == database:
            _restore_database(database, action["backup"])
        else:
            _atomic_bytes(action["target"], _file_bytes(action["backup"]))
    else:
        os.unlink(action["target"])


def _restore_post_upgrade(action, staged, database):
    if action["target"] == database:
        _restore_database(database, staged)
    else:
        _atomic_bytes(action["target"], _file_bytes(staged))


def _recover_rollback(plan):
    try:
        staged = _validate_staging(plan)
        for action, saved in zip(plan["actions"], staged):
            _restore_post_upgrade(action, saved, plan["database"])
        if any(not os.path.isfile(action["target"]) or
               _file_sha(action["target"]) != action["raw"]["after"]["sha256"]
               for action in plan["actions"]):
            raise LiteError("rollback compensation verification failed")
        shutil.rmtree(plan["staging"])
        return _upgrade_refused(
            "ROLLBACK", plan["root"], "rollback compensation restored the complete post-upgrade state",
            plan["manifest"]["to"], plan["manifest"]["from"],
            rollback={"manifest": plan["manifestPath"], "actions": plan["manifest"]["actions"]},
            next_step=_next_step("RETRY_ROLLBACK_CHECK", project=plan["root"],
                                 rollbackManifest=plan["manifestPath"]),
        )
    except Exception as exc:
        return _upgrade_refused(
            "ROLLBACK", plan["root"], "rollback compensation remains incomplete: {0}".format(exc),
            plan["manifest"]["to"], plan["manifest"]["from"],
            rollback={"manifest": plan["manifestPath"], "actions": plan["manifest"]["actions"]},
            status="BLOCKED",
            next_step=_next_step("EXECUTE_RECOVERY", project=plan["root"],
                                 rollbackManifest=plan["manifestPath"]),
        )


def _write_rollback(plan):
    if plan["result"]["status"] == "BLOCKED":
        return _recover_rollback(plan)
    try:
        _stage_rollback(plan)
    except Exception as exc:
        return _upgrade_refused(
            "ROLLBACK", plan["root"], "rollback could not create complete staging: {0}".format(exc),
            plan["manifest"]["to"], plan["manifest"]["from"],
            rollback={"manifest": plan["manifestPath"], "actions": plan["manifest"]["actions"]},
            next_step=_next_step("RETRY_ROLLBACK_CHECK", project=plan["root"],
                                 rollbackManifest=plan["manifestPath"]),
        )
    try:
        for action in plan["actions"]:
            _apply_rollback_action(action, plan["database"])
        for action in plan["actions"]:
            before = action["raw"]["before"]
            if before["exists"]:
                if (not os.path.isfile(action["target"]) or
                        _file_sha(action["target"]) != before["sha256"]):
                    raise LiteError("rollback before-state verification failed")
            elif os.path.lexists(action["target"]):
                raise LiteError("rollback created-file removal verification failed")
    except Exception:
        return _recover_rollback(plan)
    shutil.rmtree(plan["staging"])
    consumed = dict(plan["manifest"])
    consumed["state"] = "CONSUMED"
    consumed["consumedAt"] = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    _atomic_bytes(plan["manifestPath"], (json.dumps(consumed, ensure_ascii=False, sort_keys=True,
                                                    indent=2) + "\n").encode("utf-8"))
    result = dict(plan["result"])
    result["status"] = "OK"
    result["paths"] = {
        "backup": [],
        "replaced": [{"path": action["raw"]["path"], "action": "RESTORE"}
                     for action in plan["actions"] if action["raw"]["action"] == "RESTORE"],
        "created": [{"path": action["raw"]["path"], "action": "REMOVE_CREATED"}
                    for action in plan["actions"] if action["raw"]["action"] == "REMOVE_CREATED"],
        "untouched": plan["manifest"]["untouched"] + plan["manifest"]["retained"],
    }
    result["rollback"] = {"manifest": plan["manifestPath"], "state": "CONSUMED",
                          "actions": plan["manifest"]["actions"]}
    result["nextStep"] = _next_step("INSTALL_OLD_WHEEL_AND_VERIFY", project=plan["root"],
                                      wheel=plan["oldWheel"],
                                      withCodex=plan["manifest"]["withCodex"],
                                      rollbackManifest=plan["manifestPath"])
    return result


def upgrade_project(path, wheel_path=None, with_codex=False, check=False, rollback_manifest=None):
    """Check, execute, or exactly roll back a bounded upgrade to 0.3.0b1."""
    if bool(wheel_path) == bool(rollback_manifest):
        return _upgrade_refused("CHECK" if check else "UPGRADE", _project_root(path),
                                 "exactly one of wheel or rollback manifest is required",
                                 next_step=_next_step("STOP_INVALID_INVOCATION", project=_project_root(path)))
    if rollback_manifest:
        operation = "CHECK" if check else "ROLLBACK"
        plan = _load_rollback(path, rollback_manifest, operation)
        if check or plan["result"]["status"] == "REFUSED":
            return plan["result"]
        return _write_rollback(plan)
    operation = "CHECK" if check else "UPGRADE"
    plan = _upgrade_preflight(path, wheel_path, with_codex, operation)
    if check or plan["result"]["status"] in ("NO_OP", "REFUSED", "BLOCKED"):
        return plan["result"]
    try:
        return _write_upgrade(plan)
    except Exception as exc:
        return _upgrade_refused(
            "UPGRADE", plan["root"], "upgrade write failed and was compensated: {0}".format(exc),
            _identity_from_config(plan["config"]), dict(BUILD_IDENTITY),
            evidence=plan["result"]["evidence"],
            next_step=_next_step("RETRY_UPGRADE_CHECK", project=plan["root"],
                                 wheel=plan["targetWheel"], withCodex=plan["withCodex"]),
        )


def migrate(path, check=False):
    root = _project_root(path)
    config, database = _load_config(root)
    _validate_project_contract(root)
    _validate_identity(config, database)
    connection = open_database(database)
    try:
        claims = connection.execute("SELECT count(*) FROM claims WHERE status='ACTIVE'").fetchone()[0]
        writers = connection.execute("SELECT count(*) FROM repository_locks WHERE status='ACTIVE'").fetchone()[0]
        installed = schema_installed(connection)
    finally:
        connection.close()
    pending = [] if installed else [USAGE_SCHEMA_VERSION]
    result = {"current": SCHEMA_VERSION, "target": USAGE_SCHEMA_VERSION,
              "pending": pending, "activeClaims": claims, "activeWriters": writers}
    if check:
        return result
    if claims or writers:
        raise LiteError("migrate refuses active claim or repository writer")
    if installed:
        result["status"] = "no-op"
        return result
    result["backup"] = backup(root)["backup"]
    connection = open_database(database)
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + usage_schema_sql() + "\nCOMMIT;")
        if not schema_installed(connection):
            raise LiteError("usage migration did not install its schema marker")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    result["pending"] = []
    result["status"] = "ok"
    return result


def _codex_targets(root):
    return {
        os.path.join(root, ".codex", "skills", "awb-orchestrator", "SKILL.md"): "codex/skills/awb-orchestrator/SKILL.md",
        os.path.join(root, ".codex", "skills", "awb-orchestrator", "references",
                     "upgrade-and-rollback.md"): "codex/skills/awb-orchestrator/references/upgrade-and-rollback.md",
        os.path.join(root, ".codex", "agents", "planner.toml"): "codex/agents/planner.toml",
        os.path.join(root, ".codex", "agents", "implementer.toml"): "codex/agents/implementer.toml",
        os.path.join(root, ".codex", "agents", "reviewer.toml"): "codex/agents/reviewer.toml",
        os.path.join(root, ".codex", "agents", "convergence-reviewer.toml"): "codex/agents/convergence-reviewer.toml",
        os.path.join(root, ".codex", "agents", "fast-worker.toml"): "codex/agents/fast-worker.toml",
    }


def codex_install(path):
    root = _project_root(path)
    targets = _codex_targets(root)
    if any(os.path.lexists(target) for target in targets):
        raise LiteError("codex install refuses existing managed target")
    created = []
    try:
        for target, resource in targets.items():
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(_resource(resource))
            created.append(target)
    except Exception:
        for target in reversed(created):
            if os.path.exists(target):
                os.unlink(target)
        raise
    return {"status": "ok", "files": sorted(os.path.relpath(path, root) for path in created)}


def codex_check(path):
    root = _project_root(path)
    _validate_project_contract(root)
    missing = [os.path.relpath(target, root) for target in _codex_targets(root) if not os.path.isfile(target)]
    if missing:
        raise LiteError("Codex templates missing: " + ", ".join(sorted(missing)))
    with open(os.path.join(root, ".codex", "skills", "awb-orchestrator", "SKILL.md"), "r", encoding="utf-8") as handle:
        skill = handle.read()
    if ("AGENTS.md" not in skill or ".awb/project.md" not in skill or
            "convergence-reviewer" not in skill or "human" not in skill.lower() or
            "references/upgrade-and-rollback.md" not in skill or "PREFLIGHT_FIRST" not in skill):
        raise LiteError("Codex Skill contract is incomplete")
    with open(os.path.join(root, ".codex", "skills", "awb-orchestrator", "references",
                           "upgrade-and-rollback.md"), "r", encoding="utf-8") as handle:
        runbook = handle.read()
    if "AWB-UPGRADE-RUNBOOK-v1" not in runbook or "RESULT_BLOCKED" not in runbook:
        raise LiteError("Codex upgrade runbook contract is incomplete")
    with open(os.path.join(root, ".codex", "agents", "convergence-reviewer.toml"), "r", encoding="utf-8") as handle:
        convergence = handle.read()
    if "gpt-5.6-sol" not in convergence or "CONVERGENCE_REVISE" not in convergence:
        raise LiteError("convergence reviewer contract is incomplete")
    return {"status": "ok"}


def _row_hash(row):
    return _sha(_json(row))


def transfer_export(database, work_item_ids, destination):
    source = open_database(database)
    try:
        active = source.execute("SELECT count(*) FROM claims WHERE work_item_id IN ({0}) AND status='ACTIVE'".format(
            ",".join("?" for _ in work_item_ids)), work_item_ids).fetchone()[0]
        writers = source.execute("SELECT count(*) FROM repository_locks WHERE work_item_id IN ({0}) AND status='ACTIVE'".format(
            ",".join("?" for _ in work_item_ids)), work_item_ids).fetchone()[0]
        if active or writers:
            raise LiteError("transfer export refuses active claim or writer")
        bundle = {"schemaVersion": SCHEMA_VERSION, "bundleId": "bundle-" + uuid.uuid4().hex,
                  "sourceDatabaseId": _sha(os.path.realpath(database)),
                  "workItemIds": sorted(work_item_ids), "tables": {}}
        for table in TABLES:
            rows = [dict(row) for row in source.execute(
                "SELECT * FROM {0} WHERE work_item_id IN ({1}) ORDER BY rowid".format(table, ",".join("?" for _ in work_item_ids)), work_item_ids)]
            bundle["tables"][table] = rows
        if schema_installed(source):
            bundle["usageSchemaVersion"] = USAGE_SCHEMA_VERSION
            for table in USAGE_TABLES:
                bundle["tables"][table] = [dict(row) for row in source.execute(
                    "SELECT * FROM {0} WHERE work_item_id IN ({1}) ORDER BY rowid".format(
                        table, ",".join("?" for _ in work_item_ids)), work_item_ids)]
        bundle["eventWatermark"] = max([row["event_id"] for row in bundle["tables"]["events"]] or [0])
        encoded = _json(bundle)
        bundle["sha256"] = _sha(encoded)
    finally:
        source.close()
    with open(destination, "w", encoding="utf-8") as handle:
        json.dump(bundle, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    return {"status": "ok", "bundle": destination, "sha256": bundle["sha256"]}


def _bundle(path):
    with open(path, "r", encoding="utf-8") as handle:
        bundle = json.load(handle)
    expected = bundle.pop("sha256", None)
    actual = _sha(_json(bundle))
    if expected != actual or bundle.get("schemaVersion") != SCHEMA_VERSION:
        raise LiteError("invalid transfer bundle")
    bundle["sha256"] = expected
    return bundle


def transfer_import(database, bundle_path, check=False):
    bundle = _bundle(bundle_path)
    target = open_database(database)
    try:
        target.execute("BEGIN IMMEDIATE")
        changes = []
        usage_tables = USAGE_TABLES if bundle.get("usageSchemaVersion") else ()
        if usage_tables and not schema_installed(target):
            raise LiteError("transfer bundle contains usage events; run awb migrate first")
        for table in TABLES + usage_tables:
            for row in bundle["tables"].get(table, []):
                key = {"work_items": "work_item_id", "tasks": "task_id", "claims": "claim_id",
                       "repository_locks": "lock_id", "reviews": "review_id", "human_gates": "gate_id",
                       "events": "event_id", "usage_events": "usage_event_id"}[table]
                existing = target.execute("SELECT * FROM {0} WHERE {1}=?".format(table, key), (row[key],)).fetchone()
                if existing is not None and _row_hash(dict(existing)) != _row_hash(row):
                    raise LiteError("transfer conflict in {0}:{1}".format(table, row[key]))
                if existing is None:
                    changes.append((table, row))
        if check:
            target.rollback()
            return {"status": "check", "bundle": bundle["bundleId"], "pendingRows": len(changes)}
        for table, row in changes:
            columns = sorted(row)
            target.execute("INSERT INTO {0} ({1}) VALUES ({2})".format(
                table, ",".join(columns), ",".join("?" for _ in columns)), [row[column] for column in columns])
        target.commit()
        return {"status": "no-op" if not changes else "ok", "bundle": bundle["bundleId"], "insertedRows": len(changes)}
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()
