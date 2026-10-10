"""Command-line interface: ``dbbackup backup|list|verify|prune|restore|inspect|restore-test``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sys
from datetime import timedelta
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .engines import Connection, EngineError, engine_for
from .notify import publish_statuses
from .proc import CommandError
from .restoretest import DEFAULT_TOLERANCE, run_restore_test, target_for_set
from .runner import backup_targets, human_size
from .storage import StorageError, Store, open_set, verify_checksums

log = logging.getLogger("dbbackup")

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )
    try:
        return args.func(args)
    except ConfigError as e:
        log.error("config: %s", e)
        return EXIT_USAGE
    except (StorageError, EngineError, CommandError) as e:
        log.error("%s", e)
        return EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dbbackup", description="Back up and restore MariaDB, PostgreSQL and MongoDB servers.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("DBBACKUP_CONFIG", "/etc/dbbackup/config.yaml"),
        help="config file (default: $DBBACKUP_CONFIG or /etc/dbbackup/config.yaml)",
    )
    parser.add_argument("--log-level", default=os.environ.get("DBBACKUP_LOG_LEVEL", "info"), choices=["debug", "info", "warning", "error"])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backup", help="back up targets (all of them by default), apply retention, publish status")
    p.add_argument("targets", nargs="*", help="target names (default: all)")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("list", help="list backup sets")
    p.add_argument("targets", nargs="*", help="target names (default: all)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("verify", help="re-check backup sets' checksums and dump integrity")
    p.add_argument("targets", nargs="*", help="target names (default: all)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--set", default="latest", help="set to verify: 'latest' (default) or a timestamp")
    group.add_argument("--all", action="store_true", help="verify every set")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("prune", help="apply retention without backing up")
    p.add_argument("targets", nargs="*", help="target names (default: all)")
    p.add_argument("-n", "--dry-run", action="store_true", help="only show what would be deleted")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("restore", help="restore a backup set to its server or another one")
    p.add_argument("target", help="target name")
    p.add_argument("--set", default="latest", help="set to restore: 'latest' (default) or a timestamp")
    p.add_argument("-d", "--database", action="append", dest="databases", help="restore only this database (repeatable)")
    p.add_argument("--host", help="restore to this host instead of the target's")
    p.add_argument("--port", type=int, help="restore to this port instead of the target's")
    p.add_argument("--username", help="connect as this user instead of the target's")
    p.add_argument("--password-env", metavar="VAR", help="environment variable holding the password for --username")
    p.add_argument("--uri-env", metavar="VAR", help="MongoDB: environment variable holding a connection URI to restore to")
    p.add_argument("--no-globals", action="store_true", help="don't restore users/roles")
    p.add_argument("--force", action="store_true", help="replace databases that already exist")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("inspect", help="describe a backup set (no config needed)")
    p.add_argument("path", help="a set directory, or a target directory (then --set picks the set)")
    p.add_argument("--set", default="latest", help="with a target directory: 'latest' (default) or a timestamp")
    p.add_argument("--format", choices=["text", "env", "json"], default="text", help="env: shell-quoted KEY=value lines")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("restore-test", help="restore a set into a scratch server and check it against the backup's inventory")
    p.add_argument("path", help="a set directory, or a target directory (then --set picks the set)")
    p.add_argument("--set", default="latest", help="with a target directory: 'latest' (default) or a timestamp")
    p.add_argument("--host", help="the scratch server")
    p.add_argument("--port", type=int, help="its port (default: the server type's)")
    p.add_argument("--username", help="an admin user on the scratch server")
    p.add_argument("--password-env", metavar="VAR", help="environment variable holding that user's password")
    p.add_argument("--uri-env", metavar="VAR", help="MongoDB: environment variable holding the scratch server's URI")
    p.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help=f"allowed relative difference in estimated row counts (default {DEFAULT_TOLERANCE})",
    )
    p.set_defaults(func=cmd_restore_test)

    return parser


def cmd_backup(args) -> int:
    config = load_config(args.config, secrets_for=args.targets or None)
    targets = config.select(args.targets)
    statuses = backup_targets(config, targets)
    publish_statuses(config.mqtt, statuses)
    failed = [s["target"] for s in statuses if s["state"] != "ok"]
    if failed:
        log.error("backup failed for %d of %d target(s): %s", len(failed), len(statuses), ", ".join(failed))
        return EXIT_FAILED
    log.info("all %d target(s) backed up", len(statuses))
    return EXIT_OK


def cmd_list(args) -> int:
    config = load_config(args.config, secrets_for=(), mqtt_secrets=False)
    store = Store(config.backup_root)
    print(f"{'TARGET':<20} {'SET':<17} {'STATUS':<7} {'SIZE':>10} {'DBS':>4} {'WARN':>4}  SERVER")
    for target in config.select(args.targets):
        for s in store.sets(target.name):
            m = s.manifest or {}
            print(
                f"{target.name:<20} {s.name:<17} {'ok' if s.ok else 'BAD':<7} {human_size(m.get('size_bytes')):>10} "
                f"{len(m.get('databases', [])):>4} {len(m.get('warnings', [])):>4}  {m.get('server_version', '-')}"
            )
    return EXIT_OK


def cmd_verify(args) -> int:
    config = load_config(args.config, secrets_for=(), mqtt_secrets=False)
    store = Store(config.backup_root)
    failures = 0
    for target in config.select(args.targets):
        if args.all:
            sets = store.sets(target.name)
        else:
            try:
                sets = [store.find(target.name, args.set)]
            except StorageError as e:
                log.error("%s", e)
                failures += 1
                continue
        for s in sets:
            problems = ["no valid manifest"] if not s.ok else verify_checksums(s)
            if not problems:
                try:
                    engine_for(target).verify(s.path, s.manifest)
                except (EngineError, CommandError) as e:
                    problems.append(str(e))
            if problems:
                failures += 1
                for problem in problems:
                    log.error("%s/%s: %s", target.name, s.name, problem)
            else:
                log.info("%s/%s: ok", target.name, s.name)
    return EXIT_FAILED if failures else EXIT_OK


def cmd_prune(args) -> int:
    config = load_config(args.config, secrets_for=(), mqtt_secrets=False)
    store = Store(config.backup_root)
    for target in config.select(args.targets):
        with store.lock(target.name, timedelta(hours=config.lock_stale_hours)):
            store.prune(
                target.name, target.retention, stale_partial_after=timedelta(hours=config.stale_partial_hours), dry_run=args.dry_run
            )
    return EXIT_OK


def cmd_restore(args) -> int:
    overriding = bool(args.host or args.uri_env or args.username)
    config = load_config(args.config, secrets_for=() if overriding else [args.target], mqtt_secrets=False)
    (target,) = config.select([args.target])
    store = Store(config.backup_root)
    bset = store.find(target.name, args.set)

    conn = connection_from_args(args, target) if overriding else Connection.for_target(target)
    if args.port and not overriding:
        conn = conn.overridden(port=args.port)

    problems = verify_checksums(bset)
    if problems:
        raise StorageError(f"{target.name}/{bset.name} failed its checksum check: {'; '.join(problems)}")

    databases = args.databases or list(bset.manifest["databases"])
    unknown = [d for d in databases if d not in bset.manifest["databases"]]
    if unknown:
        raise ConfigError(f"database(s) not in set {bset.name}: {', '.join(unknown)}")

    engine = engine_for(target, conn)
    log.info(
        "%s: restoring set %s (%s %s) to %s: %s",
        target.name,
        bset.name,
        target.type,
        bset.manifest.get("server_version"),
        conn.host or "the server in the given URI",
        ", ".join(databases) or "(no databases)",
    )
    warnings = engine.restore(bset.path, bset.manifest, databases, force=args.force, include_globals=not args.no_globals)
    for warning in warnings:
        log.warning("%s: %s", target.name, warning)
    log.info("%s: restore complete%s", target.name, f" with {len(warnings)} warning(s)" if warnings else "")
    return EXIT_OK


def connection_from_args(args, target) -> Connection:
    """The connection given by --host/--port/--username/--password-env/--uri-env, defaulting to the target's."""
    password = None
    if args.password_env:
        if args.password_env not in os.environ:
            raise ConfigError(f"--password-env: environment variable {args.password_env} is not set")
        password = os.environ[args.password_env]
    uri = None
    if args.uri_env:
        if target.type != "mongodb":
            raise ConfigError("--uri-env is only for mongodb targets")
        if args.uri_env not in os.environ:
            raise ConfigError(f"--uri-env: environment variable {args.uri_env} is not set")
        uri = os.environ[args.uri_env]
    return Connection(
        # A URI says where it points; don't leave the target's host around to mislead the logs.
        host=None if uri else (args.host or target.host),
        port=args.port or target.port,
        username=args.username or target.username,
        password=password,
        uri=uri,
    )


