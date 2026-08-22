# Quickstart

Agent Workboard 0.3.0b1 is an opt-in, local-only, observation-only Preview. Use
a hash-verified GitHub prerelease wheel, then initialize and inspect a project:

```bash
shasum -a 256 -c SHA256SUMS
python -m pip install agent_workboard-0.3.0b1-py3-none-any.whl
awb init --project ./example
awb doctor --project ./example
awb lite --project ./example list
```

Use `awb bootstrap --project ./example` when a managed project has no database
yet. Do not point a stable project at an arbitrary database path.

For an exact verified 0.2.1 project, retain its old wheel and install the
verified 0.3.0b1 Preview wheel. Run the zero-write check first and execute only
its single structured `nextStep`:

```bash
awb upgrade --check --project ./example --wheel ./agent_workboard-0.3.0b1-py3-none-any.whl --with-codex
awb upgrade --project ./example --wheel ./agent_workboard-0.3.0b1-py3-none-any.whl --with-codex
awb doctor --project ./example
awb codex check --project ./example
```

The command refuses active claims/writers and customized package-owned Codex
files. Its `AWB-UPGRADE-v1` result contains the exact rollback manifest and one
next step. Read the shipped Orchestrator reference
`references/upgrade-and-rollback.md` before upgrade, rollback or recovery. The
upgrade installs `AWB-USAGE-v1` transactionally after an online backup; rollback
refuses if the upgraded database has been used or otherwise drifted.

## Explicit observation flow

Bind each Agent claim to its own local Codex session, or create an explicit
ORCHESTRATOR span, before syncing. Start with a dry run:

```bash
awb usage sync --project ./example --work-item AWB-123 --dry-run
awb usage sync --project ./example --work-item AWB-123
awb usage self-check --project ./example
awb usage show AWB-123 --project ./example --group-by role
awb usage export --project ./example --work-item AWB-123 --group-by role --format json --output usage-redacted.json
```

The `codex-local` adapter consumes only allowlisted metadata and cumulative
counter snapshots. It treats that local schema as `OBSERVED` and fails closed on
unknown input. Exports hash session identifiers and contain no local paths;
prompts, responses, tool output, source, credentials, session files and
databases are neither exported nor uploaded.

Raw tokens are authoritative. `estimatedCredits`, their status and rate-card
version are local analytical values—not the ChatGPT/Codex weekly quota bill.
Quota windows are reported separately. AWB-010 long-term observation is still
in progress, so this Preview provides no stable baseline, quota forecast,
automatic model routing, budget cap or Agent stop.
