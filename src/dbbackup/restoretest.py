"""Restore testing: restore a set into a scratch server, then check the result
against the inventory its manifest recorded at backup time.

Anything missing (a database, table, view, collection, user or role) is an error.
Row-count differences are only warnings. The restored side is always counted
exactly (it's a scratch server); what it's compared with depends on the backup:

- ``counts: exact`` (the target's ``exact_counts``): COUNT(*)s taken just before
  the dump, compared exactly. Writes between the count and the dump's snapshot
  show up as small differences.
- ``counts: estimated``: the servers' cheap estimates (pg_class.reltuples,
  estimatedDocumentCount; Aria/MyISAM row counts), compared within a tolerance.
- No ``counts`` key (backups made by 0.1.x): as estimated, except MariaDB, whose
  sets recorded InnoDB's TABLE_ROWS, an estimate too unreliable to compare.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .config import Target, parse_config
from .engines import Connection, engine_for
from .storage import BackupSet, StorageError, verify_checksums

log = logging.getLogger(__name__)

DEFAULT_TOLERANCE = 0.25
# Below this many rows' difference, estimates are never worth a warning.
COUNT_SLACK = 100


@dataclass
class CheckResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    objects_checked: int = 0
    counts_checked: int = 0
    users_checked: int = 0
    notes: list[str] = field(default_factory=list)


def counts_match(expected: int, actual: int, tolerance: float, slack: int = COUNT_SLACK) -> bool:
    return abs(expected - actual) <= max(tolerance * max(expected, actual), slack)


def compare_inventory(
    expected: dict, actual: dict, present: set[str], *, tolerance: float = DEFAULT_TOLERANCE, server_type: str | None = None
) -> CheckResult:
    """Check a restored server's inventory (``actual``, counted exactly) against the manifest's (``expected``).

    ``present`` is the set of databases that exist on the restored server.
    """
    result = CheckResult()
    mode = expected.get("counts")
    exact = mode == "exact"
    compare_counts = not (mode is None and server_type == "mariadb")
    if not compare_counts:
        result.notes.append("row counts not compared: this backup recorded InnoDB's TABLE_ROWS estimates, which are too unreliable")
    for db, info in sorted(expected.get("databases", {}).items()):
        if db not in present:
            result.errors.append(f"database {db} is missing")
            continue
        restored = actual.get("databases", {}).get(db, {}).get("objects", {})
        for name, obj in sorted(info.get("objects", {}).items()):
            result.objects_checked += 1
            got = restored.get(name)
            if got is None:
                result.errors.append(f"{db}: {obj['kind']} {name} is missing")
                continue
            if got["kind"] != obj["kind"]:
                result.errors.append(f"{db}: {name} was a {obj['kind']} but was restored as a {got['kind']}")
                continue
            if compare_counts and obj.get("rows") is not None and got.get("rows") is not None:
                result.counts_checked += 1
                if exact and got["rows"] != obj["rows"]:
                    result.warnings.append(f"{db}: {name} has {got['rows']} rows, but {obj['rows']} were counted at backup time")
                elif not exact and not counts_match(obj["rows"], got["rows"], tolerance):
                    result.warnings.append(f"{db}: {name} has {got['rows']} rows, but the backup estimated about {obj['rows']}")

    expected_users = expected.get("users")
    actual_users = actual.get("users")
    if expected_users is not None and actual_users is not None:
        result.users_checked = len(expected_users)
        for user in sorted(set(expected_users) - set(actual_users)):
            result.errors.append(f"user/role {user} is missing")
    elif expected_users is not None:
        result.warnings.append("couldn't list the restored server's users, so they weren't checked")
    return result


def target_for_set(bset: BackupSet) -> Target:
    """A Target with default settings for the set's server type: restore testing needs no config file."""
    manifest = bset.manifest
    raw = {"targets": [{"name": manifest["target"], "type": manifest["type"], "host": "unused"}]}
    return parse_config(raw, {}).targets[0]


@dataclass
class RestoreTestReport:
    target: str
    set: str
    restore_seconds: float
    check: CheckResult
    restore_warnings: list[str]

    @property
    def passed(self) -> bool:
        return not self.check.errors


def run_restore_test(bset: BackupSet, conn: Connection, *, tolerance: float = DEFAULT_TOLERANCE) -> RestoreTestReport:
    """Verify, restore and check one set against a scratch server reached through ``conn``."""
    if not bset.ok:
        raise StorageError(f"{bset.path} has no valid manifest")
    problems = verify_checksums(bset)
    if problems:
        raise StorageError(f"{bset.target}/{bset.name} failed its checksum check: {'; '.join(problems)}")

    target = target_for_set(bset)
    engine = engine_for(target, conn)
    engine.verify(bset.path, bset.manifest)
    databases = list(bset.manifest["databases"])

    log.info("%s/%s: restoring %d database(s) into the scratch server", bset.target, bset.name, len(databases))
    started = time.monotonic()
    restore_warnings = engine.restore(bset.path, bset.manifest, databases, force=False, include_globals=True)
    restore_seconds = time.monotonic() - started

    log.info("%s/%s: checking the restored server against the backup's inventory", bset.target, bset.name)
    present = set(engine.list_databases())
    actual = engine.inventory([d for d in databases if d in present], exact=True)
    check = compare_inventory(bset.manifest.get("inventory", {}), actual, present, tolerance=tolerance, server_type=target.type)
    return RestoreTestReport(bset.target, bset.name, restore_seconds, check, restore_warnings)
