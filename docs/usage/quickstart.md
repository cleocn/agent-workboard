# Quickstart

Use a hash-verified release wheel, then initialize and inspect a project:

```bash
python -m pip install agent_workboard-0.2.1-py3-none-any.whl
awb init --project ./example
awb doctor --project ./example
awb lite --project ./example list
```

Use `awb bootstrap --project ./example` when a managed project has no database
yet. Do not point a stable project at an arbitrary database path.

For an exact verified 0.1.0 or 0.2.0 project, retain its old wheel and install
the verified 0.2.1 wheel. Run the zero-write check first and execute only its
single structured `nextStep`:

```bash
awb upgrade --check --project ./example --wheel ./agent_workboard-0.2.1-py3-none-any.whl --with-codex
awb upgrade --project ./example --wheel ./agent_workboard-0.2.1-py3-none-any.whl --with-codex
awb doctor --project ./example
awb codex check --project ./example
```

The command refuses active claims/writers and customized package-owned Codex
files. Its `AWB-UPGRADE-v1` result contains the exact rollback manifest and one
next step. Read the shipped Orchestrator reference
`references/upgrade-and-rollback.md` before upgrade, rollback or recovery.
