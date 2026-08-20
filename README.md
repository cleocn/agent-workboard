# Agent Workboard

Agent Workboard is a local-first SQLite workboard for a small, auditable
planner → reviewer → human gate → implementer → reviewer workflow. It has no
runtime third-party dependency and its read-only board listens only on a
loopback address.

## Install a verified release

Download the `agent_workboard-0.2.0-py3-none-any.whl` asset and verify its
SHA-256 against `SHA256SUMS` from the GitHub release. Install it with pip, then
initialize a project:

```bash
python -m pip install agent_workboard-0.2.0-py3-none-any.whl
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

## Upgrade an existing 0.1.0 project

Back up the project and install the verified 0.2.0 wheel. If the old project
uses the official GitHub URL lock, place the verified 0.1.0 wheel beside the
0.2.0 wheel; a local file lock continues to use its recorded path. Then run:

```bash
awb upgrade --project ./my-project --wheel ./agent_workboard-0.2.0-py3-none-any.whl --with-codex
awb doctor --project ./my-project
awb codex check --project ./my-project
```

Upgrade refuses an invalid database, active claim or writer, a wheel/identity
mismatch, and customized package-owned Codex files. It writes a timestamped
backup directory under `.awb/backups/` before replacing the project lock.
To roll back, restore `.awb/config.json`, `.awb/requirements-awb.txt`, and any
Codex files from that directory, reinstall the hash-locked 0.1.0 wheel, and run
`awb doctor`. Restore the database backup only if a later release introduces a
data/schema change; 0.2.0 does not change the schema.

The 0.2.0 release adds bounded `3+1+1` plan and implementation review
convergence, the higher-model convergence reviewer, the unified WorkItem
management contract, and the seven canonical templates maintained under
`docs/work-item-templates/`.

## Security and scope

The package has no remote control plane and does not publish, deploy, or write
to remote services. The MIT license applies to this repository; contributions
are accepted under the same terms with no contributor license agreement.

Historical implementation materials and private migration evidence are not
part of this public repository.
