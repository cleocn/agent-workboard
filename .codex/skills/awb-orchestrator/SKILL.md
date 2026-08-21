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

When an existing managed project has `.awb/config.json` and its package lock,
installed AWB version, requested target wheel, or prior upgrade result indicates
that an upgrade or rollback may be needed, read the adjacent
`references/upgrade-and-rollback.md` (`AWB-UPGRADE-RUNBOOK-v1`) completely before
acting.  Follow its `PREFLIGHT_FIRST` route: check before every write, stop on
`REFUSED` or `BLOCKED`, and consume exactly the one structured `nextStep` in the
`AWB-UPGRADE-v1` result.  Also read it before interpreting a rollback manifest,
recovering an interrupted rollback, or advising that an unsupported version pair
can proceed.

Every new WorkItem must include a validated `AWB-WORKITEM-MGMT-v1` envelope and
continuous T01+ task board at create time.  Historical records must be backfilled
through the public command before reopened or materially expanded.  Treat SQLite
show/timeline projections as runtime authority; Markdown templates are creation
and planning inputs, not a second live state.

For PLAN and IMPLEMENTATION separately, route a strict serial 3+1+1 review:
rounds 1–3 use the ordinary reviewer.  After round 3 REVISE, freeze the artifact
and route exactly one round-4 `convergence-reviewer`; do not return it to the
author first.  Only CONVERGENCE_REVISE permits one minimal author revision, then
round 5 returns to the ordinary reviewer for the convergence close conditions and
direct regressions only.  A round-5 REVISE, convergence WAITING_HUMAN, or true
BLOCKED stops automation.  Never reset counts, switch reviewers, resubmit the
same artifact, alter the ID, or create a replacement WorkItem to evade the cap.

Reviewer feedback is evidence, not authority to expand scope.  Route only valid
blocking Findings to authors.  On exhaustion, project the Finding disagreement
table, passed acceptance/tests, open Findings, consumed rounds, risks of changing
or keeping the result, planning impact, and one next step; neither orchestrator
nor reviewer decides for the human.

This generic template has no absolute paths and does not assume any product,
repository, deployment environment, or WorkItem naming policy beyond the
MVP-LITE compatible types.
