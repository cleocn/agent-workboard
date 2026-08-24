"""The public local-only command line entry point."""

import argparse
import json
import os
import sys

from . import __version__
from . import candidate
from . import lite
from . import orchestrator
from . import usage
from .project import (backup, bootstrap, codex_check, codex_install, doctor,
                      init_project, migrate, transfer_export, transfer_import,
                      upgrade_project, usage_policy, set_usage_policy)


def _print(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _project_database(path, development=False):
    from .project import _load_config, _project_root, _validate_identity
    config, database = _load_config(_project_root(path))
    if development and config["runtimeMode"] != "development":
        raise lite.LiteError("--development requires a development configuration")
    _validate_identity(config, database)
    return database


def _project_usage_policy(path):
    from .project import _load_config, _project_root
    config, unused_database = _load_config(_project_root(path))
    return config["usagePolicy"]


def _project_identity(path):
    from .project import _load_config, _project_root
    root = _project_root(path)
    config, database = _load_config(root)
    return root, config, database


def _workflow_command(args):
    root, config, database = _project_identity(args.project)
    if args.workflow_command == "check":
        if bool(args.all) == bool(args.work_item):
            raise lite.LiteError("workflow check requires exactly one WorkItem or --all")
        result = lite.workflow_check(
            database, root, None if args.all else args.work_item
        )
        _print(result)
        return 2 if result.get("status") in ("VIOLATION", "WAITING_HUMAN") else 0
    if args.workflow_command == "repair":
        if not args.apply:
            raise lite.LiteError("workflow repair is zero-write without --apply")
        result = lite.workflow_repair(
            database, root, args.work_item, args.action, args.fingerprint,
            args.request_id, args.human,
        )
        _print(result)
        return 2 if result.get("status") in ("REFUSED", "WAITING_HUMAN") else 0
    common = (database, args.work_item, args.agent, args.role,
              config["repositoryKey"], args.orchestrator_id,
              args.orchestrator_generation, root)
    if args.workflow_command == "status":
        result = lite.workflow_status(*common)
    else:
        result = lite.workflow_advance(
            database, root, args.work_item, args.agent, args.role,
            config["repositoryKey"], args.expected_step,
            args.expected_row_version, args.request_id,
            args.orchestrator_id, args.orchestrator_generation, args.ttl,
            args.plan_artifact, args.submission_file, args.quality_file,
            args.review_file, args.decision, args.local_tests_passed,
            args.candidate, args.candidate_fingerprint,
        )
    _print(result)
    return 2 if result.get("status") in ("REFUSED", "WAITING_HUMAN") else 0


def _candidate_command(args):
    root, config, database = _project_identity(args.project)
    name = args.candidate_command
    if name == "status":
        result = candidate.status(database, root, args.work_item, args.candidate)
    elif name == "prepare":
        result = candidate.prepare(
            database, root, config["repositoryKey"], args.work_item,
            args.candidate, args.source, args.base_file, args.target_file,
            args.owner, args.request_id, args.orchestrator_id,
            args.orchestrator_generation,
        )
    elif name == "freeze":
        result = candidate.freeze(
            database, root, config["repositoryKey"], args.work_item,
            args.candidate, args.allowlist_file, args.owner, args.request_id,
            args.orchestrator_id, args.orchestrator_generation,
        )
    elif name == "build":
        result = candidate.build(
            database, root, config["repositoryKey"], args.work_item,
            args.candidate, args.toolchain_file, args.owner, args.request_id,
            args.orchestrator_id, args.orchestrator_generation,
        )
    elif name == "quarantine":
        result = candidate.quarantine(
            database, root, config["repositoryKey"], args.work_item,
            args.candidate, args.owner, args.request_id, args.restore,
            args.orchestrator_id, args.orchestrator_generation,
            args.quarantine_request_id,
        )
    else:
        result = candidate.finalize(
            database, root, config["repositoryKey"], args.work_item,
            args.candidate, args.owner, args.request_id,
            args.orchestrator_id, args.orchestrator_generation,
        )
    _print(result)
    return 2 if result.get("status") in ("REFUSED", "WAITING_HUMAN") else 0


def _publication_command(args):
    root, unused_config, database = _project_identity(args.project)
    name = args.publication_command
    if name == "status":
        result = candidate.publication_status(
            database, root, args.work_item, args.evidence_file
        )
    elif name == "authorize":
        result = candidate.publication_authorize(
            database, root, args.work_item, args.human, args.candidate,
            args.candidate_fingerprint, args.authorization_file,
            args.request_id,
        )
    elif name == "postflight":
        result = candidate.publication_postflight(
            database, root, args.work_item, args.operator,
            args.ready_fingerprint, args.authorization_request_id,
            args.evidence_file, args.request_id,
        )
    else:
        result = candidate.publication_retry(
            database, root, args.work_item, args.human, args.ready_fingerprint,
            args.retry_file, args.request_id,
        )
    _print(result)
    return 2 if result.get("status") in ("REFUSED", "WAITING_HUMAN", "NOT_READY") else 0


def _lite_args(args):
    database = args.database
    if database is None:
        database = _project_database(args.project, args.development)
    else:
        # Explicit databases are an identity assertion, never a way to bypass
        # a missing project contract or stable/development alias protection.
        try:
            configured = _project_database(args.project, args.development)
        except lite.LiteError as exc:
            if "invalid or missing .awb/config.json" in str(exc):
                raise lite.LiteError("--database requires a configured AWB project")
            raise
        else:
            if os.path.realpath(database) != os.path.realpath(configured):
                raise lite.LiteError("--database must match the configured project database")
    from .project import _load_config, _project_root
    config, unused = _load_config(_project_root(args.project))
    return ["--database", database, "--project-root", os.path.realpath(args.project),
            "--repository-key", config["repositoryKey"],
            "--usage-policy", config["usagePolicy"]] + getattr(args, "remainder", [])


def _usage_disabled(operation):
    return {"protocolVersion": "AWB-USAGE-POLICY-v1", "operation": operation,
            "status": "DISABLED", "policy": "OFF", "writes": 0}


def _usage_command(args):
    database = _project_database(args.project)
    policy = _project_usage_policy(args.project)
    if args.usage_command == "policy":
        if args.policy_command == "show":
            _print(usage_policy(args.project))
        else:
            _print(set_usage_policy(
                args.project, "BEST_EFFORT" if args.policy_command == "enable" else "OFF"
            ))
    elif args.usage_command == "sync":
        result = (_usage_disabled("SYNC") if policy == "OFF" else
                  usage.sync(database, args.work_item, args.all_bound, args.dry_run))
        if args.format == "json":
            _print(result)
        elif policy == "OFF":
            print("usagePolicy=OFF status=DISABLED writes=0")
        else:
            print(usage.render_sync_table(result))
    elif args.usage_command == "show":
        simulated = None
        if args.simulate_rate_card:
            with open(args.simulate_rate_card, "rb") as handle:
                simulated = handle.read()
        result = usage.project(database, args.group_by, args.from_time, args.to_time,
                               args.work_item_id, simulated)
        _print(result) if args.format == "json" else print(usage.render_table(result))
    elif args.usage_command == "export":
        _print(usage.export_report(database, args.output, args.format, args.group_by,
                                   args.from_time, args.to_time, args.work_item))
    elif args.usage_command == "correct":
        with open(args.correction_file, "r", encoding="utf-8") as handle:
            replacement = json.load(handle)
        _print(usage.correct(database, args.event, args.human, args.reason, replacement))
    elif args.usage_command == "span":
        if args.span_command == "begin":
            _print(_usage_disabled("SPAN_BEGIN") if policy == "OFF" else
                   usage.begin_span(database, args.work_item, args.task, args.agent,
                                    args.session_id, args.model))
        else:
            _print(usage.end_span(database, args.span, args.agent))
    elif args.usage_command == "cohort":
        payload = {}
        if args.payload_file:
            with open(args.payload_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        event_type = {"start": "COHORT_STARTED", "snapshot": "COHORT_SNAPSHOT",
                      "semantics-changed": "COHORT_SEMANTICS_CHANGED",
                      "conclude": "COHORT_CONCLUDED"}[args.cohort_command]
        _print(usage.cohort_event(database, event_type, args.cohort, args.actor,
                                  "HUMAN" if args.human else "AGENT", payload))
    else:
        _print(usage.self_check(database))


def _orchestrator_command(args):
    name = args.orchestrator_command
    try:
        database = _project_database(args.project)
        if name == "register":
            result = orchestrator.register(database, args.orchestrator, args.request_id)
        elif name == "claim":
            result = orchestrator.claim(database, args.work_item, args.orchestrator,
                                        args.ttl, args.request_id)
        elif name == "claim-next":
            result = orchestrator.claim_next(database, args.orchestrator, args.ttl,
                                             args.request_id)
        elif name == "renew":
            result = orchestrator.renew(database, args.work_item, args.orchestrator,
                                        args.generation, args.ttl, args.request_id)
        elif name == "release":
            result = orchestrator.release(database, args.work_item, args.orchestrator,
                                          args.generation, args.request_id)
        elif name == "recover":
            result = orchestrator.recover(database, args.work_item, args.orchestrator,
                                          args.ttl, args.request_id)
        elif name == "list":
            result = orchestrator.list_leases(database, args.orchestrator, args.status)
        else:
            result = orchestrator.show(database, args.work_item)
    except lite.LiteError:
        result = orchestrator.error_result(name)
    _print(result)
    return 2 if result["status"] in ("REFUSED", "CONFLICT") else 0


def _activity_command(args):
    database = _project_database(args.project)
    if args.activity_command == "list":
        result = orchestrator.list_activity(
            database, args.work_item, args.effective_status
        )
    elif args.activity_command == "show":
        result = orchestrator.show_activity(database, args.kind, args.resource_id)
    else:
        try:
            expected_activity = json.loads(args.expected_activity)
        except (TypeError, ValueError):
            raise lite.LiteError("expected activity must be exact JSON")
        result = orchestrator.reconcile_expired(
            database, args.work_item, args.kind, args.resource_id, args.owner,
            args.generation, args.request_id, fingerprint=args.fingerprint,
            expected_activity=expected_activity, not_after=args.not_after,
        )
    _print(result)
    return 2 if result["status"] in ("REFUSED", "CONFLICT") else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="awb")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--project", default=".")
    init.add_argument("--with-codex", action="store_true")
    init.add_argument("--development", action="store_true")
    init.add_argument("--wheel")
    boot = sub.add_parser("bootstrap")
    boot.add_argument("--project", default=".")
    check = sub.add_parser("doctor")
    check.add_argument("--project", default=".")
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("--project", default=".")
    migration = sub.add_parser("migrate")
    migration.add_argument("--project", default=".")
    migration.add_argument("--check", action="store_true")
    upgrade = sub.add_parser("upgrade")
    upgrade.add_argument("--project", default=".")
    upgrade_source = upgrade.add_mutually_exclusive_group(required=True)
    upgrade_source.add_argument("--wheel")
    upgrade_source.add_argument("--rollback")
    upgrade.add_argument("--check", action="store_true")
    upgrade.add_argument("--with-codex", action="store_true")
    upgrade.add_argument("--expected-stale-activity")
    upgrade.add_argument("--reconciliation-request-id")
    codex = sub.add_parser("codex")
    codex_sub = codex.add_subparsers(dest="codex_command", required=True)
    for name in ("install", "check"):
        command = codex_sub.add_parser(name)
        command.add_argument("--project", default=".")
    transfer = sub.add_parser("transfer")
    transfer_sub = transfer.add_subparsers(dest="transfer_command", required=True)
    export = transfer_sub.add_parser("export")
    export.add_argument("--database", required=True)
    export.add_argument("--work-item", action="append", required=True)
    export.add_argument("--output", required=True)
    imported = transfer_sub.add_parser("import")
    imported.add_argument("--database", required=True)
    imported.add_argument("--bundle", required=True)
    imported.add_argument("--check", action="store_true")
    usage_parser = sub.add_parser("usage")
    usage_sub = usage_parser.add_subparsers(dest="usage_command", required=True)
    usage_policy_parser = usage_sub.add_parser("policy")
    usage_policy_sub = usage_policy_parser.add_subparsers(dest="policy_command", required=True)
    for policy_name in ("show", "enable", "disable"):
        policy_command = usage_policy_sub.add_parser(policy_name)
        policy_command.add_argument("--project", default=".")
    usage_sync = usage_sub.add_parser("sync")
    usage_sync.add_argument("--project", default=".")
    usage_sync_scope = usage_sync.add_mutually_exclusive_group(required=True)
    usage_sync_scope.add_argument("--work-item")
    usage_sync_scope.add_argument("--all-bound", action="store_true")
    usage_sync.add_argument("--dry-run", action="store_true")
    usage_sync.add_argument("--format", choices=("json", "table"), default="json")
    usage_show = usage_sub.add_parser("show")
    usage_show.add_argument("work_item_id", nargs="?")
    usage_show.add_argument("--project", default=".")
    usage_show.add_argument("--from", dest="from_time")
    usage_show.add_argument("--to", dest="to_time")
    usage_show.add_argument("--group-by", choices=("work_item", "role", "agent", "model", "stage", "date"), default="role")
    usage_show.add_argument("--format", choices=("json", "table"), default="table")
    usage_show.add_argument("--simulate-rate-card")
    usage_export = usage_sub.add_parser("export")
    usage_export.add_argument("--project", default=".")
    usage_export.add_argument("--work-item")
    usage_export.add_argument("--from", dest="from_time")
    usage_export.add_argument("--to", dest="to_time")
    usage_export.add_argument("--group-by", choices=("work_item", "role", "agent", "model", "stage", "date"), default="role")
    usage_export.add_argument("--format", choices=("json", "csv"), required=True)
    usage_export.add_argument("--output", required=True)
    usage_correct = usage_sub.add_parser("correct")
    usage_correct.add_argument("--project", default=".")
    usage_correct.add_argument("--event", type=int, required=True)
    usage_correct.add_argument("--human", required=True)
    usage_correct.add_argument("--reason", required=True)
    usage_correct.add_argument("--correction-file", required=True)
    usage_span = usage_sub.add_parser("span")
    span_sub = usage_span.add_subparsers(dest="span_command", required=True)
    span_begin = span_sub.add_parser("begin")
    span_begin.add_argument("--project", default=".")
    span_begin.add_argument("--work-item", required=True)
    span_begin.add_argument("--task")
    span_begin.add_argument("--agent", required=True)
    span_begin.add_argument("--session-id", required=True)
    span_begin.add_argument("--model", required=True)
    span_end = span_sub.add_parser("end")
    span_end.add_argument("--project", default=".")
    span_end.add_argument("--span", required=True)
    span_end.add_argument("--agent", required=True)
    usage_cohort = usage_sub.add_parser("cohort")
    cohort_sub = usage_cohort.add_subparsers(dest="cohort_command", required=True)
    for cohort_name in ("start", "snapshot", "semantics-changed", "conclude"):
        cohort_command = cohort_sub.add_parser(cohort_name)
        cohort_command.add_argument("--project", default=".")
        cohort_command.add_argument("--cohort", required=True)
        cohort_command.add_argument("--actor", required=True)
        cohort_command.add_argument("--payload-file")
        cohort_command.add_argument("--human", action="store_true")
    usage_check = usage_sub.add_parser("self-check")
    usage_check.add_argument("--project", default=".")
    coordinator = sub.add_parser("orchestrator")
    coordinator_sub = coordinator.add_subparsers(dest="orchestrator_command", required=True)
    register = coordinator_sub.add_parser("register")
    register.add_argument("--project", default=".")
    register.add_argument("--orchestrator", required=True)
    register.add_argument("--request-id", required=True)
    coordinator_claim = coordinator_sub.add_parser("claim")
    coordinator_claim.add_argument("work_item", metavar="work-item")
    coordinator_claim.add_argument("--project", default=".")
    coordinator_claim.add_argument("--orchestrator", required=True)
    coordinator_claim.add_argument("--ttl", type=int, default=orchestrator.DEFAULT_TTL)
    coordinator_claim.add_argument("--request-id", required=True)
    claim_next = coordinator_sub.add_parser("claim-next")
    claim_next.add_argument("--project", default=".")
    claim_next.add_argument("--orchestrator", required=True)
    claim_next.add_argument("--ttl", type=int, default=orchestrator.DEFAULT_TTL)
    claim_next.add_argument("--request-id", required=True)
    for coordinator_name in ("renew", "release"):
        command = coordinator_sub.add_parser(coordinator_name)
        command.add_argument("work_item", metavar="work-item")
        command.add_argument("--project", default=".")
        command.add_argument("--orchestrator", required=True)
        command.add_argument("--generation", type=int, required=True)
        command.add_argument("--request-id", required=True)
        if coordinator_name == "renew":
            command.add_argument("--ttl", type=int, default=orchestrator.DEFAULT_TTL)
    recover = coordinator_sub.add_parser("recover")
    recover.add_argument("work_item", metavar="work-item")
    recover.add_argument("--project", default=".")
    recover.add_argument("--orchestrator", required=True)
    recover.add_argument("--ttl", type=int, default=orchestrator.DEFAULT_TTL)
    recover.add_argument("--request-id", required=True)
    coordinator_list = coordinator_sub.add_parser("list")
    coordinator_list.add_argument("--project", default=".")
    coordinator_list.add_argument("--orchestrator")
    coordinator_list.add_argument("--status", choices=("ACTIVE", "RELEASED", "EXPIRED"))
    coordinator_show = coordinator_sub.add_parser("show")
    coordinator_show.add_argument("work_item", metavar="work-item")
    coordinator_show.add_argument("--project", default=".")
    activity = sub.add_parser("activity")
    activity_sub = activity.add_subparsers(dest="activity_command", required=True)
    activity_list = activity_sub.add_parser("list")
    activity_list.add_argument("--project", default=".")
    activity_list.add_argument("--work-item")
    activity_list.add_argument("--effective-status", choices=("LIVE", "STALE", "INACTIVE"))
    activity_show = activity_sub.add_parser("show")
    activity_show.add_argument("--project", default=".")
    activity_show.add_argument("--kind", choices=("claim", "repository-writer", "orchestrator-lease"), required=True)
    activity_show.add_argument("--resource-id", required=True)
    activity_reconcile = activity_sub.add_parser("reconcile-expired")
    activity_reconcile.add_argument("--project", default=".")
    activity_reconcile.add_argument("--work-item", required=True)
    activity_reconcile.add_argument("--kind", choices=("claim", "repository-writer", "orchestrator-lease"), required=True)
    activity_reconcile.add_argument("--resource-id", required=True)
    activity_reconcile.add_argument("--owner", required=True)
    activity_reconcile.add_argument("--generation", type=int, required=True)
    activity_reconcile.add_argument("--request-id", required=True)
    activity_reconcile.add_argument("--fingerprint", required=True)
    activity_reconcile.add_argument("--expected-activity", required=True)
    activity_reconcile.add_argument("--not-after")
    workflow = sub.add_parser("workflow")
    workflow_sub = workflow.add_subparsers(dest="workflow_command", required=True)
    for workflow_name in ("status", "advance"):
        command = workflow_sub.add_parser(workflow_name)
        command.add_argument("work_item", metavar="work-item")
        command.add_argument("--project", default=".")
        command.add_argument("--agent", required=True)
        command.add_argument("--role", required=True, choices=("PLANNER", "IMPLEMENTER", "REVIEWER"))
        command.add_argument("--orchestrator-id")
        command.add_argument("--orchestrator-generation", type=int)
        if workflow_name == "advance":
            command.add_argument("--expected-step", required=True)
            command.add_argument("--expected-row-version", type=int, required=True)
            command.add_argument("--request-id", required=True)
            command.add_argument("--ttl", type=int, default=900)
            command.add_argument("--plan-artifact")
            command.add_argument("--submission-file")
            command.add_argument("--quality-file")
            command.add_argument("--review-file")
            command.add_argument("--decision", choices=("APPROVED", "REJECTED"))
            command.add_argument("--local-tests-passed", action="store_true")
            command.add_argument("--candidate")
            command.add_argument("--candidate-fingerprint")
    workflow_check = workflow_sub.add_parser("check")
    workflow_check.add_argument("work_item", metavar="work-item", nargs="?")
    workflow_check.add_argument("--project", default=".")
    workflow_check.add_argument("--all", action="store_true")
    workflow_repair = workflow_sub.add_parser("repair")
    workflow_repair.add_argument("work_item", metavar="work-item")
    workflow_repair.add_argument("--project", default=".")
    workflow_repair.add_argument("--apply", action="store_true")
    workflow_repair.add_argument("--action", required=True)
    workflow_repair.add_argument("--fingerprint", required=True)
    workflow_repair.add_argument("--request-id", required=True)
    workflow_repair.add_argument("--human", required=True)
    candidate_parser = sub.add_parser("candidate")
    candidate_sub = candidate_parser.add_subparsers(dest="candidate_command", required=True)
    candidate_status = candidate_sub.add_parser("status")
    candidate_status.add_argument("work_item", metavar="work-item")
    candidate_status.add_argument("--project", default=".")
    candidate_status.add_argument("--candidate", required=True)
    for candidate_name in ("prepare", "freeze", "build", "quarantine", "finalize"):
        command = candidate_sub.add_parser(candidate_name)
        command.add_argument("work_item", metavar="work-item")
        command.add_argument("--project", default=".")
        command.add_argument("--candidate", required=True)
        command.add_argument("--owner", required=True)
        command.add_argument("--request-id", required=True)
        command.add_argument("--orchestrator-id")
        command.add_argument("--orchestrator-generation", type=int)
        if candidate_name == "prepare":
            command.add_argument("--source", required=True)
            command.add_argument("--base-file", required=True)
            command.add_argument("--target-file", required=True)
        elif candidate_name == "freeze":
            command.add_argument("--allowlist-file", required=True)
        elif candidate_name == "build":
            command.add_argument("--toolchain-file", required=True)
        elif candidate_name == "quarantine":
            command.add_argument("--restore", action="store_true")
            command.add_argument("--quarantine-request-id")
    publication = sub.add_parser("publication")
    publication_sub = publication.add_subparsers(dest="publication_command", required=True)
    publication_status = publication_sub.add_parser("status")
    publication_status.add_argument("work_item", metavar="work-item")
    publication_status.add_argument("--project", default=".")
    publication_status.add_argument("--evidence-file")
    publication_authorize = publication_sub.add_parser("authorize")
    publication_authorize.add_argument("work_item", metavar="work-item")
    publication_authorize.add_argument("--project", default=".")
    publication_authorize.add_argument("--human", required=True)
    publication_authorize.add_argument("--candidate", required=True)
    publication_authorize.add_argument("--candidate-fingerprint", required=True)
    publication_authorize.add_argument("--authorization-file", required=True)
    publication_authorize.add_argument("--request-id", required=True)
    publication_postflight = publication_sub.add_parser("postflight")
    publication_postflight.add_argument("work_item", metavar="work-item")
    publication_postflight.add_argument("--project", default=".")
    publication_postflight.add_argument("--operator", required=True)
    publication_postflight.add_argument("--ready-fingerprint", required=True)
    publication_postflight.add_argument("--authorization-request-id", required=True)
    publication_postflight.add_argument("--evidence-file", required=True)
    publication_postflight.add_argument("--request-id", required=True)
    publication_retry = publication_sub.add_parser("retry")
    publication_retry.add_argument("work_item", metavar="work-item")
    publication_retry.add_argument("--project", default=".")
    publication_retry.add_argument("--human", required=True)
    publication_retry.add_argument("--ready-fingerprint", required=True)
    publication_retry.add_argument("--retry-file", required=True)
    publication_retry.add_argument("--request-id", required=True)
    lite_parser = sub.add_parser("lite", help="compatibility access to MVP-LITE commands")
    lite_parser.add_argument("--project", default=".")
    lite_parser.add_argument("--database")
    lite_parser.add_argument("--development", action="store_true")
    lite_parser.add_argument("remainder", nargs=argparse.REMAINDER)
    serve = sub.add_parser("serve")
    serve.add_argument("--project", default=".")
    serve.add_argument("--database")
    serve.add_argument("--development", action="store_true")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            _print(init_project(args.project, args.with_codex, args.development, args.wheel))
        elif args.command == "bootstrap":
            _print(bootstrap(args.project))
        elif args.command == "doctor":
            _print(doctor(args.project))
        elif args.command == "backup":
            _print(backup(args.project))
        elif args.command == "migrate":
            _print(migrate(args.project, args.check))
        elif args.command == "upgrade":
            if args.rollback and args.with_codex:
                parser.error("--with-codex is only valid with --wheel")
            result = upgrade_project(
                args.project, args.wheel, args.with_codex, check=args.check,
                rollback_manifest=args.rollback,
                expected_stale_activity=args.expected_stale_activity,
                reconciliation_request_id=args.reconciliation_request_id,
            )
            _print(result)
            return 2 if result["status"] in ("REFUSED", "BLOCKED") else 0
        elif args.command == "codex":
            _print(codex_install(args.project) if args.codex_command == "install" else codex_check(args.project))
        elif args.command == "transfer":
            if args.transfer_command == "export":
                _print(transfer_export(args.database, args.work_item, args.output))
            else:
                _print(transfer_import(args.database, args.bundle, args.check))
        elif args.command == "usage":
            _usage_command(args)
        elif args.command == "orchestrator":
            return _orchestrator_command(args)
        elif args.command == "activity":
            return _activity_command(args)
        elif args.command == "workflow":
            return _workflow_command(args)
        elif args.command == "candidate":
            return _candidate_command(args)
        elif args.command == "publication":
            return _publication_command(args)
        elif args.command == "serve":
            database = _lite_args(args)
            return lite.main(database + ["serve", "--host", args.host, "--port", str(args.port)])
        else:
            return lite.main(_lite_args(args))
        return 0
    except (lite.LiteError, usage.UsageError, IOError, ValueError) as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 2
