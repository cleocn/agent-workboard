# AWB-004 release evidence contract

This document defines the reproducible evidence shipped with Agent Workboard
0.2.0. Exact commit, tree, artifact hashes, and command results are generated
from the clean `v0.2.0` tag and published in `SHA256SUMS` and the GitHub Release;
they are deliberately not copied into this tracked file because changing this
file would change the identity it describes.

## Public lineage

- Repository: `https://github.com/cleocn/agent-workboard`
- Verified base before implementation: `568e05414242ad5fbc4381752cc784a909305885`
- Preserved release: `v0.1.0` at commit `133365c0cd7583f2cadf21f10dcc82d9fc7bba7f`
- Candidate rule: one direct, fast-forward successor commit; no orphan, force,
  unrelated history, or changed path outside the reviewed allowlist.

## Required candidate checks

1. Python 3.7 full unit suite and release-specific negative tests.
2. Unique exact `v0.2.0` tag, clean tree, and version/tag agreement.
3. Two wheel/sdist builds with identical per-format SHA-256 values.
4. Wheel and sdist identity equals the candidate commit/tree/tag.
5. A no-Git sdist-to-wheel install, CLI, init, doctor, Codex install/check, and
   bootstrap lifecycle from an unrelated directory.
6. Byte equality for the seven `docs/work-item-templates/` sources in the
   sdist and wheel, plus presence of active specifications and all five Agents.
7. A temporary v0.1.0 consumer upgrade, database/contract/Codex verification,
   rollback, and refusal tests for active writers/claims and customized Codex.
8. Public-lineage, changed-path, reachable-object, secret, and `git diff --check`
   gates.

## Scope-accounting note

The approved plan estimated 15–19 modified paths and at most two tracked new
files. Against the public 0.1.0 repository that estimate was inaccurate: the
already accepted AWB-005 inputs were not yet tracked publicly. Before release
documentation/manifest refresh, the candidate had 22 modified and 10 new
paths. Nine Git-new paths are explicit AC-003 inputs (two convergence-reviewer
files and seven canonical templates); the remaining new path is the AWB-004
release-build test. They add no unapproved feature, state, service, dependency,
or authorization. The final changed-path inventory is generated and reviewed
after documentation and manifest refresh; any path outside the approved list
is a release-gate failure.
