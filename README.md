# Agent Workboard

Agent Workboard is a local-first SQLite workboard for a small, auditable
planner → reviewer → policy gate → implementer → reviewer workflow. It has no
runtime third-party dependency and its read-only board listens only on a
loopback address.

## Install the 0.3.1b8 Preview

This release is an opt-in, local-only, observation-only Preview. Download the
wheel, sdist and `SHA256SUMS` from the GitHub `v0.3.1b8` prerelease, verify both
artifacts, then initialize a project:

```bash
shasum -a 256 -c SHA256SUMS
python -m pip install agent_workboard-0.3.1b8-py3-none-any.whl
awb init --project ./my-project
awb doctor --project ./my-project
awb lite --project ./my-project list
```

`init` creates a managed `.awb/` directory and writes a hash-pinned local
wheel requirement. `bootstrap` creates a missing database; `doctor` checks the
project contract, package build identity, non-editable installation and SQLite
integrity before opening the board.

For a repository that consumes the published release, use its fixed
`requirements-awb.txt` and run:

```bash
python -m pip install --require-hashes -r .awb/requirements-awb.txt
awb bootstrap --project .
awb doctor --project .
```

Stable mode refuses editable or unverified packages. Development mode is only
for a disposable database under `.awb/dev/`.

## Upgrade an exact 0.3.1b3 through 0.3.1b7 project

Retain the exact old release wheel and install the verified 0.3.1b8 wheel. If the
project uses an official GitHub URL lock, place its verified source wheel beside
the Preview wheel; a local file lock continues to use its recorded path. Always
run the read-only preflight first, then execute only its one structured
`nextStep`:

```bash
awb upgrade --check --project ./my-project --wheel ./agent_workboard-0.3.1b8-py3-none-any.whl --with-codex
awb upgrade --project ./my-project --wheel ./agent_workboard-0.3.1b8-py3-none-any.whl --with-codex
awb doctor --project ./my-project
awb codex check --project ./my-project
```

Upgrade selects only the frozen exact b3→b4→b5→b6→b7→b8 migration graph. Each
supported source reaches b8 through one zero-write check and one execution. Its
zero-write check reports LIVE and STALE activity separately. Stale-only state
returns one `EXECUTE_UPGRADE_WITH_RECONCILIATION` action whose fingerprint and
request ID are passed unchanged to execute; backup and snapshot revalidation
occur before audited reconciliation. LIVE or mixed state refuses until its
owner releases normally. It also refuses an invalid database, identity drift,
missing or partial source extensions, and customized package-owned Codex files.
Execution first makes an online backup, then validates existing `AWB-USAGE-v1`,
`AWB-ORCHESTRATOR-v1` and `AWB-AUTO-GATE-v1` in one transaction without schema
DDL. Existing WorkItems preserve their policies. The bound rollback manifest includes the database;
any post-upgrade workflow or usage write makes rollback fail closed rather than discard data.
Use `awb upgrade --check --rollback <manifest>` before the exact rollback
command. Do not restore files or database tables by hand.

Use `awb activity list --project .` to inspect all effective activity, or
`awb activity show --kind orchestrator-lease --resource-id <id> --project .`.
The exact `reconcile-expired` command is a recovery primitive; normal consumers
do not need it for upgrade because b6 performs stale-only recovery internally.

The shipped Orchestrator Skill routes Agents to
`references/upgrade-and-rollback.md` (`AWB-UPGRADE-RUNBOOK-v1`) before upgrade,
rollback or recovery work. It preserves zero-write preflight, structured
one-next-step results and exact rollback material.

## WorkItem gate policy

After the additive `AWB-AUTO-GATE-v1` migration, a new STANDARD WorkItem
defaults to `AUTO_ON_PASS`; an explicit opt-out persists `MANUAL`. Before create,
the Orchestrator Skill classifies actual remote, destructive, and anomalous-state
risk. Any signal requires a user choice between the two policies, and no answer
means no WorkItem is created. The choice is audited but never authorizes the
risky action.

Independent Reviewer and strict 3+1+1 review remain mandatory. Only the latest
PASS with zero open Findings and a current candidate-bound verifier receipt can produce a
`SYSTEM/AUTO_GATE_APPROVED` event. Failures and drift fall back to human
handling; no HUMAN record is fabricated.

