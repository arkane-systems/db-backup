"""PostgreSQL: ``pg_dumpall --globals-only`` for roles and tablespaces, plus one
directory-format ``pg_dump`` per database (parallel dump and restore, zstd inside)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .. import proc
from .base import DumpResult, Engine, EngineError, safe_filename

log = logging.getLogger(__name__)

# psql output separators that can't plausibly appear in identifiers.
FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"

GLOBALS_FILE = "globals.sql.zst"
GLOBALS_TRAILER = b"PostgreSQL database cluster dump complete"

RELKINDS = {"r": "table", "p": "partitioned table", "v": "view", "m": "materialized view", "f": "foreign table"}


def conninfo_dbname(db: str) -> str:
    """A ``dbname=...`` conninfo string, so database names are never parsed as connection strings."""
    return "dbname='" + db.replace("\\", "\\\\").replace("'", "\\'") + "'"


_ROLE_STATEMENT = re.compile(r'^(?:CREATE|ALTER|COMMENT ON) ROLE ("(?:[^"]|"")*"|[^\s;]+)')


def without_roles(sql: str, existing: set[str]) -> tuple[str, set[str]]:
    """Drop the statements in a globals dump that create or alter a role in ``existing``.

    Returns the remaining SQL, and the roles that were left alone. Memberships
    (GRANT role TO role) are kept: they only add.
    """
    kept = []
    skipped = set()
    for line in sql.splitlines():
        if m := _ROLE_STATEMENT.match(line):
            name = m.group(1)
            role = name[1:-1].replace('""', '"') if name.startswith('"') else name
            if role in existing:
                skipped.add(role)
                continue
        kept.append(line)
    return "\n".join(kept) + "\n", skipped


class PostgresEngine(Engine):
    type = "postgres"
    min_version = (18,)

    # -- connection helpers ----------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = {"PGAPPNAME": "dbbackup", "PGCONNECT_TIMEOUT": "15"}
        if self.conn.password is not None:
            env["PGPASSWORD"] = self.conn.password
        if self.target.sslmode:
            env["PGSSLMODE"] = self.target.sslmode
        return env

    def _conn_args(self) -> list[str]:
        args = ["-h", self.conn.host, "-p", str(self.conn.port)]
        if self.conn.username:
            args += ["-U", self.conn.username]
        return args

    def query(self, sql: str, db: str | None = None, **variables: str) -> list[list[str]]:
        """Run SQL through psql. ``variables`` are psql variables, interpolated safely as :'name' or :"name"."""
        cmd = [
            "psql",
            "-X",
            "-q",
            "-A",
            "-t",
            "-F",
            FIELD_SEP,
            "-R",
            RECORD_SEP,
            "-v",
            "ON_ERROR_STOP=1",
            *self._conn_args(),
            "-d",
            conninfo_dbname(db or self.target.maintenance_db),
        ]
        for name, value in variables.items():
            cmd += ["-v", f"{name}={value}"]
        out = proc.run(cmd, env=self._env(), input=sql).stdout
        return [record.split(FIELD_SEP) for record in out.rstrip("\n").split(RECORD_SEP) if record]

    # -- discovery -------------------------------------------------------------

    def server_version(self) -> str:
        return self.query("SHOW server_version")[0][0].split()[0]

    def list_databases(self) -> list[str]:
        rows = self.query("SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY datname")
        return [r[0] for r in rows]

    def can_read_passwords(self) -> bool:
        """Superusers, and members of pg_read_all_data, can read pg_authid and so dump role passwords."""
        return self.query("SELECT has_table_privilege('pg_catalog.pg_authid', 'SELECT')")[0][0] == "t"

    def inventory(self, databases: list[str], *, exact: bool = False) -> dict:
        result = {}
        for db in databases:
            rows = self.query(
                r"""SELECT n.nspname, c.relname, c.relkind, c.reltuples::bigint, c.relispopulated,
                           quote_ident(n.nspname) || '.' || quote_ident(c.relname)
                    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
                      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                      AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp%'""",
                db=db,
            )
            objects = {}
            countable = []
            for schema, name, kind, tuples, populated, qualified in rows:
                key = f"{schema}.{name}"
                has_rows = kind in ("r", "p", "m") and populated == "t"
                # reltuples is -1 until the table is first vacuumed or analyzed.
                estimate = int(tuples) if has_rows and int(tuples) >= 0 else None
                objects[key] = {"kind": RELKINDS[kind], "rows": None if exact else estimate}
                if exact and has_rows:
                    countable.append((key, qualified))
            if countable:
                sql = " UNION ALL ".join(f"SELECT {i}, count(*) FROM {qualified}" for i, (_, qualified) in enumerate(countable))
                for i, count in self.query(sql, db=db):
                    objects[countable[int(i)][0]]["rows"] = int(count)
            result[db] = {"objects": objects}
        users = [r[0] for r in self.query("SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY rolname")]
        return {"counts": "exact" if exact else "estimated", "databases": result, "users": users}

    # -- backup ----------------------------------------------------------------

    def backup(self, set_dir: Path, databases: list[str]) -> DumpResult:
        warnings = []
        passwords = self.can_read_passwords()
        globals_cmd = ["pg_dumpall", *self._conn_args(), "-d", conninfo_dbname(self.target.maintenance_db), "--globals-only"]
        if not passwords:
            globals_cmd.append("--no-role-passwords")
            warnings.append("backup role can't read pg_authid, so role passwords were not saved; grant it pg_read_all_data")
        log.info("%s: dumping roles and tablespaces", self.target.name)
        proc.pipe_to_zstd(globals_cmd, set_dir / GLOBALS_FILE, self.target.compression_level, env=self._env())

        files = {}
        for db in databases:
            name = safe_filename(db) + ".pgdump"
            log.info("%s: dumping database %s", self.target.name, db)
            cmd = [
                "pg_dump",
                *self._conn_args(),
                "-d",
                conninfo_dbname(db),
                "--format=directory",
                f"--jobs={self.target.jobs}",
                f"--compress=zstd:{self.target.compression_level}",
                f"--file={set_dir / name}",
                *self.target.dump_args,
            ]
            stderr = proc.run(cmd, env=self._env()).stderr
            warnings += [f"{db}: {line.strip()}" for line in stderr.splitlines() if "warning" in line.lower()]
            files[db] = name

        return DumpResult({"globals": GLOBALS_FILE, "role_passwords": passwords, "databases": files}, warnings)

    def verify(self, set_dir: Path, manifest: dict) -> None:
        contents = manifest["contents"]
        _, tail = proc.zstd_head_tail(set_dir / contents["globals"])
        if GLOBALS_TRAILER not in tail:
            raise EngineError(f"{contents['globals']}: dump is incomplete (no completion trailer)")
        for db, name in contents["databases"].items():
            if not (set_dir / name / "toc.dat").is_file():
                raise EngineError(f"{db}: {name} has no toc.dat")
            try:
                proc.run(["pg_restore", "--list", str(set_dir / name)])
            except proc.CommandError as e:
                raise EngineError(f"{db}: pg_restore cannot read {name}: {e}") from e

    # -- restore ---------------------------------------------------------------

    def restore(self, set_dir: Path, manifest: dict, databases: list[str], *, force: bool, include_globals: bool) -> list[str]:
        contents = manifest["contents"]
        warnings = []
        maintenance_db = self.target.maintenance_db

        existing = set(self.list_databases())
        # Every cluster has the maintenance database, so it only clashes if something is in it.
        clashes = [db for db in databases if db in existing and (db != maintenance_db or self._has_user_objects(db))]
        if clashes and not force:
            raise EngineError(f"database(s) already exist on {self.conn.host}: {', '.join(clashes)} (use --force to replace them)")

        if include_globals:
            # Roles first: restored objects are owned by, and granted to, them.
            log.info("%s: restoring roles and tablespaces", self.target.name)
            warnings += self._restore_globals(set_dir / contents["globals"])
            if not contents.get("role_passwords", True):
                warnings.append("this backup has no role passwords; restored login roles need their passwords set again")

        for db in databases:
            cmd = ["pg_restore", *self._conn_args(), f"--jobs={self.target.jobs}"]
            if db == maintenance_db and db in existing:
                # We can't drop or create the database we connect through, so restore into it in place.
                log.info("%s: restoring database %s in place", self.target.name, db)
                cmd += ["-d", conninfo_dbname(db)] + (["--clean", "--if-exists"] if db in clashes else [])
            else:
                if db in clashes:
                    log.warning("%s: dropping existing database %s (--force)", self.target.name, db)
                    self.query('DROP DATABASE :"target_db" WITH (FORCE)', target_db=db)
                log.info("%s: restoring database %s", self.target.name, db)
                cmd += ["-d", conninfo_dbname(maintenance_db), "--create"]
            cmd.append(str(set_dir / contents["databases"][db]))
            try:
                proc.run(cmd, env=self._env())
            except proc.CommandError as e:
                raise EngineError(f"{db}: pg_restore failed: {e}") from e
            proc.run(["vacuumdb", *self._conn_args(), "--analyze-only", "--quiet", "-d", conninfo_dbname(db)], env=self._env())
        return warnings

    def _has_user_objects(self, db: str) -> bool:
        rows = self.query(
            r"""SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_toast%')""",
            db=db,
        )
        return rows[0][0] == "t"

    def _restore_globals(self, path: Path) -> list[str]:
        # Roles that already exist on the destination (including the one we're
        # restoring as) are left exactly as they are: replaying their ALTER ROLE
        # ... PASSWORD would reset their passwords to the backed-up server's.
        existing = {r[0] for r in self.query("SELECT rolname FROM pg_roles")}
        sql, skipped = without_roles(proc.run(["zstd", "-q", "-dc", str(path)]).stdout, existing)
        if skipped:
            log.info("%s: left existing role(s) as they are: %s", self.target.name, ", ".join(sorted(skipped)))
        cmd = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=0", *self._conn_args(), "-d", conninfo_dbname(self.target.maintenance_db)]
        stderr = proc.run(cmd, env=self._env(), input=sql).stderr
        return [f"globals: {line.strip()}" for line in stderr.splitlines() if "ERROR:" in line]
