# Agent Workboard v0.1.0 publication and migration plan

Status: public planning summary only; implementation and final acceptance are
not represented here.

- publication target: `https://github.com/cleocn/agent-workboard`, public,
  `main`, `v0.1.0`
- public Git identity: `cleocn <778219+cleocn@users.noreply.github.com>`
- license: MIT, `Copyright (c) 2026 cleocn`

The public repository starts from a clean parentless root. Every reachable
object is scanned before publication; the immutable GitHub Release contains
source archives, a wheel, SHA256SUMS, and minimal release notes. A separate
human gate is required before any consumer or database cutover. Private
history, internal paths and approval records, secrets, runtime databases, and
unlicensed assets are excluded.
