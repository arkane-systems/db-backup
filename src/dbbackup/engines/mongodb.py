"""MongoDB: by default one whole-instance ``mongodump --oplog --archive``, which
includes users and roles (in ``admin``) and is consistent to a single point in time.

``mongodump`` can't exclude databases from a whole-instance dump, so a target with
``include`` or ``exclude`` gets one archive per database (plus ``admin``, for users
and roles) instead. Those can't use ``--oplog``, so each is consistent only per
collection; the manifest records a warning saying so.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import quote_plus, urlencode

from .. import proc
from .base import DumpResult, Engine, EngineError, safe_filename

log = logging.getLogger(__name__)

SYSTEM_DATABASES = frozenset({"admin", "config", "local"})
INSTANCE_FILE = "mongodb.archive.zst"
USERS_FILE = "admin.archive.zst"
ARCHIVE_MAGIC = bytes.fromhex("6de29981")  # 0x8199e26d, little-endian
AUTH_COLLECTIONS = ("admin.system.users", "admin.system.roles")
FAILED_RE = re.compile(r"(\d+) document\(s\) failed to restore")


class MongoDBEngine(Engine):
    type = "mongodb"
    min_version = (8, 3)

    # -- connection helpers ----------------------------------------------------

    def uri(self) -> str:
        if self.conn.uri:
            return self.conn.uri
        userinfo = ""
        if self.conn.username:
            userinfo = quote_plus(self.conn.username)
            if self.conn.password is not None:
                userinfo += ":" + quote_plus(self.conn.password)
            userinfo += "@"
        params = {"authSource": "admin", **self.target.uri_options}
        return f"mongodb://{userinfo}{self.conn.host}:{self.conn.port}/?{urlencode(params)}"

    def _config_file(self):
        """A 0600 mongodump/mongorestore --config file holding the URI (it contains the password)."""
        return proc.secret_file(f"uri: {json.dumps(self.uri())}\n", suffix=".yaml")

    def _client(self):
        from pymongo import MongoClient

        return MongoClient(self.uri(), serverSelectionTimeoutMS=15000, appname="dbbackup")

    # -- discovery -------------------------------------------------------------

    def server_version(self) -> str:
        with self._client() as client:
            return client.server_info()["version"]

    def is_replica_set(self) -> bool:
        with self._client() as client:
            return bool(client.admin.command("hello").get("setName"))

    def list_databases(self) -> list[str]:
        with self._client() as client:
            return sorted(d for d in client.list_database_names() if d not in SYSTEM_DATABASES)

    def inventory(self, databases: list[str], *, exact: bool = False) -> dict:
        from pymongo.errors import OperationFailure

        result = {}
        with self._client() as client:
            for db in databases:
                objects = {}
                for coll in client[db].list_collections():
                    name, ctype = coll["name"], coll.get("type", "collection")
                    if name.startswith("system."):
                        continue
                    rows = None
                    if ctype == "collection":
                        # The estimate is collection metadata, normally accurate; exact means a scan.
                        collection = client[db][name]
                        rows = collection.count_documents({}) if exact else collection.estimated_document_count()
                    objects[name] = {"kind": ctype, "rows": rows}
                result[db] = {"objects": objects}
            try:
                users = sorted(f"{u['user']}@{u['db']}" for u in client.admin["system.users"].find({}, {"user": 1, "db": 1}))
                users += sorted(f"role:{r['role']}@{r['db']}" for r in client.admin["system.roles"].find({}, {"role": 1, "db": 1}))
            except OperationFailure as e:
                log.warning("%s: can't list users (%s); restore checks will skip them", self.target.name, e)
                users = None
        return {"counts": "exact" if exact else "estimated", "databases": result, "users": users}

    # -- backup ----------------------------------------------------------------

    def backup(self, set_dir: Path, databases: list[str]) -> DumpResult:
        warnings = []
        replica_set = self.is_replica_set()
        level = self.target.compression_level

        with self._config_file() as cfg:
            base = ["mongodump", f"--config={cfg}", "--archive", *self.target.dump_args]

            if not (self.target.include or self.target.exclude):
                if not replica_set:
                    warnings.append("server is not a replica set member, so the dump can't use --oplog and isn't point-in-time consistent")
                log.info("%s: dumping whole instance%s", self.target.name, " with oplog" if replica_set else "")
                proc.pipe_to_zstd(base + (["--oplog"] if replica_set else []), set_dir / INSTANCE_FILE, level)
                return DumpResult({"mode": "instance", "oplog": replica_set, "archive": INSTANCE_FILE}, warnings)

            warnings.append(
                "include/exclude is set, so databases are dumped one by one without --oplog: "
                "each is consistent per collection only, not across the instance"
            )
            log.info("%s: dumping users and roles", self.target.name)
            proc.pipe_to_zstd(base + ["--db=admin"], set_dir / USERS_FILE, level)
            files = {}
            for db in databases:
                name = safe_filename(db) + ".archive.zst"
                log.info("%s: dumping database %s", self.target.name, db)
                proc.pipe_to_zstd(base + [f"--db={db}"], set_dir / name, level)
                files[db] = name
            return DumpResult({"mode": "per-database", "oplog": False, "users": USERS_FILE, "databases": files}, warnings)

    def verify(self, set_dir: Path, manifest: dict) -> None:
        contents = manifest["contents"]
        if contents["mode"] == "instance":
            archives = [contents["archive"]]
        else:
            archives = [contents["users"], *contents["databases"].values()]
        for name in archives:
            try:
                head, _ = proc.zstd_head_tail(set_dir / name)
            except proc.CommandError as e:
                raise EngineError(f"{name} is corrupt: {e}") from e
            if head[:4] != ARCHIVE_MAGIC:
                raise EngineError(f"{name} is not a mongodump archive")

    # -- restore ---------------------------------------------------------------

    def restore(self, set_dir: Path, manifest: dict, databases: list[str], *, force: bool, include_globals: bool) -> list[str]:
        contents = manifest["contents"]
        warnings = []

        existing = set(self.list_databases())
        clashes = [db for db in databases if db in existing]
        if clashes and not force:
            where = self.conn.host or "the server"
            raise EngineError(f"database(s) already exist on {where}: {', '.join(clashes)} (use --force to replace them)")
        if clashes:
            with self._client() as client:
                for db in clashes:
                    log.warning("%s: dropping existing database %s (--force)", self.target.name, db)
                    client.drop_database(db)

        with self._config_file() as cfg:
            # Never --drop: it would also replace existing users with the backed-up
            # server's (resetting e.g. the password of the account we restore as).
            # Without it, accounts that already exist are left alone; --force is
            # handled above, by dropping the clashing databases first.
            base = ["mongorestore", f"--config={cfg}", "--archive"]

            if contents["mode"] == "instance":
                everything = set(databases) == set(manifest["databases"])
                cmd = list(base)
                if not everything:
                    cmd += [f"--nsInclude={_ns_escape(db)}.*" for db in databases]
                    if contents["oplog"]:
                        warnings.append("restoring only some databases, so the oplog was not replayed")
                    if include_globals:
                        warnings.append("restoring only some databases, so users and roles were not restored")
                elif include_globals:
                    if contents["oplog"]:
                        cmd.append("--oplogReplay")
                else:
                    cmd += [f"--nsExclude={ns}" for ns in AUTH_COLLECTIONS]
                    if contents["oplog"]:
                        warnings.append("mongorestore can't replay the oplog while excluding users and roles, so it was not replayed")
                log.info("%s: restoring %s", self.target.name, "whole instance" if everything else ", ".join(databases))
                warnings += self._mongorestore(cmd, set_dir / contents["archive"])
            else:
                for db in databases:
                    log.info("%s: restoring database %s", self.target.name, db)
                    warnings += self._mongorestore(base, set_dir / contents["databases"][db])
                if include_globals:
                    log.info("%s: restoring users and roles", self.target.name)
                    warnings += self._mongorestore(base, set_dir / contents["users"])
        return warnings

    def _mongorestore(self, cmd: list[str], archive: Path) -> list[str]:
        try:
            stderr = proc.zstd_pipe_from(archive, cmd)
        except proc.CommandError as e:
            raise EngineError(f"mongorestore failed: {e}") from e
        failed = sum(int(n) for n in FAILED_RE.findall(stderr))
        return [f"{archive.name}: {failed} document(s) failed to restore"] if failed else []


def _ns_escape(db: str) -> str:
    """Escape a database name for a mongorestore namespace pattern."""
    return re.sub(r"([\\*$])", r"\\\1", db)
