# Changelog

## 0.3.1b9 Preview

- Install the accepted built-in Codex role types with explicit model and
  reasoning-effort routing, bounded/no-history forks, upward-only promotion,
  observable same-type fallbacks, and HUMAN-only Reviewer downshift.
- Keep root and packaged Agent/Skill resources aligned while removing the
  unconsumed `modelOverrides` WorkItem schema surface.
- Extend the frozen migration graph with exact public b8 to b9 using the
  existing backup-bound transaction and `NO_DDL` rollback contract.

## 0.3.1b8 Preview

- Centralize verification defaults, classifier, registered checks and the
  candidate-bound `AWB-VERIFY-RECEIPT-v1` in one package authority.
- Keep only the five workflow safety boundaries as hard contracts; make test
  intensity and evidence cost configurable policy with bounded HUMAN downgrade.
- Replace duplicate quality-baseline and release-gate evidence with one current
  receipt while retaining independent review and risky-action authorization.
- Extend the frozen no-DDL migration graph with exact public b7 to b8 and retain
  backup-first direct upgrade plus same-b8 no-op behavior.

## 0.3.1b7 Preview

- Add one declarative workflow transition kernel with transaction-bound pre/post
  invariants, atomic Reviewer task lifecycle, and fail-closed mutation receipts.
- Add content-free `workflow check` plus a closed exact-fingerprint repair for
  uniquely proven projection splits; ambiguous state remains zero-write.
- Replace implicit TTL cleanup with the fingerprinted
  `EXPIRE_AND_RECONCILE_ACTIVITY` bundle and preserve exact owner/generation
  fencing, replay and complete old-or-new transaction outcomes.
- Extend the frozen no-DDL migration graph with exact public b6 to b7 while
  retaining the streamlined single-build Preview evidence policy.

## 0.3.1b6 Preview

- Fix terminal and non-claimable Orchestrator claims to return the stable
  structured refusal envelope without changing the database.
- Bind FINAL approval audit to the exact released Reviewer claim in both
  automatic and manual flows, with fail-closed legacy validation.
- Add a frozen `AWB-MIGRATION-GRAPH-v1` route so exact public b3, b4, and b5
  projects can reach b6 through one preflight, one backup-first execution, and
  one whole-route rollback.
- Preserve the b5 activity model, no-DDL schema contract, Usage default OFF,
  independent reviews, local-only scope, and streamlined Preview gates.

## 0.3.1b5 Preview

- Project claims, repository writers, and Orchestrator leases now share one
  effective LIVE versus persisted STALE projection in doctor and public
  `activity list/show` diagnostics.
- True FINAL transitions atomically close their activity, while exact
  `activity reconcile-expired` remains audited, idempotent, and fenced.
- Exact public b3 and b4 consumers can upgrade directly to b5. Stale-only
  projects receive one backup-first reconciliation-aware next step; live or
  mixed projects stop at the live owner, without manual SQLite cleanup.

## 0.3.1b4 Preview

- Make Usage collection project-level and default it to `OFF`; explicit
  `BEST_EFFORT` opt-in preserves local observation while OFF avoids automatic
  binding, spans, mutation sync and periodic refresh.
- Let a PLAN Reviewer apply one bounded non-material amendment through the
  package-owned atomic writer path; the editor cannot self-review and the next
  round requires a fresh Reviewer within strict 3+1+1/no-round-6 limits.
- Treat runtime events as workflow evidence authority, retain one final
  implementation summary and one release postflight, and remove duplicate
  per-round evidence plus expensive Preview reproducibility/object-closure gates.
- Support only exact 0.3.1b3→0.3.1b4 backup-first upgrades with no schema DDL,
  database-bound rollback, and same-identity no-op validation.
- Preserve independent Implementation review, AUTO_ON_PASS authorization
  boundaries, test-failure stops and local-only operation; no PyPI, stable/GA,
  runner, daemon, remote control or consumer migration is included.

## 0.3.1b3 Preview

- Accept Codex 0.148 multi-`session_meta` records only as a strict, unique
  requested-HEAD-to-ancestor chain; keep single metadata on parser v2 and mark
  legal multi-metadata input as parser v3.
- Keep the claim role authoritative, inspect only the HEAD source role as a
  quality cross-check, and fail closed on broken, reordered, duplicate, cyclic,
  ambiguous, or post-token metadata without materializing session content.
- Support only exact 0.3.1b2→0.3.1b3 backup-first upgrades with no schema DDL,
  database-bound rollback, and same-identity no-op validation.
- Keep AWB-010 unstarted, usage evidence pre-cohort, and PyPI, stable/GA,
  consumer migration, runner, daemon, and remote-control claims out of scope.

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
