# Agent Workboard upgrade and rollback runbook

Protocol: `AWB-UPGRADE-RUNBOOK-v1`

## APPLIES

Use this runbook only for an existing stable managed project with
`.awb/config.json` and an exact source/target pair listed by the installed AWB
release. The current Preview matrix supports only the exact released
`0.2.1/v0.2.1` identity to the exact running `0.3.0b1/v0.3.0b1` wheel, and an
exact same-identity no-op after the usage schema is installed and valid.

## DOES_NOT_APPLY

Do not use it for a new project, a development database, standalone `awb
migrate`, package download or publication, a remote consumer migration, a
direct `0.1.0`/`0.2.0` jump, or any unlisted future version. Never infer
compatibility from a semantic-version wildcard; older projects must first reach
exact 0.2.1 with the released 0.2.1 procedure.

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

Stop for an unsupported identity/pair, source or target drift, active claim or
repository writer, a pre-existing/partial usage extension on the 0.2.1 source,
a missing/invalid usage extension on a same-identity 0.3.0b1 project,
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
`usageSchemaVersion=AWB-USAGE-v1`. If `--with-codex` was used, also run `awb
codex check --project <project>`. Record both results in the WorkItem before
treating upgrade as accepted. The upgrade itself performs the backup-first
additive database migration; do not run a separate unbound migration step.

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
