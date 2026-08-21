# AWB-007 release evidence contract

Protocol: `AWB-007-RELEASE-EVIDENCE-v1`

This document defines the evidence required to publish Agent Workboard 0.2.1.
It is not proof that publication has occurred. Final commit, tree, artifact and
remote digests belong in `SHA256SUMS`, the immutable GitHub Release body and the
AWB-007 event record so this tracked file never hashes itself.

## Frozen inputs

- Public base: `e813397bce0f75ba70176608a7ca9a50fa2f26ea`
- Public base tree: `d47fea22c9f9000bdb4d44e6682336ab348caf06`
- Preserved v0.1.0 commit: `133365c0cd7583f2cadf21f10dcc82d9fc7bba7f`
- AWB-006 nine-path content-list SHA-256:
  `5c1ff3126c723bd3317418a40710d62ee3a06fdc75f226d75e71efdc06f9386a`
- Candidate branch: `release/awb-007-v0.2.1`
- Candidate tag: one annotated `v0.2.1`

## Supported upgrade matrix

Only the exact released 0.1.0 and 0.2.0 build identities may upgrade to the
exact running 0.2.1 identity. Exact same-identity input is a zero-write no-op.
Every other version, tag, commit, tree or wheel identity is refused without
project, database, Codex or backup-tree writes.

## Required local evidence

- The candidate is a direct single-parent successor of the public base and
  changes exactly the approved 18 paths.
- Python 3.7 full regression and the targeted project, release-build and
  release-gate suites pass without skipped or weakened tests.
- Two fresh exact-tag builds produce identical wheel and sdist SHA-256 values.
- A no-Git sdist builds a wheel with the same frozen build identity and the
  same `AWB-UPGRADE-RUNBOOK-v1` bytes.
- Disposable 0.1.0 and 0.2.0 consumers pass check, upgrade, doctor, Codex check,
  exact rollback and restored-old-wheel verification; refusal and failure
  compensation paths remain zero-write or produce bounded recovery material.
- Manifest, archive-path, reachable-object secret and `git diff --check` gates
  pass, and source/tag identities for v0.1.0 and v0.2.0 remain unchanged.

## Required publication evidence

After independent implementation review and explicit remote authorization,
record the exact candidate commit/tree, annotated tag object, wheel/sdist/
`SHA256SUMS` digests, immutable GitHub Release identifier and downloaded asset
digests. Push only the reviewed branch-to-main and tag refspecs without force.
PyPI publication and real-consumer migration remain out of scope.
