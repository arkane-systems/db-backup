"""Engine logic that doesn't need a server: command construction (no secrets on
argv), dump-filtering for restores, and naming."""

import subprocess
from pathlib import Path

import pytest

from dbbackup import proc
from dbbackup.config import parse_config
from dbbackup.engines import engine_for
from dbbackup.engines.base import safe_filename, version_tuple
from dbbackup.engines.mariadb import option_file_value, statement_account, without_accounts
from dbbackup.engines.mongodb import _ns_escape
from dbbackup.engines.postgres import conninfo_dbname, without_roles

SECRET = "pa ss'w\"o#rd\\"


def target(ttype, **extra):
    raw = {"targets": [{"name": "t", "type": ttype, "host": "db.example", "username": "backup", "password_env": "PW", **extra}]}
    return parse_config(raw, {"PW": SECRET}).targets[0]


class Recorder:
    """Stands in for the proc helpers: records every command and its environment."""

    def __init__(self, monkeypatch, stdout=""):
        self.calls = []
        self.files = {}
        self.stdout = stdout

        def run(cmd, *, env=None, input=None, check=True):
            self.calls.append((list(cmd), env or {}))
            self._capture_files(cmd)
            return subprocess.CompletedProcess(cmd, 0, self.stdout, "")

        def pipe_to_zstd(cmd, out_path, level, *, env=None):
            self.calls.append((list(cmd), env or {}))
            self._capture_files(cmd)
            return ""

        monkeypatch.setattr(proc, "run", run)
        monkeypatch.setattr(proc, "pipe_to_zstd", pipe_to_zstd)

    def _capture_files(self, cmd):
        for arg in cmd:
            for prefix in ("--defaults-extra-file=", "--config="):
                if arg.startswith(prefix):
                    self.files[arg] = Path(arg.removeprefix(prefix)).read_text()

    def assert_no_secret_on_argv(self):
        for cmd, _env in self.calls:
            assert not any(SECRET in arg for arg in cmd), cmd


def test_postgres_backup_commands(monkeypatch, tmp_path):
    rec = Recorder(monkeypatch, stdout="t\x1e")
    engine = engine_for(target("postgres", jobs=3, dump_args=["--no-comments"]))
    engine.backup(tmp_path, ["app", "odd'db"])
    rec.assert_no_secret_on_argv()
    assert all(env.get("PGPASSWORD") == SECRET for _, env in rec.calls)

    dumps = [cmd for cmd, _ in rec.calls if cmd[0] == "pg_dump"]
    assert len(dumps) == 2
    assert "--format=directory" in dumps[0] and "--jobs=3" in dumps[0] and "--compress=zstd:3" in dumps[0]
    assert dumps[0][-1] == "--no-comments"
    assert "dbname='odd\\'db'" in dumps[1]
    globals_cmd = next(cmd for cmd, _ in rec.calls if cmd[0] == "pg_dumpall")
    assert "--globals-only" in globals_cmd and "--no-role-passwords" not in globals_cmd


def test_postgres_without_pg_authid_access_skips_passwords(monkeypatch, tmp_path):
    rec = Recorder(monkeypatch, stdout="f\x1e")
    result = engine_for(target("postgres")).backup(tmp_path, [])
    globals_cmd = next(cmd for cmd, _ in rec.calls if cmd[0] == "pg_dumpall")
    assert "--no-role-passwords" in globals_cmd
    assert result.contents["role_passwords"] is False
    assert any("passwords were not saved" in w for w in result.warnings)


def test_mariadb_credentials_go_in_an_option_file(monkeypatch, tmp_path):
    rec = Recorder(monkeypatch)
    engine_for(target("mariadb", ssl=False)).backup(tmp_path, ["shop"])
    rec.assert_no_secret_on_argv()
    for cmd, _ in rec.calls:
        assert cmd[1].startswith("--defaults-extra-file="), "must be the first option"
        assert "--skip-ssl" in cmd
    (option_file,) = set(rec.files.values())
    assert option_file == f"[client]\nuser={option_file_value('backup')}\npassword={option_file_value(SECRET)}\n"
    dump = next(cmd for cmd, _ in rec.calls if "--databases" in cmd)
    assert "--single-transaction" in dump and dump[-2:] == ["--databases", "shop"]
    assert not any(Path(arg.split("=", 1)[1]).exists() for arg in rec.files), "option file must be deleted"


def test_option_file_value_escaping():
    assert option_file_value('a"b\\c') == '"a\\"b\\\\c"'


def test_mongodb_uri_goes_in_a_config_file(monkeypatch, tmp_path):
    rec = Recorder(monkeypatch)
    engine = engine_for(target("mongodb", uri_options={"replicaSet": "rs0"}))
    monkeypatch.setattr(type(engine), "is_replica_set", lambda self: True)
    engine.backup(tmp_path, ["shop"])
    rec.assert_no_secret_on_argv()
    (cmd,) = [c for c, _ in rec.calls]
    assert "--oplog" in cmd and "--archive" in cmd
    (config,) = rec.files.values()
    assert config.startswith('uri: "mongodb://backup:')
    assert "replicaSet=rs0" in config and "authSource=admin" in config


def test_mongodb_uri_quotes_credentials():
    uri = engine_for(target("mongodb")).uri()
    assert uri.startswith("mongodb://backup:pa+ss%27w%22o%23rd%5C@db.example:27017/?")


