# Agent Workboard Preview publication and migration contract

Status: approved release contract; candidate evidence is recorded separately.

- publication target: `https://github.com/cleocn/agent-workboard`, public,
  `main`, current approved Preview tag
- public Git identity: `cleocn <778219+cleocn@users.noreply.github.com>`
- license: MIT, `Copyright (c) 2026 cleocn`

The Preview commit must be the direct normal successor of the verified public
`main` tip and preserve immutable prior tag/history. Orphan roots,
force/non-fast-forward publication, and private internal history are forbidden.
Preview publication checks one clean direct successor, its exact changed-path
allowlist, one full test/build/fresh install, artifact members and secrets,
three-asset SHA-256, remote drift, and an independent Implementation review.
It does not require per-file manifest hashes, double-build/no-Git
reproducibility, Git reachable-object closure, duplicate postflight files, or a
Release-body hash. Stable releases may choose stricter supply-chain gates.
The GitHub Release contains the wheel, sdist, `SHA256SUMS`, and release notes.
The exact-tagged build freezes version/commit/tree/tag identity into both
artifacts; the sdist can build without Git and carries the same seven canonical
templates as the wheel. Active consumer migration remains separately gated.

## Review-before-publication order

Release WorkItems explicitly opt in by preparing a managed candidate. The
candidate is frozen to its direct-successor commit/tree/tag, exact changed-path
allowlist and source fingerprint, then built once with the declared pinned
offline toolchain. The Implementation submission and independent review bind
that exact FROZEN+BUILT fingerprint. PASS/open0 creates `PUBLICATION_READY` and
does not execute FINAL or close the Orchestrator lease.

Remote publication still requires a HUMAN authorization envelope for the exact
repository, version, refs, candidate fingerprint and three assets. AWB exposes
only local authorization/status/postflight audit commands; the external
Publication Operator cannot change candidate bytes. Authorization also binds
the creation decision actor, prepublication attestation, formal review,
`PUBLICATION_READY` and build fingerprints. `publication status
--evidence-file` validates the content-addressed evidence core and exact
already-published remote state before returning
`SUBMIT_EXACT_PUBLICATION_POSTFLIGHT`. Exact successful postflight delays
AUTO/MANUAL FINAL until all bindings, remote refs, immutable prerelease and the
three public asset hashes match. Partial or ambiguous remote state fails closed
and never permits force, overwrite or delete recovery.

Candidate storage uses same-filesystem `active`, `staging`, `quarantine` and
`journal` sibling roots. Quarantine moves are recoverable; finalize retains all
source and artifact bytes. Build toolchain mismatch returns
`TOOLCHAIN_NOT_READY` without network access or automatic installation.
