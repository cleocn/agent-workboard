# Agent Workboard

Agent Workboard is a local-first SQLite workboard for a small, auditable
planner → reviewer → human gate → implementer → reviewer workflow. It has no
runtime third-party dependency and its read-only board listens only on a
loopback address.

## Install a verified release

Download the `agent_workboard-0.2.1-py3-none-any.whl` asset and verify its
SHA-256 against `SHA256SUMS` from the GitHub release. Install it with pip, then
initialize a project:

```bash
python -m pip install agent_workboard-0.2.1-py3-none-any.whl
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

## Upgrade an existing 0.1.0 or 0.2.0 project

Retain the exact old wheel and install the verified 0.2.1 wheel. If the project
uses an official GitHub URL lock, place its verified 0.1.0 or 0.2.0 wheel beside
the 0.2.1 wheel; a local file lock continues to use its recorded path. Always
run the read-only preflight first, then execute only its structured `nextStep`:

```bash
awb upgrade --check --project ./my-project --wheel ./agent_workboard-0.2.1-py3-none-any.whl --with-codex
awb upgrade --project ./my-project --wheel ./agent_workboard-0.2.1-py3-none-any.whl --with-codex
awb doctor --project ./my-project
awb codex check --project ./my-project
```

Upgrade accepts only the exact released 0.1.0→0.2.1 and 0.2.0→0.2.1 identities.
It refuses an invalid database, active claim or writer, identity drift and
customized package-owned Codex files. It writes a bound rollback manifest under
`.awb/backups/`; use `awb upgrade --check --rollback <manifest>` before executing
the exact rollback command. Do not restore files by hand. The retained database
backup is evidence only because 0.2.1 does not change the schema.

The shipped Orchestrator Skill routes Agents to
`references/upgrade-and-rollback.md` (`AWB-UPGRADE-RUNBOOK-v1`) before upgrade,
rollback or recovery work. The 0.2.1 release adds that bounded Agent runbook,
zero-write preflight, structured results and exact rollback material.

## Security and scope

The package has no remote control plane and does not publish, deploy, or write
to remote services. The MIT license applies to this repository; contributions
are accepted under the same terms with no contributor license agreement.

Historical implementation materials and private migration evidence are not
part of this public repository.
