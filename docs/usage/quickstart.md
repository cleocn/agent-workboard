# Quickstart

Use a hash-verified release wheel, then initialize and inspect a project:

```bash
python -m pip install agent_workboard-0.1.0-py3-none-any.whl
awb init --project ./example
awb doctor --project ./example
awb lite --project ./example list
```

Use `awb bootstrap --project ./example` when a managed project has no database
yet. Do not point a stable project at an arbitrary database path.
