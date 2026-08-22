# AWB-015 release evidence contract

Protocol: `AWB-015-RELEASE-EVIDENCE-v1`

This document defines the evidence required for Agent Workboard 0.3.1b1
Preview; it does not assert that publication has occurred.

## Frozen candidate

- base commit: `e7616c97c8572ba2556e9d636365595424f6eccf`
- base tree: `bef05f7c608aafef8ca49bfcaf0b782cab4fb718`
- branch: `release/awb-015-v0.3.1b1`
- annotated tag: `v0.3.1b1`
- shape: one normal commit whose sole parent is the frozen base
- scope: the exact 29-path allowlist approved in the AWB-015 management contract

## Local review evidence

The implementation submission and independent review must record the exact
candidate commit, tree, annotated tag object, all 29 changed paths, preservation
of the 21 accepted AWB-012/AWB-014 inputs, Python 3.7 and current full tests,
the exact 0.3.0b1 upgrade/doctor/transfer/rollback matrix, privacy and secret
scans, fresh install, two clean reproducible builds, no-Git sdist-to-wheel
identity, manifest/archive checks, reachable-object scan, and `git diff --check`.

`SHA256SUMS` contains only the sorted wheel and sdist hashes. Dynamic commit,
tree, tag object, artifact hashes and Release-body hash belong in the AWB event
evidence and generated checksum/body files, not in this tracked document; this
avoids a self-hash cycle.

## Publication boundary

Remote refs, GitHub Release, and assets may be written only after an independent
IMPLEMENTATION PASS with zero open Findings. The release must be the prerelease
`Agent Workboard 0.3.1b1 Preview`, use the exact independently reviewed body,
upload only wheel, sdist, and `SHA256SUMS`, then pass downloaded-asset hash and
ref/tag postflight. No PyPI, real consumer migration, runner/daemon, remote
control, or additional repository setting is authorized.

Public text must state Preview, opt-in and local-only; exact 0.3.0b1 upgrade;
multi-Orchestrator coordination; AUTO_ON_PASS risk and independent-review
boundaries; best-effort usage and caffeinate limitations; no product runner;
and that `estimatedCredits` are not the ChatGPT/Codex weekly quota bill.