def cmd_inspect(args) -> int:
    bset = open_set(Path(args.path), args.set)
    if not bset.ok:
        raise StorageError(f"{bset.path} has no valid manifest")
    m = bset.manifest
    if args.format == "json":
        print(json.dumps({k: v for k, v in m.items() if k != "files"}, indent=2, sort_keys=True))
    elif args.format == "env":
        values = {
            "DBB_TARGET": m["target"],
            "DBB_TYPE": m["type"],
            "DBB_SET": bset.name,
            "DBB_SERVER_VERSION": m.get("server_version", ""),
            "DBB_DATABASES": str(len(m.get("databases", []))),
            "DBB_SIZE_BYTES": str(m.get("size_bytes", "")),
        }
        for key, value in values.items():
            print(f"{key}={shlex.quote(value)}")
    else:
        print(f"target:   {m['target']} ({m['type']} {m.get('server_version', '?')}, {m.get('host') or 'URI'})")
        print(f"set:      {bset.name}  finished {m.get('finished')}  ({human_size(m.get('size_bytes'))}, {m.get('duration_s')}s)")
        print(f"contents: {', '.join(m.get('databases', [])) or '(no databases)'}")
        for warning in m.get("warnings", []):
            print(f"warning:  {warning}")
    return EXIT_OK


def cmd_restore_test(args) -> int:
    bset = open_set(Path(args.path), args.set)
    if not bset.ok:
        raise StorageError(f"{bset.path} has no valid manifest")
    target = target_for_set(bset)
    if not (args.host or args.uri_env):
        raise ConfigError("restore-test needs the scratch server: --host (with --username/--password-env), or --uri-env")
    report = run_restore_test(bset, connection_from_args(args, target), tolerance=args.tolerance)

    name = f"{report.target}/{report.set}"
    for warning in report.restore_warnings:
        log.warning("%s: restore: %s", name, warning)
    for note in report.check.notes:
        log.info("%s: %s", name, note)
    for warning in report.check.warnings:
        log.warning("%s: %s", name, warning)
    for error in report.check.errors:
        log.error("%s: %s", name, error)
    c = report.check
    summary = (
        f"{len(bset.manifest['databases'])} database(s) restored in {report.restore_seconds:.1f}s; "
        f"checked {c.objects_checked} object(s), {c.counts_checked} row count(s), {c.users_checked} user(s)/role(s): "
        f"{len(c.errors)} error(s), {len(c.warnings) + len(report.restore_warnings)} warning(s)"
    )
    if report.passed:
        log.info("%s: PASSED: %s", name, summary)
        return EXIT_OK
    log.error("%s: FAILED: %s", name, summary)
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
