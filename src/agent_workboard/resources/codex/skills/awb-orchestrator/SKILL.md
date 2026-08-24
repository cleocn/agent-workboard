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

Run `awb workflow check <WorkItem>` before resuming an item whose projection is
uncertain. The check is read-only and content-free. Consume a deterministic
repair only through the returned exact action, fingerprint and request id; an
ambiguous result remains `WAITING_HUMAN`. Never substitute the legacy review
task recovery command or edit projection tables directly.

For PLAN and IMPLEMENTATION separately, route a strict serial 3+1+1 review.
New plans opt in with `submit_plan --plan-artifact <project-relative-path>`.
For an opt-in PLAN, each round uses a fresh Reviewer and the latest artifact
editor cannot review that revision.  Rounds 1-3 may PASS, use the package-owned
`review --replacement-file` path to AMEND only an allowed non-material defect,
or return a material change to the Planner as REVISE_TO_PLANNER.  Either R3
AMENDED or R3 REVISE_TO_PLANNER routes directly to the single round-4
`convergence-reviewer`.  Round 4 may make one minimal AMENDED change and then
routes to a fresh ordinary round 5.  Round 5 permits only PASS or WAITING_HUMAN
and never creates round 6.  A Reviewer never edits files directly or acquires a
generic writer; PASS is read-only.  Historical plans without the artifact
envelope retain the read-only AWB-REVIEW-v1 behavior.  IMPLEMENTATION review is
unchanged: its Reviewer never edits the product and the existing 3+1+1
REVISE/CONVERGENCE_REVISE route remains authoritative.  Never reset counts,
alter the ID, or create a replacement WorkItem to evade the cap.
Reviewer claims atomically manage the Reviewer task from claim through review;
never issue a separate Reviewer `task --status` mutation. Planner and
Implementer submission should use `workflow advance`, which atomically completes
the task and releases its exact claim/writer bundle.

Reviewer feedback is evidence, not authority to expand scope.  Route only valid
blocking Findings to authors.  On exhaustion, project the Finding disagreement
table, passed acceptance/tests, open Findings, consumed rounds, risks of changing
or keeping the result, planning impact, and one next step; neither orchestrator
nor reviewer decides for the human.

Read `.awb/config.json` `usagePolicy` before Usage orchestration; a missing field
means `OFF`.  Under `OFF`, do not bind sessions, create spans, run mutation
`usage sync/show`, refresh periodically, or use Usage coverage/credits/quota as
a gate.  Explicit historical `usage show/export/self-check` remains available.
Under `BEST_EFFORT`, preserve the existing binding/span/sync privacy behavior;
failures remain diagnostic and do not authorize displaying prompt, response,
tool output, credentials, session content, or local session paths.

Treat `awb doctor` and `awb activity list/show` as the effective activity
projection. LIVE resources must be released by their exact owner. STALE rows
remain visible history and may be reconciled only through the exact public
request-id/owner/generation command. A true FINAL transition closes all activity
atomically; BLOCKED, WAITING_HUMAN and nonterminal HELD never imply cleanup.
For exact b3/b4/b5/b6 upgrades, consume the single recovery-aware b7 nextStep unchanged;
never ask the user to edit SQLite or perform a separate stale cleanup first.
The b7 target owns the frozen `AWB-MIGRATION-GRAPH-v1`; never infer a route from
version ordering or install intermediate releases.

Runtime events are the process audit authority.  Do not require per-round
submission JSON, per-round quality hashes, duplicate postflight files, or a
Release body hash.  A normal WorkItem retains one final implementation summary;
a release WorkItem retains one final release postflight.  A Preview gate runs
one clean build, one full test, one fresh wheel install, exact changed-path
allowlist, wheel/sdist secret and member scan, three-asset SHA-256, remote drift
check, and independent Implementation review.  Preview does not require a
second reproducibility build, a no-Git rebuild, per-file manifest hashes, or Git
reachable-object/history closure.  Remote/destructive authority is unchanged.

For an explicit Release WorkItem, prepare, freeze and build only through `awb
candidate`.  Independent IMPLEMENTATION PASS/open0 produces
`PUBLICATION_READY` instead of FINAL; exact HUMAN candidate authorization and
accepted publication postflight are still required before FINAL.  A failed
publication with proven `partialState=NONE` may use the HUMAN retry command only
when the retained IMPLEMENTATION history has a legal fresh ordinary R2 or R3;
R3/R4/R5 and ambiguous or partial remote state stop at HUMAN without resetting
review counts.

Use `awb workflow status` before `awb workflow advance`.  Consume exactly its
single fingerprinted `LOCAL_SAFE` nextStep and expected row version.  Never use
advance for HUMAN, remote, destructive, candidate, publication, upgrade,
rollback or delete work.  MVP-LITE mutations return a compact
`AWB-MUTATION-RECEIPT-v1` by default; request `--full` only when the complete
projection is actually needed.  Managed candidates keep active, staging,
quarantine and journal as same-filesystem sibling roots; quarantine is
recoverable and neither quarantine nor finalize deletes bytes.  Pinned build
toolchain drift is a refusal, not permission to install or download tools.

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
