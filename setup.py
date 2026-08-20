import ast
import gzip
import json
import os
import shutil
import subprocess
import tarfile

from setuptools import find_packages, setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist


ROOT = os.path.dirname(os.path.abspath(__file__))
IDENTITY_FILE = ".awb-release-identity.json"
TEMPLATE_FILES = (
    "README.md",
    "awb-template.md",
    "fe-template.md",
    "remediation-plan-template.md",
    "test-issue-template.md",
    "test-issue-trigger-rules.md",
    "wa-template.md",
)


def _source_version():
    with open(os.path.join(ROOT, "src", "agent_workboard", "__init__.py"), "r") as handle:
        module = ast.parse(handle.read())
    for statement in module.body:
        if isinstance(statement, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "__version__" for target in statement.targets):
                return ast.literal_eval(statement.value)
    raise RuntimeError("package version is missing")


VERSION = _source_version()
EXPECTED_TAG = "v" + VERSION


def _git(*args):
    return subprocess.check_output(["git"] + list(args), cwd=ROOT).decode("ascii").strip()


def _identity_from_git():
    if _git("status", "--porcelain"):
        raise RuntimeError("release build requires a clean Git worktree")
    tags = _git("tag", "--points-at", "HEAD").splitlines()
    if tags != [EXPECTED_TAG]:
        raise RuntimeError("release build requires the unique exact {0} tag".format(EXPECTED_TAG))
    return {
        "packageVersion": VERSION,
        "sourceCommit": _git("rev-parse", "HEAD"),
        "sourceTree": _git("rev-parse", "HEAD^{tree}"),
        "sourceTag": EXPECTED_TAG,
    }


def _read_frozen_identity():
    envelope = os.path.join(ROOT, IDENTITY_FILE)
    if not os.path.isfile(envelope):
        raise RuntimeError("release build requires Git or a frozen sdist identity envelope")
    with open(envelope, "r") as handle:
        identity = json.load(handle)
    required = ("packageVersion", "sourceCommit", "sourceTree", "sourceTag")
    if sorted(identity) != sorted(required):
        raise RuntimeError("sdist identity envelope is incomplete")
    if identity["packageVersion"] != VERSION or identity["sourceTag"] != EXPECTED_TAG:
        raise RuntimeError("sdist identity version or tag is invalid")
    for key in ("sourceCommit", "sourceTree"):
        value = identity[key]
        if not isinstance(value, str) or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
            raise RuntimeError("sdist identity {0} is invalid".format(key))
    build_path = os.path.join(ROOT, "src", "agent_workboard", "_build.py")
    with open(build_path, "r") as handle:
        statement = handle.read().split("=", 1)[1].strip()
    if ast.literal_eval(statement) != identity:
        raise RuntimeError("sdist identity and frozen _build.py disagree")
    return identity


def _release_identity():
    return _identity_from_git() if os.path.isdir(os.path.join(ROOT, ".git")) else _read_frozen_identity()


def _write_identity(path, identity):
    frozen = {key: identity[key] for key in
              ("packageVersion", "sourceCommit", "sourceTree", "sourceTag")}
    with open(path, "w") as handle:
        handle.write("BUILD_IDENTITY = " + repr(frozen) + "\n")


class build_py(_build_py):
    def run(self):
        identity = _release_identity()
        _build_py.run(self)
        _write_identity(os.path.join(self.build_lib, "agent_workboard", "_build.py"), identity)
        target = os.path.join(self.build_lib, "agent_workboard", "resources", "work_item_templates")
        os.makedirs(target, exist_ok=True)
        for name in TEMPLATE_FILES:
            source = os.path.join(ROOT, "docs", "work-item-templates", name)
            if not os.path.isfile(source):
                raise RuntimeError("required WorkItem template is missing: " + name)
            shutil.copyfile(source, os.path.join(target, name))


class sdist(_sdist):
    def make_release_tree(self, base_dir, files):
        identity = _identity_from_git()
        _sdist.make_release_tree(self, base_dir, files)
        with open(os.path.join(base_dir, IDENTITY_FILE), "w") as handle:
            json.dump(identity, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        _write_identity(os.path.join(base_dir, "src", "agent_workboard", "_build.py"), identity)
        template_target = os.path.join(base_dir, "docs", "work-item-templates")
        os.makedirs(template_target, exist_ok=True)
        for name in TEMPLATE_FILES:
            shutil.copyfile(os.path.join(ROOT, "docs", "work-item-templates", name),
                            os.path.join(template_target, name))

    def make_archive(self, base_name, fmt, root_dir=None, base_dir=None, owner=None, group=None):
        if fmt != "gztar" or root_dir is not None or not base_dir:
            raise RuntimeError("release sdist requires the deterministic gztar format")
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "946684800"))
        archive_path = base_name + ".tar.gz"
        os.makedirs(self.dist_dir, exist_ok=True)
        with open(archive_path, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
                with tarfile.open(mode="w", fileobj=compressed, format=tarfile.PAX_FORMAT) as archive:
                    for directory, directories, names in os.walk(base_dir):
                        directories.sort()
                        names.sort()
                        for path in [os.path.join(directory, name) for name in directories + names]:
                            info = archive.gettarinfo(path, arcname=path)
                            info.uid = info.gid = 0
                            info.uname = info.gname = ""
                            info.mtime = epoch
                            if info.isfile():
                                with open(path, "rb") as handle:
                                    archive.addfile(info, handle)
                            else:
                                archive.addfile(info)
        return archive_path


setup(
    name="agent-workboard",
    version=VERSION,
    description="Local-first Agent Workboard",
    python_requires=">=3.7",
    package_dir={"": "src"},
    packages=find_packages("src"),
    package_data={"agent_workboard": ["resources/*/*", "resources/*/*/*", "resources/*/*/*/*"]},
    entry_points={"console_scripts": ["awb=agent_workboard.cli:main"]},
    url="https://github.com/cleocn/agent-workboard",
    project_urls={"Source": "https://github.com/cleocn/agent-workboard"},
    license="MIT",
    classifiers=["License :: OSI Approved :: MIT License"],
    cmdclass={"build_py": build_py, "sdist": sdist},
)
