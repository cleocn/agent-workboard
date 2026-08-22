# AWB-011 release evidence contract

Protocol: `AWB-011-RELEASE-EVIDENCE-v1`

This document defines the evidence required for Agent Workboard 0.3.0b1 Preview;
it does not assert that publication has occurred.

## Frozen candidate

- base commit: `c7d210db9cb4fb59d8e263277c74d0c040b40d7c`
- base tree: `d24b1e77adff3ed01d26a6d99e7ea11d51263588`
- branch: `release/awb-011-v0.3.0b1`
- annotated tag: `v0.3.0b1`
- shape: one normal commit whose sole parent is the frozen base
- scope: the exact 27-path allowlist approved in the AWB-011 management contract

## Local review evidence

The implementation submission and independent review must record the exact
candidate commit/tree/tag object, all 27 changed paths, Python 3.7 full and
targeted tests, privacy sentinel, exact 0.2.1 upgrade/rollback and refusal tests,
fresh install, two clean reproducible builds, no-Git sdist-to-wheel identity,
manifest/archive checks, reachable-object secret/path scan and `git diff
--check`.

`SHA256SUMS` contains only the sorted wheel and sdist hashes. Dynamic commit,
tree, tag object, artifact hashes and draft release-body hash belong in the AWB
event evidence and generated checksum/body files, not in this tracked document;
this avoids a self-hash cycle.

## Publication boundary

Remote refs, GitHub Release and assets may be written only after an independent
IMPLEMENTATION PASS with zero open Findings. The release must be a prerelease
named `Agent Workboard 0.3.0b1 Preview`, upload only wheel, sdist and
`SHA256SUMS`, then pass downloaded-asset hash and ref/tag postflight. No PyPI,
telemetry, real consumer migration, AWB-010 cohort operation or automated
control is authorized.

Public text must state Preview, opt-in, local-only and observation-only; that
`estimatedCredits` are not the ChatGPT/Codex weekly bill; that `codex-local`
uses an observed local schema and fails closed; and that AWB-010 long-term
observation is not complete.
