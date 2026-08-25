# Agent Workboard Preview publication and migration contract

Status: approved release contract; candidate evidence is recorded separately.

- publication target: `https://github.com/cleocn/agent-workboard`, public,
  `main`, current approved Preview tag
- public Git identity: `cleocn <778219+cleocn@users.noreply.github.com>`
- license: MIT, `Copyright (c) 2026 cleocn`

The Preview commit must be the direct normal successor of the verified public
`main` tip and preserve immutable prior tag/history. Orphan roots,
force/non-fast-forward publication, and private internal history are forbidden.
Preview publication uses the effective policy and registered checks selected by
`agent_workboard.verify`; this contract does not duplicate their list or counts.
The current managed candidate receipt and independent Implementation review are
mandatory. Stable releases may request a stricter verifier policy.
The GitHub Release contains the wheel, sdist, `SHA256SUMS`, and release notes.
The exact-tagged build freezes version/commit/tree/tag identity into both
artifacts; the sdist can build without Git and carries the same seven canonical
templates as the wheel. Active consumer migration remains separately gated.

## Review-before-publication order

Release WorkItems explicitly opt in by preparing a managed candidate. The
candidate lifecycle records its source and build identity through package-owned
commands. The Implementation submission and independent review bind that exact
managed candidate and `AWB-VERIFY-RECEIPT-v1`. PASS/open0 creates `PUBLICATION_READY` and
does not execute FINAL or close the Orchestrator lease.

Remote publication still requires a HUMAN authorization envelope for the exact
repository, version, refs, candidate fingerprint and three assets. AWB exposes
only local authorization/status/postflight audit commands; the external
Publication Operator cannot change candidate bytes. Authorization also binds
the creation decision actor, prepublication attestation, formal review,
`PUBLICATION_READY` and build fingerprints. The read-only historical
`publication status --evidence-file` adapter may inspect an existing b7 record.
New remote facts are supplied on stdin to `awb verify run --phase
POST_PUBLICATION`; the managed runtime validates them and extends the current
receipt without changing its core. Exact successful post-publication verification delays
AUTO/MANUAL FINAL until all bindings, remote refs, immutable prerelease and the
three public asset hashes match. Partial or ambiguous remote state fails closed
and never permits force, overwrite or delete recovery.

Candidate storage uses same-filesystem `active`, `staging`, `quarantine` and
`journal` sibling roots. Quarantine moves are recoverable; finalize retains all
source and artifact bytes. Build toolchain mismatch returns
`TOOLCHAIN_NOT_READY` without network access or automatic installation.
