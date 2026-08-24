# Local multi-Orchestrator quickstart

In b6, persisted `ACTIVE` and effective activity are separate. An unexpired row
is `LIVE`; an expired persisted row is visible as `STALE` until an audited
reconciliation or a true terminal transition closes it. `doctor`, `activity`
and Orchestrator list/show use the same transaction clock.

`AWB-ORCHESTRATOR-v1` lets independent local host runtimes own different
WorkItems without sharing one WorkItem. AWB supplies one-shot SQLite/JSON
coordination commands; the host runtime owns polling, Agent sessions and process
lifecycle.

## Install the additive schema

For an existing database, check first. The write makes one online backup and
installs all pending extensions in one transaction:

```bash
awb migrate --project . --check
awb migrate --project .
awb doctor --project .
```

`doctor` must report `orchestratorSchemaVersion=AWB-ORCHESTRATOR-v1`. A partial
or unknown schema fails closed.

## Claim a specified WorkItem

Mutation request IDs are mandatory and safe to retry. The same ID and exact
request returns the stored result without another side effect; reusing it for
different content is refused.

```bash
awb orchestrator register --project . --orchestrator local-1 --request-id register-1
awb orchestrator claim AWB-012 --project . --orchestrator local-1 \
  --ttl 900 --request-id claim-1
```

Save the returned `lease.generation`. Once a WorkItem has any Orchestrator lease
history, every new Planner, Implementer or Reviewer dispatch must carry the
current active, unexpired fence:

```bash
awb lite --project . claim AWB-012 AWB-012-T02 \
  --agent implementer-1 --role IMPLEMENTER \
  --orchestrator-id local-1 --orchestrator-generation 1
```

An extension-free database or a WorkItem with zero lease history retains the
legacy no-fence dispatch path. After the first history row, omitting either or
both fence values, using another owner, or replaying an expired/released/old
generation is refused before the Agent claim, WorkItem projection or workflow
event changes.

## Queue claim, renewal and release

```bash
awb orchestrator claim-next --project . --orchestrator local-2 \
  --ttl 900 --request-id next-1
awb orchestrator renew AWB-012 --project . --orchestrator local-1 \
  --generation 1 --ttl 900 --request-id renew-1
awb orchestrator show AWB-012 --project .
awb orchestrator list --project . --status ACTIVE
awb orchestrator release AWB-012 --project . --orchestrator local-1 \
  --generation 1 --request-id release-1
awb activity list --project . --work-item AWB-012
```

`claim-next` uses P0→P3, `updated_at`, then WorkItem ID ordering inside the same
`BEGIN IMMEDIATE` transaction that creates the lease. Different WorkItems can
have different owners. Two callers racing for one WorkItem cannot both win.

The host should renew before expiry. `WAITING_HUMAN` may be renewed, but an
Orchestrator cannot perform a HUMAN gate. `HELD` and `BLOCKED` refuse renewal.
Releasing or losing the Orchestrator lease does not revoke a previously valid
Agent task claim.

Only `FINAL_ACCEPTANCE_APPROVED` is a true terminal state and atomically closes
its claim, writer, and lease. `BLOCKED`, `WAITING_HUMAN`, task `CANCELLED`, and
nonterminal `HELD` remain resumable and do not trigger cleanup.
The terminal audit also binds the exact released FINAL Reviewer claim; legacy
b5 manual approvals proceed only when that binding is unique and exact.

## Recover an expired owner

```bash
awb orchestrator recover AWB-012 --project . --orchestrator local-3 \
  --ttl 900 --request-id recover-1
```

Recovery requires the latest lease to be expired and creates the next
generation. It can recover monitoring ownership while an Agent is already
executing, but never releases or impersonates that Agent. A stale owner must
stop when it receives `STALE_FENCE`.

Every result uses `AWB-ORCHESTRATOR-v1`, a stable status/reason, and exactly one
structured `nextStep`. `OK` and `NO_OP` exit 0; `REFUSED` and `CONFLICT` exit 2.
Events contain only normalized request/result fields—never prompts, responses,
tool output, credentials, hostnames, PIDs or local session paths.

## Transactional routine advancement

Use `awb workflow status <WorkItem> --agent <id> --role <role> --project .`
to obtain the current row version and exact step fingerprint. Pass both values
unchanged to `awb workflow advance`. Eight routine planning, review and
implementation begin/submit bundles are local-safe. Each bundle writes its
legacy audit subevents plus one `WORKFLOW_ADVANCED` receipt in one transaction;
exact request replay is zero-write. Any HUMAN, remote, destructive, publication,
candidate, upgrade or rollback step is returned for its proper actor and cannot
be consumed by advance.

## Limits

- One active Orchestrator lease per WorkItem; no shared ownership or force flag.
- One active repository writer per `repositoryKey`, even across different
  WorkItems and Orchestrators.
- No daemon, scheduler, remote control plane, provider binding or host process
  management.
- Usage spans remain explicit observation data and never grant scheduling
  authority.

The Codex Orchestrator Skill may best-effort hold one foreground `caffeinate
-di` tool session on macOS while the main Agent is actively managing WorkItems.
Multiple active WorkItems share that exact-session inhibitor, which is released
only after the last active item stops. Missing-platform, start, ownership, or
cleanup failures are explicit warnings; the Skill never scans names, guesses
PIDs, uses `pkill`/`pmset`, or installs a daemon. This tool-session behavior does
not change `hostProcessManagement:false` and is not an AWB runner.
