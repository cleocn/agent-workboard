# Agent Workboard

Agent Workboard is a local-first SQLite workboard for a small, auditable
planner → reviewer → human gate → implementer → reviewer workflow. It has no
runtime third-party dependency and its read-only board listens only on a
loopback address.

## Install the 0.3.0b1 Preview

This release is an opt-in, local-only, observation-only Preview. Download the
wheel, sdist and `SHA256SUMS` from the GitHub `v0.3.0b1` prerelease, verify both
artifacts, then initialize a project:

```bash
shasum -a 256 -c SHA256SUMS
python -m pip install agent_workboard-0.3.0b1-py3-none-any.whl
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

## Upgrade an exact 0.2.1 project

Retain the exact old 0.2.1 wheel and install the verified 0.3.0b1 wheel. If the
project uses an official GitHub URL lock, place its verified 0.2.1 wheel beside
the Preview wheel; a local file lock continues to use its recorded path. Always
run the read-only preflight first, then execute only its one structured
`nextStep`:

```bash
awb upgrade --check --project ./my-project --wheel ./agent_workboard-0.3.0b1-py3-none-any.whl --with-codex
awb upgrade --project ./my-project --wheel ./agent_workboard-0.3.0b1-py3-none-any.whl --with-codex
awb doctor --project ./my-project
awb codex check --project ./my-project
```

Upgrade accepts only the exact released 0.2.1→0.3.0b1 identity pair. It refuses
an invalid database, active claim or writer, identity drift, a pre-existing
usage extension and customized package-owned Codex files. Execution first makes
an online backup, then installs the additive `AWB-USAGE-v1` schema in one
transaction. The bound rollback manifest includes the database; any post-upgrade
workflow or usage write makes rollback fail closed rather than discard data.
Use `awb upgrade --check --rollback <manifest>` before the exact rollback
command. Do not restore files or database tables by hand.

The shipped Orchestrator Skill routes Agents to
`references/upgrade-and-rollback.md` (`AWB-UPGRADE-RUNBOOK-v1`) before upgrade,
rollback or recovery work. It preserves zero-write preflight, structured
one-next-step results and exact rollback material.

## Usage observation Preview

Usage collection is off until sessions are explicitly bound and `awb usage
sync` is invoked. The `codex-local` adapter observes a local, non-public schema
and fails closed on drift. It reads only allowlisted session metadata and usage
counters; it does not upload prompts, responses, tool output, source, sessions
or databases. See `docs/usage/quickstart.md` for the opt-in workflow.

Raw tokens are the primary evidence. `estimatedCredits` are versioned local
analysis units, not the ChatGPT/Codex weekly quota bill. Quota window snapshots
remain separate and are never allocated directly to a role or WorkItem. The
AWB-010 long-running observation has not completed, so this Preview does not
claim a stable baseline, quota prediction, GA readiness or automated control.

## Security and scope

The package has no remote control plane and does not publish, deploy, or write
to remote services. The MIT license applies to this repository; contributions
are accepted under the same terms with no contributor license agreement.

Historical implementation materials and private migration evidence are not
part of this public repository.
