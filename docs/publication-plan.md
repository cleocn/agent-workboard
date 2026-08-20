# Agent Workboard v0.2.0 publication and migration contract

Status: approved release contract; candidate evidence is recorded separately.

- publication target: `https://github.com/cleocn/agent-workboard`, public,
  `main`, `v0.2.0`
- public Git identity: `cleocn <778219+cleocn@users.noreply.github.com>`
- license: MIT, `Copyright (c) 2026 cleocn`

The 0.2.0 commit must be the direct normal successor of the verified public
`main` tip and preserve the immutable `v0.1.0` tag/history. Orphan roots,
force/non-fast-forward publication, and private internal history are forbidden.
Every reachable object and every changed path is checked before publication.
The GitHub Release contains the wheel, sdist, `SHA256SUMS`, and release notes.
The exact-tagged build freezes version/commit/tree/tag identity into both
artifacts; the sdist can build without Git and carries the same seven canonical
templates as the wheel. Active consumer migration remains separately gated.