def test_mongodb_include_exclude_dumps_per_database(monkeypatch, tmp_path):
    rec = Recorder(monkeypatch)
    engine = engine_for(target("mongodb", exclude=["scratch"]))
    monkeypatch.setattr(type(engine), "is_replica_set", lambda self: True)
    result = engine.backup(tmp_path, ["logs", "shop"])
    dbs = [next(a for a in cmd if a.startswith("--db=")) for cmd, _ in rec.calls]
    assert dbs == ["--db=admin", "--db=logs", "--db=shop"]
    assert not any("--oplog" in cmd for cmd, _ in rec.calls)
    assert result.contents["mode"] == "per-database"
    assert result.warnings


@pytest.mark.parametrize(
    "line, account",
    [
        ("CREATE USER IF NOT EXISTS `shop_app`@`%` IDENTIFIED BY PASSWORD '*2E68';", ("shop_app", "%")),
        ("/*M!100005 CREATE ROLE IF NOT EXISTS 'reporting' WITH ADMIN mariadb_dump_import_role */;", ("reporting", None)),
        ("GRANT `reporting` TO `root`@`localhost` WITH ADMIN OPTION;", ("root", "localhost")),
        ("GRANT PROXY ON ``@`%` TO `root`@`%` WITH GRANT OPTION;", ("root", "%")),
        ("GRANT USAGE ON *.* TO `reporting`;", ("reporting", None)),
        ("/*!80001 ALTER USER 'root'@'%' DEFAULT ROLE NONE */;", ("root", "%")),
        ("/*M!100005 SET DEFAULT ROLE NONE FOR 'mariadb.sys'@'localhost' */;", ("mariadb.sys", "localhost")),
        ("CREATE USER IF NOT EXISTS `we``ird`@`h` IDENTIFIED BY PASSWORD 'x';", ("we`ird", "h")),
        ("GRANT mariadb_dump_import_role TO CURRENT_USER();", None),
        ("SET ROLE mariadb_dump_import_role;", None),
    ],
)
def test_mariadb_statement_account(line, account):
    assert statement_account(line) == account


def test_mariadb_without_accounts_leaves_existing_alone():
    sql = "\n".join(
        [
            "CREATE USER IF NOT EXISTS `root`@`%` IDENTIFIED BY PASSWORD '*AAAA';",
            "CREATE USER IF NOT EXISTS `app`@`%` IDENTIFIED BY PASSWORD '*BBBB';",
            "GRANT ALL PRIVILEGES ON *.* TO `root`@`%` IDENTIFIED BY PASSWORD '*AAAA' WITH GRANT OPTION;",
            "GRANT SELECT ON `shop`.* TO `app`@`%`;",
            "SET ROLE NONE;",
        ]
    )
    kept, skipped = without_accounts(sql, {("root", "%")})
    assert skipped == {("root", "%")}
    assert "root" not in kept
    assert kept.count("\n") == 3 and "`app`@`%`" in kept and "SET ROLE NONE;" in kept


def test_postgres_without_roles():
    sql = "\n".join(
        [
            "CREATE ROLE postgres;",
            "ALTER ROLE postgres WITH SUPERUSER LOGIN PASSWORD 'SCRAM';",
            'CREATE ROLE "Odd ""Role""";',
            'ALTER ROLE "Odd ""Role""" WITH LOGIN;',
            "CREATE ROLE app;",
            "ALTER ROLE app WITH LOGIN PASSWORD 'x';",
            "GRANT reporting TO postgres;",
        ]
    )
    kept, skipped = without_roles(sql, {"postgres", 'Odd "Role"'})
    assert skipped == {"postgres", 'Odd "Role"'}
    assert kept == "CREATE ROLE app;\nALTER ROLE app WITH LOGIN PASSWORD 'x';\nGRANT reporting TO postgres;\n"


@pytest.mark.parametrize(
    "name, expected",
    [("app1", "app1"), ("odd.name/db", "odd%2Ename%2Fdb"), ("..", "%2E%2E"), ("wiki-db", "wiki-db"), ("ünï", "%C3%BCn%C3%AF")],
)
def test_safe_filename(name, expected):
    assert safe_filename(name) == expected


def test_conninfo_dbname():
    assert conninfo_dbname("a b") == "dbname='a b'"
    assert conninfo_dbname("x'\\y") == "dbname='x\\'\\\\y'"


def test_version_tuple():
    assert version_tuple("11.4.13-MariaDB-ubu2404") == (11, 4, 13)
    assert version_tuple("18.6") == (18, 6)
    assert version_tuple("8.3.11") >= (8, 3)
    assert version_tuple("garbage") == ()


def test_version_warning():
    engine = engine_for(target("mariadb"))
    assert engine.version_warning("11.4.0-MariaDB") is None
    assert "older than the minimum" in engine.version_warning("10.11.9-MariaDB")


def test_select_databases(monkeypatch):
    engine = engine_for(target("postgres", exclude=["scratch"]))
    monkeypatch.setattr(type(engine), "list_databases", lambda self: ["app", "postgres", "scratch"])
    assert engine.select_databases() == ["app", "postgres"]
    engine = engine_for(target("postgres", include=["app", "scratch"], exclude=["scratch"]))
    assert engine.select_databases() == ["app"]
    engine = engine_for(target("postgres", include=["missing"]))
    with pytest.raises(Exception, match="not found on server: missing"):
        engine.select_databases()


def test_ns_escape():
    assert _ns_escape("a*b$c") == "a\\*b\\$c"
