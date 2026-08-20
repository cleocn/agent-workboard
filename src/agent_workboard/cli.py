"""The public local-only command line entry point."""

import argparse
import json
import os
import sys

from . import __version__
from . import lite
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
    upgrade.add_argument("--wheel", required=True)
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
            _print(upgrade_project(args.project, args.wheel, args.with_codex))
        elif args.command == "codex":
            _print(codex_install(args.project) if args.codex_command == "install" else codex_check(args.project))
        elif args.command == "transfer":
            if args.transfer_command == "export":
                _print(transfer_export(args.database, args.work_item, args.output))
            else:
                _print(transfer_import(args.database, args.bundle, args.check))
        elif args.command == "serve":
            database = _lite_args(args)
            return lite.main(database + ["serve", "--host", args.host, "--port", str(args.port)])
        else:
            return lite.main(_lite_args(args))
        return 0
    except lite.LiteError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 2
