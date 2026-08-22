# Changelog

## 0.3.1b2 Preview

- Add a HUMAN-only, fail-closed `recover-review-task` command for one precisely
  bound orphaned PLAN Reviewer task; it never unblocks or resumes work itself.
- Support only exact 0.3.1b1→0.3.1b2 backup-first upgrades with no schema DDL,
  database-bound rollback, and same-identity no-op validation.
- Preserve independent 3+1+1 review, AUTO_ON_PASS authorization boundaries,
  existing usage/Orchestrator/auto-gate semantics, and local-only operation.
- Do not include the pending Codex usage parser candidate; AWB-010 remains
  unstarted and estimated credits remain separate from official weekly quota.

## 0.3.1b1 Preview

- Add provider-neutral, one-shot local Orchestrator registration, WorkItem lease,
  claim-next, renewal, release, recovery, and generation-fenced dispatch commands.
- Make new STANDARD WorkItems default to `AUTO_ON_PASS`, retain independent
  3+1+1 review, and require an explicit policy choice when creation risk is remote,
  destructive, or anomalous; risky actions remain separately authorized.
- Add main-Agent best-effort usage display and process-scoped macOS
  `caffeinate -di` guidance without introducing a runner, daemon, or timing SLA.
- Support only exact 0.3.0b1→0.3.1b1 backup-first upgrades that transactionally
  install Orchestrator and auto-gate schemas, with database-bound rollback.
- Keep PyPI publication, remote control, host process management, stable/GA claims,
  and official weekly-quota accounting out of scope.

## 0.3.0b1 Preview

- Add opt-in, local-only, observation-only usage accounting for explicitly bound
  AWB sessions, with role/agent/model/stage/time projections and redacted export.
- Preserve privacy with an allowlist-only `codex-local` adapter that fails closed
  on observed-schema drift and never uploads session content or source.
- Report raw token counters and versioned estimated credits separately from quota
  window snapshots; estimated credits are not the ChatGPT/Codex weekly bill.
- Support only exact 0.2.1→0.3.0b1 upgrades with backup-first transactional usage
  migration, database-bound rollback and zero-write refusal on drift or use.
- Keep AWB-010 long-term observation, automated routing, budget caps, hard stops,
  telemetry, real consumer migration and PyPI publication out of scope.

## 0.2.1

- Ship `AWB-UPGRADE-RUNBOOK-v1` with the Orchestrator Skill and release wheel.
- Add zero-write `upgrade --check`, structured results and exact rollback/recovery material.
- Support only the verified exact 0.1.0→0.2.1 and 0.2.0→0.2.1 upgrade pairs; refuse all others.
- Complete rollback handling for package-owned Codex files newly created by an upgrade.

## 0.2.0

- Add evidence-backed, bounded `3+1+1` convergence for plan and implementation review.
- Add the higher-model convergence reviewer and enforce Finding/quality-ratchet contracts.
- Make the unified WorkItem management contract and seven canonical templates distributable resources.
- Add a guarded 0.1.0-to-0.2.0 project/Codex upgrade with backups and no-clobber checks.
- Replace the initial orphan-root publication gate with a normal public-main successor gate.

## 0.1.0

- First public MIT release of the local-first Agent Workboard package.
