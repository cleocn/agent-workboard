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
