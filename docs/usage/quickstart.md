# Quickstart

Use a hash-verified release wheel, then initialize and inspect a project:

```bash
python -m pip install agent_workboard-0.2.0-py3-none-any.whl
awb init --project ./example
awb doctor --project ./example
awb lite --project ./example list
```

Use `awb bootstrap --project ./example` when a managed project has no database
yet. Do not point a stable project at an arbitrary database path.

For a verified 0.1.0 project, retain its old wheel and run the guarded upgrade
after installing 0.2.0:

```bash
awb upgrade --project ./example --wheel ./agent_workboard-0.2.0-py3-none-any.whl --with-codex
awb doctor --project ./example
awb codex check --project ./example
```

The command refuses active claims/writers and customized package-owned Codex
files. Its result names the backup directory needed for rollback.
