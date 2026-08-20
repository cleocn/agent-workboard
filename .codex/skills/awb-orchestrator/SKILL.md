---
name: awb-orchestrator
description: Run a local Agent Workboard workflow for the current project.
---

# Agent Workboard orchestrator

Read the project `AGENTS.md` and `.awb/project.md` first.  Use the installed
`awb` command; do not edit SQLite directly.  Create one top-level WorkItem,
then keep the order planner → independent reviewer → human plan gate →
implementer → independent reviewer → human final gate.  Do not replace a
reviewer or human gate, and do not perform remote actions without explicit
approval.

This generic template has no absolute paths and does not assume any product,
repository, deployment environment, or WorkItem naming policy beyond the
MVP-LITE compatible types.

