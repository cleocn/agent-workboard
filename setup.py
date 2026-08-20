import json
import os
import subprocess

from setuptools import find_packages, setup
from setuptools.command.build_py import build_py as _build_py


def _git(*args):
    return subprocess.check_output(["git"] + list(args), cwd=os.path.dirname(os.path.abspath(__file__))).decode("ascii").strip()


class build_py(_build_py):
    def run(self):
        if _git("status", "--porcelain"):
            raise RuntimeError("release build requires a clean Git worktree")
        commit = _git("rev-parse", "HEAD")
        tree = _git("rev-parse", "HEAD^{tree}")
        tag = _git("describe", "--exact-match", "--tags", "HEAD")
        if tag != "v0.1.0":
            raise RuntimeError("release build requires exact v0.1.0 tag")
        _build_py.run(self)
        target = os.path.join(self.build_lib, "agent_workboard", "_build.py")
        with open(target, "w") as handle:
            handle.write("BUILD_IDENTITY = " + repr({
                "packageVersion": "0.1.0", "sourceCommit": commit,
                "sourceTree": tree, "sourceTag": tag,
            }) + "\n")


setup(
    name="agent-workboard",
    version="0.1.0",
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
    cmdclass={"build_py": build_py},
)
