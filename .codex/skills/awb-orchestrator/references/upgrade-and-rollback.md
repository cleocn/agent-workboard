# Agent Workboard upgrade and rollback runbook

Protocol: `AWB-UPGRADE-RUNBOOK-v1`

## APPLIES

Use this runbook only for an existing stable managed project with
`.awb/config.json` and an exact source/target pair listed by the installed AWB
release. The current Preview matrix supports exact released
`0.3.1b3/v0.3.1b3`, `0.3.1b4/v0.3.1b4`, `0.3.1b5/v0.3.1b5`,
`0.3.1b6/v0.3.1b6`, `0.3.1b7/v0.3.1b7`, `0.3.1b8/v0.3.1b8`, and
`0.3.1b9/v0.3.1b9` identities
through the frozen `AWB-MIGRATION-GRAPH-v1` to the exact running
`0.3.1b10/v0.3.1b10` wheel, and an exact same-identity 0.3.1b10 no-op when
the usage, Orchestrator, and auto-gate
schemas are installed and valid. This model-routing release performs no schema DDL.

## DOES_NOT_APPLY

Do not use it for a new project, a development database, standalone `awb
migrate`, package download or publication, a remote consumer migration, a
direct `0.3.1b2` or earlier jump, or any unlisted future version. Never infer
compatibility from a semantic-version wildcard; older projects must first reach
exact 0.3.1b3 with the released 0.3.1b3 procedure.

## AUTHORITY

A local read-only preflight may run under existing diagnostic authority.
`UPGRADE` and `ROLLBACK` are local writes and require explicit authority from the
current WorkItem or user.  That authority does not include commit, push, publish,
remote writes, package download, or migration of an active consumer.

## PREFLIGHT_FIRST

Before upgrade, run and retain the JSON and exit code:

```text
awb upgrade --check --project <project> --wheel <exact-wheel> [--with-codex]
```

Before rollback, use only the exact returned manifest:

```text
awb upgrade --check --project <project> --rollback <exact-manifest>
```

Do not perform the write unless the result is `READY` and contains exactly one
non-empty `nextStep` object.

The check distinguishes effective LIVE activity from persisted ACTIVE rows
whose expiry has passed. LIVE (including mixed LIVE plus stale) returns one
`STOP_LIVE_ACTIVITY_OWNER_AND_RECHECK` step and performs no write. Stale-only
returns one `EXECUTE_UPGRADE_WITH_RECONCILIATION` step containing the exact
`expectedStaleActivity` fingerprint and `reconciliationRequestId`; pass those
arguments unchanged to `awb upgrade`. The target wheel creates and verifies the
backup first, rechecks the snapshot, reconciles it transactionally, and then
upgrades. No manual cleanup or SQLite command is part of the consumer path.

## RESULT_READY

Execute only the single `nextStep`, with its exact project, wheel, Codex flag, or
manifest arguments.  Do not substitute an artifact, relax an identity/hash
check, or invent a parallel action.

## RESULT_NO_OP

Do not create a backup and do not run upgrade.  Retain the evidence.  The only
`nextStep` is `{"action":"NONE","arguments":{}}`; it authorizes no action.

## RESULT_REFUSED

Stop immediately.  Do not modify the project, database, Codex files, or backup
tree.  Retain `reason`, `evidence`, `risks`, and the exit code; resolve only the
single `nextStep`, then run preflight again.

## MANDATORY_STOP

Stop for an unsupported identity/pair, source or target drift, active claim,
repository writer, or Orchestrator lease, a missing or invalid usage,
Orchestrator, or auto-gate extension on a supported source or same-identity
0.3.1b10 project,
customized/unowned/symlink Codex content, wrong project, path traversal,
duplicate target, manifest replay, or any live/backup/staging hash drift. Do not
delete, copy, or edit files by hand to bypass the refusal.

## EVIDENCE

Retain the complete preflight, upgrade, and rollback JSON plus exit codes; old
and new wheel paths and SHA-256 values; database pre/post SHA-256 values; backup
and manifest paths; and the later doctor/Codex results. Never record credentials
or sensitive raw data.

## POST_UPGRADE

After `OK`, run `awb doctor --project <project>` and require
`usageSchemaVersion=AWB-USAGE-v1`,
`orchestratorSchemaVersion=AWB-ORCHESTRATOR-v1`, and
`gatePolicySchemaVersion=AWB-AUTO-GATE-v1`. If `--with-codex` was used, also run
`awb codex check --project <project>`. Record both results in the WorkItem before
treating upgrade as accepted. The upgrade itself performs a backup-first
transactional validation of all three existing extensions without schema DDL;
do not run a separate unbound migration step.

## ROLLBACK

Rollback only when the user explicitly requests it, or required post-upgrade
validation failed and local write authority was granted. First run rollback
preflight against the exact manifest. The database is a bound restore action:
any workflow or usage write after upgrade changes its post-upgrade hash and must
make rollback refuse rather than discard data. If it is `READY`, run:

```text
awb upgrade --project <project> --rollback <exact-manifest>
```

Never handwrite `rm`/`cp`, select another backup, or restore the retained live
database automatically.

## ROLLBACK_VERIFY

After rollback `OK`, install the old wheel locked by the restored requirements,
then run `awb doctor --project <project>`.  If the upgrade included Codex files,
run `awb codex check --project <project>`.  Retain the reported
restored/removed/retained/untouched evidence.

## RESULT_BLOCKED

Stop every other mutation.  Execute only the recovery-only `nextStep`.  That
call may restore the complete post-upgrade state, but must not continue into the
formal rollback in the same invocation.  Run rollback preflight again only after
recovery reports the retry action; otherwise escalate the retained evidence.

## NEXT_STEP_ARGUMENTS

Treat `nextStep` as structured data, never as a shell-command string.  A forward
`READY` or retry action contains the canonical `project`, exact `wheel`, and
`withCodex` selection.  A rollback `READY`, retry, refusal, or recovery-only
action contains the canonical `project` and exact `rollbackManifest`.  Successful
rollback verification additionally contains the old `wheel` and `withCodex`
selection.  Forward refusals retain the exact forward arguments, so the stated
repair can be followed by the same preflight.  `NO_OP`, and `DONE` if a future
compatible implementation emits it, use `NONE` with empty arguments.

## One-next-step rule

Every `AWB-UPGRADE-v1` result has exactly one non-empty `nextStep`.  Consume that
object as the only allowed continuation; never translate suggestions into extra
requirements or additional actions.
