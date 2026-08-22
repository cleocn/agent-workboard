---
name: awb-orchestrator
description: Run a local Agent Workboard workflow for the current project.
---

# Agent Workboard orchestrator

Read the project `AGENTS.md` and `.awb/project.md` first.  Use the installed
`awb` command; do not edit SQLite directly.  Create one top-level WorkItem,
then keep the order planner → independent reviewer → plan gate → implementer →
independent reviewer → final gate.  Never replace the independent Reviewer or
the strict review rounds.  A gate may be automatic only under the persisted
`AUTO_ON_PASS` policy and a runtime `AUTO_GATE_APPROVED` SYSTEM event; otherwise
it remains HUMAN.  Automatic gates never authorize remote, destructive,
publish, deploy, or delete actions.

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

Before every create, produce an `AWB-CREATION-RISK-v1` classification from the
actual requested scope, authorized actions, and observed runtime state.  Signals
are `REMOTE`, `DESTRUCTIVE`, or `ANOMALOUS_STATE`.  Negative declarations in
`outOfScope` or `authorization.forbidden` do not trigger by themselves; ambiguous
real state is `ANOMALOUS_STATE`.  With no signals, omit `--human-review` so a new
STANDARD WorkItem defaults to `AUTO_ON_PASS`, unless the user explicitly asked
for `manual`.  With any signal, stop before invoking create and ask the user to
choose exactly `manual` or `auto-on-pass`; unanswered means zero create and zero
side effect.  Pass the choice, risk file, and decision actor to the public CLI.
Choosing automatic review records the decision but grants no authority for the
risky action.  Existing or imported WorkItems without the policy remain MANUAL.

When the database reports `AWB-ORCHESTRATOR-v1`, use the public `awb
orchestrator` commands for coordinator ownership. Prefer explicit `claim
<WorkItem>`; use `claim-next` only for queue dispatch. Save the returned
generation, renew before expiry, and pass the exact `--orchestrator-id` and
`--orchestrator-generation` when acquiring a new Agent claim. After any lease
history, never omit the fence or reuse a released, expired, or stale generation.
One Orchestrator owns a WorkItem at a time, while the existing repository writer
lock remains authoritative across WorkItems. A recovered coordinator observes
an already-running Agent and never releases or impersonates it. AWB does not
spawn, supervise, kill, steer, or migrate host processes.

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

After every successful WorkItem state mutation, run `awb usage sync --project
<project> --work-item <WI>` and `awb usage show <WI> --project <project>
--group-by role --format table`.  Display only aggregate tokens, estimated
credits, quota, coverage, and sync-gap reasons.  Never display prompt, response,
tool output, credential, session content, or local session paths.  While this
main Agent remains active in a tool/Agent wait, refresh at a 300-second target on
a best-effort basis; missed intervals are not replayed and no offline timer,
daemon, runner, or heartbeat SLA is implied.

On Darwin, while actively managing one or more WorkItems, best-effort start the
foreground command `caffeinate -di` only through a long-running Agent tool
session.  The returned tool-session/cell handle is the sole ownership proof.
Share that one inhibitor across the active WorkItems and terminate it only after
the last active WorkItem stops because of completion, cancellation,
blocking/human wait, or before the main Agent ends its active turn.  Terminate and confirm only that
exact session; never use PID files, process-name scans, `pkill`, guessed PIDs, or
`pmset`.  If start/cleanup or ownership confirmation fails, emit
`POWER_INHIBIT_UNAVAILABLE` or `POWER_INHIBIT_CLEANUP_FAILED`, continue the AWB
workflow, and do not start another inhibitor until ownership is safe again.
Non-Darwin or a missing binary is an explicit best-effort degradation.  These
are Agent tool-session operations and Skill contract checks, not a product
runner or host-process-management API.

This generic template has no absolute paths and does not assume any product,
repository, deployment environment, or WorkItem naming policy beyond the
MVP-LITE compatible types.
