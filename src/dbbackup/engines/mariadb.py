"""MariaDB: ``mariadb-dump --system=users`` for accounts and grants, plus one
``--single-transaction`` dump per database, each piped through zstd."""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Iterator
from pathlib import Path

from .. import proc
from .base import DumpResult, Engine, EngineError, safe_filename

log = logging.getLogger(__name__)

SYSTEM_DATABASES = frozenset({"information_schema", "performance_schema", "sys", "mysql"})
USERS_FILE = "system-users.sql.zst"
TRAILER = b"-- Dump completed"
MAX_PACKET = "--max-allowed-packet=1G"

TABLE_KINDS = {"BASE TABLE": "table", "SYSTEM VERSIONED": "table", "VIEW": "view", "SEQUENCE": "sequence"}
_UNESCAPE = re.compile(r"\\(.)")
_UNESCAPES = {"n": "\n", "t": "\t", "0": "\0", "\\": "\\"}


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def quote_str(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def option_file_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# An account name part: `backticked`, 'single-quoted', or bare.
_NAME = r"(?:`(?:[^`]|``)*`|'(?:[^'\\]|\\.|'')*'|[A-Za-z0-9_$.]+)"
_ACCOUNT = rf"(?P<user>{_NAME})(?:@(?P<host>{_NAME}))?"
# Statements in a --system=users dump that create or change one account (user or role).
_ACCOUNT_STATEMENTS = [
    re.compile(rf"^CREATE (?:USER|ROLE) IF NOT EXISTS {_ACCOUNT}(?:\s|;|$)"),
    re.compile(rf"^ALTER USER {_ACCOUNT}(?:\s|;|$)"),
    re.compile(rf"^SET DEFAULT ROLE \S+ FOR {_ACCOUNT}(?:\s|;|$)"),
    re.compile(rf"^GRANT .*? TO {_ACCOUNT}(?:\s|;|$)"),
]
# Version-conditional comments, /*!80001 ... */ and /*M!100005 ... */, wrap many of them.
_CONDITIONAL = re.compile(r"^/\*M?!\d+\s+(.*?)\s*\*/;?$")


def _unquote(name: str) -> str:
    if name[0] == "`":
        return name[1:-1].replace("``", "`")
    if name[0] == "'":
        return re.sub(r"\\(.)|''", lambda m: m.group(1) or "'", name[1:-1])
    return name


def statement_account(line: str) -> tuple[str, str | None] | None:
    """The (user, host) a users-dump statement creates or changes; host is None for a role. None if no single account."""
    m = _CONDITIONAL.match(line)
    body = m.group(1) if m else line
    for pattern in _ACCOUNT_STATEMENTS:
        if m := pattern.match(body):
            return _unquote(m["user"]), _unquote(m["host"]) if m["host"] else None
    return None


def without_accounts(sql: str, existing: set[tuple[str, str | None]]) -> tuple[str, set[tuple[str, str | None]]]:
    """Drop the statements in a ``--system=users`` dump that would create or change an account in ``existing``.

    Returns the remaining SQL, and the accounts that were left alone.
    """
    kept = []
    skipped = set()
    for line in sql.splitlines():
        account = statement_account(line)
        if account is not None and account in existing:
            skipped.add(account)
        else:
            kept.append(line)
    return "\n".join(kept) + "\n", skipped


def format_account(account: tuple[str, str | None]) -> str:
    user, host = account
    return user if host is None else f"{user}@{host}"


class MariaDBEngine(Engine):
    type = "mariadb"
    min_version = (11, 4)

    # -- connection helpers ----------------------------------------------------

    @contextlib.contextmanager
    def _client_args(self) -> Iterator[list[str]]:
        """Connection arguments; the credentials go in a 0600 option file, which must be the first argument."""
        lines = ["[client]"]
        if self.conn.username:
            lines.append(f"user={option_file_value(self.conn.username)}")
        if self.conn.password is not None:
            lines.append(f"password={option_file_value(self.conn.password)}")
        with proc.secret_file("\n".join(lines) + "\n", suffix=".cnf") as path:
            args = [f"--defaults-extra-file={path}", "-h", self.conn.host, "-P", str(self.conn.port), "--protocol=TCP"]
            if self.target.ssl is True:
                args.append("--ssl")
            elif self.target.ssl is False:
                args.append("--skip-ssl")
            yield args

    def query(self, sql: str) -> list[list[str | None]]:
        with self._client_args() as args:
            out = proc.run(["mariadb", *args, "--batch", "--skip-column-names", "-e", sql]).stdout
        rows = []
        for line in out.splitlines():
            rows.append(
                [None if f == "NULL" else _UNESCAPE.sub(lambda m: _UNESCAPES.get(m.group(1), m.group(1)), f) for f in line.split("\t")]
            )
        return rows

    # -- discovery -------------------------------------------------------------

    def server_version(self) -> str:
        return self.query("SELECT VERSION()")[0][0]

    def list_databases(self) -> list[str]:
        return sorted(r[0] for r in self.query("SHOW DATABASES") if r[0] not in SYSTEM_DATABASES)

    def _tables(self, databases: list[str]) -> list[list[str | None]]:
        if not databases:
            return []
        schemas = ", ".join(quote_str(d) for d in databases)
        return self.query(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_ROWS FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA IN ({schemas})"
        )

    def inventory(self, databases: list[str], *, exact: bool = False) -> dict:
        result = {db: {"objects": {}} for db in databases}
        countable = []
        for schema, name, ttype, engine, rows in self._tables(databases):
            kind = TABLE_KINDS.get(ttype, ttype.lower())
            # InnoDB's TABLE_ROWS is a statistics estimate that can be off by orders of
            # magnitude (e.g. right after a bulk load), so don't record it; other
            # engines' (Aria, MyISAM) are exact.
            estimate = int(rows) if kind == "table" and rows is not None and engine != "InnoDB" else None
            result[schema]["objects"][name] = {"kind": kind, "rows": None if exact else estimate}
            if exact and kind == "table":
                countable.append((schema, name))
        if countable:
            sql = " UNION ALL ".join(f"SELECT {i}, COUNT(*) FROM {quote_ident(s)}.{quote_ident(n)}" for i, (s, n) in enumerate(countable))
            for i, count in self.query(sql):
                schema, name = countable[int(i)]
                result[schema]["objects"][name]["rows"] = int(count)
        users = sorted(
            name if is_role == "Y" else f"{name}@{host}" for name, host, is_role in self.query("SELECT User, Host, is_role FROM mysql.user")
        )
        return {"counts": "exact" if exact else "estimated", "databases": result, "users": users}

    # -- backup ----------------------------------------------------------------

    def backup(self, set_dir: Path, databases: list[str]) -> DumpResult:
        warnings = []
        for schema, name, ttype, engine, _rows in self._tables(databases):
            if ttype == "BASE TABLE" and engine and engine != "InnoDB":
                warnings.append(f"{schema}.{name} uses {engine}, not InnoDB, so its dump isn't transactionally consistent")

        with self._client_args() as args:
            log.info("%s: dumping users and grants", self.target.name)
            proc.pipe_to_zstd(
                ["mariadb-dump", *args, "--system=users", "--insert-ignore"], set_dir / USERS_FILE, self.target.compression_level
            )

            files = {}
            for db in databases:
                name = safe_filename(db) + ".sql.zst"
                log.info("%s: dumping database %s", self.target.name, db)
                cmd = [
                    "mariadb-dump",
                    *args,
                    "--single-transaction",
                    "--routines",
                    "--events",
                    "--triggers",
                    "--hex-blob",
                    MAX_PACKET,
                    *self.target.dump_args,
                    "--databases",
                    db,
                ]
                stderr = proc.pipe_to_zstd(cmd, set_dir / name, self.target.compression_level)
                warnings += [f"{db}: {line.strip()}" for line in stderr.splitlines() if "warning" in line.lower()]
                files[db] = name

        return DumpResult({"users": USERS_FILE, "databases": files}, warnings)

    def verify(self, set_dir: Path, manifest: dict) -> None:
        contents = manifest["contents"]
        for label, name in [("users", contents["users"]), *contents["databases"].items()]:
            try:
                _, tail = proc.zstd_head_tail(set_dir / name)
            except proc.CommandError as e:
                raise EngineError(f"{label}: {name} is corrupt: {e}") from e
            if TRAILER not in tail:
                raise EngineError(f"{label}: {name} is incomplete (no completion trailer)")

    # -- restore ---------------------------------------------------------------

    def restore(self, set_dir: Path, manifest: dict, databases: list[str], *, force: bool, include_globals: bool) -> list[str]:
        contents = manifest["contents"]
        warnings = []

        existing = set(self.list_databases())
        clashes = [db for db in databases if db in existing]
        if clashes and not force:
            raise EngineError(f"database(s) already exist on {self.conn.host}: {', '.join(clashes)} (use --force to replace them)")

        # Databases first: table-level grants can only be applied once their tables exist.
        with self._client_args() as args:
            for db in databases:
                if db in clashes:
                    log.warning("%s: dropping existing database %s (--force)", self.target.name, db)
                    self.query(f"DROP DATABASE {quote_ident(db)}")
                log.info("%s: restoring database %s", self.target.name, db)
                try:
                    proc.zstd_pipe_from(set_dir / contents["databases"][db], ["mariadb", *args, MAX_PACKET])
                except proc.CommandError as e:
                    raise EngineError(f"{db}: restore failed: {e}") from e

            if include_globals:
                log.info("%s: restoring users and grants", self.target.name)
                warnings += self._restore_users(set_dir / contents["users"], args)
        return warnings

    def _restore_users(self, path: Path, args: list[str]) -> list[str]:
        # Accounts that already exist on the destination (including the one we're
        # restoring as, and on a fresh server its own root and healthcheck accounts)
        # are left exactly as they are: replaying their GRANT ... IDENTIFIED BY
        # PASSWORD would reset their passwords to the backed-up server's.
        existing = {
            (user, None if is_role == "Y" else host) for user, host, is_role in self.query("SELECT User, Host, is_role FROM mysql.user")
        }
        sql, skipped = without_accounts(proc.run(["zstd", "-q", "-dc", str(path)]).stdout, existing)
        if skipped:
            log.info("%s: left existing account(s) as they are: %s", self.target.name, ", ".join(sorted(map(format_account, skipped))))
        # --force: carry on past individual failures (e.g. grants on tables that weren't restored) and report them.
        result = proc.run(["mariadb", *args, "--force"], input=sql, check=False)
        return [f"users: {line.strip()}" for line in result.stderr.splitlines() if line.startswith("ERROR")]
