# Quickstart

Agent Workboard 0.3.1b7 is an opt-in, local-only Preview. Use
a hash-verified GitHub prerelease wheel, then initialize and inspect a project:

Usage is project-level and defaults to OFF, including when an older config
omits the field. OFF is healthy: claims and reviews continue without binding,
spans, mutation sync/show, periodic refresh, or Usage gates.

```bash
shasum -a 256 -c SHA256SUMS
python -m pip install agent_workboard-0.3.1b7-py3-none-any.whl
awb init --project ./example
awb doctor --project ./example
awb lite --project ./example list
```

Use `awb bootstrap --project ./example` when a managed project has no database
yet. Do not point a stable project at an arbitrary database path.

For an exact verified 0.3.1b3, 0.3.1b4, 0.3.1b5, or 0.3.1b6 project, retain its old wheel and install the
verified 0.3.1b7 Preview wheel. Run the zero-write check first and execute only
its single structured `nextStep`:

```bash
awb upgrade --check --project ./example --wheel ./agent_workboard-0.3.1b7-py3-none-any.whl --with-codex
awb upgrade --project ./example --wheel ./agent_workboard-0.3.1b7-py3-none-any.whl --with-codex
awb doctor --project ./example
awb codex check --project ./example
```

The check separates LIVE from STALE activity. Stale-only projects receive one
fingerprint-bound reconciliation/upgrade action; LIVE or mixed state first
stops at the live owner. No manual SQLite cleanup is needed. The command also
refuses customized package-owned Codex files. Its `AWB-UPGRADE-v1` result
contains the exact rollback manifest and one
next step. Read the shipped Orchestrator reference
`references/upgrade-and-rollback.md` before upgrade, rollback or recovery. The
upgrade validates the existing `AWB-USAGE-v1`, `AWB-ORCHESTRATOR-v1` and
`AWB-AUTO-GATE-v1` transactionally without schema DDL after an online backup;
rollback refuses if
the upgraded database has been used or otherwise drifted.

## Explicit observation flow

Bind each Agent claim to its own local Codex session, or create an explicit
ORCHESTRATOR span, before syncing. Start with a dry run:

```bash
awb usage policy show --project ./example
awb usage policy enable --project ./example
awb usage sync --project ./example --work-item AWB-123 --dry-run
awb usage sync --project ./example --work-item AWB-123
awb usage self-check --project ./example
awb usage show AWB-123 --project ./example --group-by role
awb usage export --project ./example --work-item AWB-123 --group-by role --format json --output usage-redacted.json
awb usage policy disable --project ./example
```

The `codex-local` adapter accepts a single requested-head metadata record as
parser v2 or a strict requested-head-to-ancestor chain as parser v3, then consumes only
allowlisted metadata and cumulative counter snapshots. It treats that local schema as `OBSERVED` and fails closed on
unknown input. Exports hash session identifiers and contain no local paths;
prompts, responses, tool output, source, credentials, session files and
databases are neither exported nor uploaded.

Raw tokens are authoritative. `estimatedCredits`, their status and rate-card
version are local analytical values—not the ChatGPT/Codex weekly quota bill.
Quota windows are reported separately. AWB-010 long-term observation is still
in progress, so this Preview provides no stable baseline, quota forecast,
automatic model routing, budget cap or Agent stop.

## Main-Agent display cadence

Only BEST_EFFORT performs collection. OFF never asks the Orchestrator Skill to
sync after mutations or refresh on a timer. Neither mode creates a daemon,
offline timer, runner, or heartbeat SLA. Explicit reports remain limited to
aggregate tokens, estimated credits, quota, coverage, and gap reasons; prompt,
response, tool output, credentials, raw session content, and local paths remain
excluded.
