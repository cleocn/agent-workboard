"""The public local-only command line entry point."""

import argparse
import json
import os
import sys

from . import __version__
from . import lite
from . import orchestrator
from . import usage
from .project import (backup, bootstrap, codex_check, codex_install, doctor,
                      init_project, migrate, transfer_export, transfer_import,
                      upgrade_project)


def _print(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _project_database(path, development=False):
    from .project import _load_config, _project_root, _validate_identity
    config, database = _load_config(_project_root(path))
    if development and config["runtimeMode"] != "development":
        raise lite.LiteError("--development requires a development configuration")
    _validate_identity(config, database)
    return database


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
    return ["--database", database] + getattr(args, "remainder", [])


def _usage_command(args):
    database = _project_database(args.project)
    if args.usage_command == "sync":
        result = usage.sync(database, args.work_item, args.all_bound, args.dry_run)
        _print(result) if args.format == "json" else print(usage.render_sync_table(result))
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
            _print(usage.begin_span(database, args.work_item, args.task, args.agent,
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
            result = upgrade_project(args.project, args.wheel, args.with_codex,
                                     check=args.check, rollback_manifest=args.rollback)
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
        elif args.command == "serve":
            database = _lite_args(args)
            return lite.main(database + ["serve", "--host", args.host, "--port", str(args.port)])
        else:
            return lite.main(_lite_args(args))
        return 0
    except (lite.LiteError, usage.UsageError, IOError, ValueError) as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 2