New PLAN artifacts may opt in with `submit_plan --plan-artifact`. An opt-in PLAN
Reviewer can apply a bounded non-material correction only through the
package-owned `review --replacement-file` path. The amendment consumes its
round, its editor cannot approve the revision, and the next round requires a
fresh Reviewer. PLAN remains strict 3+1+1 with no round 6. Implementation
review remains read-only and unchanged.

## Efficient local workflow and release candidates

`awb workflow status` returns one fingerprinted next step. `awb workflow
advance` consumes only a matching `LOCAL_SAFE` step and expected row version,
combining the routine claim/task/start/writer or complete/unlock/release/submit
sequence in one database transaction. HUMAN, remote, destructive, candidate,
publication, upgrade and rollback steps are always refused. MVP-LITE mutations
now return `AWB-MUTATION-RECEIPT-v1` by default; add `--full` when the complete
WorkItem projection is needed.

`awb verify status/run/override` is the single verification entry point. Its
classifier selects the effective policy, registered checks run only in a disposable
copy protected from live database writes, and a successful FINAL run records one
`AWB-VERIFY-RECEIPT-v1`. Implementation submission uses `--verify-receipt`; the
independent Reviewer and FINAL gate validate that same current candidate binding.

An explicit Release WorkItem uses `awb candidate prepare/freeze/build/status/
quarantine/finalize`. Candidate bytes live below the project-owned managed root;
quarantine is reversible and finalize never deletes bytes. The build command
accepts one exact offline Python/setuptools/wheel toolchain file and never
downloads or substitutes tools. Independent Implementation review happens
before publication: PASS/open0 produces `PUBLICATION_READY`, while exact HUMAN
authorization and post-publication receipt extension remain separate prerequisites for
FINAL. AWB audits these local boundaries but does not execute remote publication.

## HUMAN-only orphan Reviewer recovery

`awb lite --project <project> recover-review-task <WorkItem> <Task> --human
<human-id> --reason <reason> --request-id <unique-id>` can reset only one
strictly evidenced orphaned PLAN Reviewer task from `IN_PROGRESS` to
`NOT_STARTED`. It is not a general reset and never unblocks, approves, submits,
claims, publishes, upgrades, or resumes implementation. Conflicts and runtime
drift fail closed with zero writes.

## Usage observation Preview

New and legacy projects normalize `usagePolicy` to `OFF`; normal workflow then
creates no bindings/spans and performs no mutation sync or periodic refresh.
Use `awb usage policy enable --project <project>` to opt into `BEST_EFFORT`, and
`awb usage policy disable` to return to OFF. Explicit historical show/export/
self-check stays available. When enabled, the `codex-local` adapter observes a local, non-public schema
and fails closed on drift. It accepts a single requested-head metadata record as
parser v2 or a strict requested-head-to-ancestor chain as parser v3, then reads only allowlisted metadata and usage
counters; it does not upload prompts, responses, tool output, source, sessions
or databases. See `docs/usage/quickstart.md` for the opt-in workflow.

Raw tokens are the primary evidence. `estimatedCredits` are versioned local
analysis units, not the ChatGPT/Codex weekly quota bill. Quota window snapshots
remain separate and are never allocated directly to a role or WorkItem. The
AWB-010 long-running observation has not completed, so this Preview does not
claim a stable baseline, quota prediction, GA readiness or automated control.

## Local multi-Orchestrator coordination

The source tree includes provider-neutral `awb orchestrator` primitives for
multiple local runtimes to own different WorkItems. Each WorkItem has at most
one active Orchestrator lease; repository writes still use the existing single
writer lock. Existing databases must run `awb migrate --check` and then the
backup-first `awb migrate` before using this extension. See
`docs/orchestration/quickstart.md` for the explicit-claim, dispatch-fence,
renew, release and recovery contract. This is a coordination API only: AWB does
not spawn, supervise, kill or steer host Agent processes.

## Security and scope

The package has no remote control plane and does not publish, deploy, or write
to remote services. The MIT license applies to this repository; contributions
are accepted under the same terms with no contributor license agreement.

Historical implementation materials and private migration evidence are not
part of this public repository.
