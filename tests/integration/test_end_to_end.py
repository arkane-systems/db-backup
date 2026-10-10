"""End-to-end tests against the compose.yaml stack. Run inside the test image:

    docker compose --profile tool run --rm --build tests

The tests run in file order and build on each other: one full backup run, then
checks of what it wrote, then restores into the empty *-restore servers.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from pymongo import MongoClient

pytestmark = pytest.mark.integration

CONFIG = os.environ.get("DBBACKUP_CONFIG", "/etc/dbbackup/config.yaml")
BACKUPS = Path("/backups")
GOOD_TARGETS = ["pg", "maria", "mongo", "mongo-filtered"]

SOURCE_PG = ("postgres", os.environ.get("SOURCE_ROOT_PASSWORD_PG", ""))
RESTORE_PG = ("postgres-restore", os.environ.get("RESTORE_PASSWORD", ""))
SOURCE_MARIA = ("mariadb", os.environ.get("SOURCE_ROOT_PASSWORD_MARIADB", ""))
RESTORE_MARIA = ("mariadb-restore", os.environ.get("RESTORE_PASSWORD", ""))
_SOURCE_MONGO_PASSWORD = os.environ.get("SOURCE_ROOT_PASSWORD_MONGO", "")
SOURCE_MONGO_URI = f"mongodb://root:{_SOURCE_MONGO_PASSWORD}@mongodb:27017/?authSource=admin&directConnection=true"
RESTORE_MONGO_URI = os.environ.get("RESTORE_MONGO_URI", "")


# -- helpers ------------------------------------------------------------------


def dbbackup(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["dbbackup", "--config", CONFIG, *args], capture_output=True, text=True)
    print(f"$ dbbackup {' '.join(args)}  -> {result.returncode}\n{result.stderr}")
    return result


def pg(server: tuple[str, str], sql: str, db: str = "postgres", user: str = "postgres", password: str | None = None) -> list[str]:
    host, root_password = server
    env = {**os.environ, "PGPASSWORD": password or root_password}
    cmd = ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-h", host, "-U", user, "-d", f"dbname='{db}'", "-c", sql]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=True).stdout.splitlines()


def maria(server: tuple[str, str], sql: str, user: str = "root", password: str | None = None) -> list[list[str]]:
    host, root_password = server
    env = {**os.environ, "MYSQL_PWD": password or root_password}
    cmd = ["mariadb", "-h", host, "-u", user, "--batch", "--skip-column-names", "-e", sql]
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True).stdout
    return [line.split("\t") for line in out.splitlines()]


def sets(target: str) -> list[Path]:
    tdir = BACKUPS / target
    return sorted(p for p in tdir.iterdir() if p.is_dir() and not p.name.endswith(".partial")) if tdir.exists() else []


def manifest(target: str) -> dict:
    return json.loads((sets(target)[-1] / "manifest.json").read_text())


def retained_messages(topic_filter: str, wait: float = 2.0) -> dict[str, dict]:
    import paho.mqtt.client as mqtt

    messages = {}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = lambda _c, _u, msg: messages.__setitem__(msg.topic, json.loads(msg.payload))
    client.connect("mosquitto")
    client.subscribe(topic_filter)
    client.loop_start()
    time.sleep(wait)
    client.loop_stop()
    client.disconnect()
    return messages


def reset_restore_servers() -> None:
    """Empty the restore servers, so the suite can be re-run against a long-lived stack."""
    for db in pg(RESTORE_PG, "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'"):
        pg(RESTORE_PG, f'DROP DATABASE "{db}" WITH (FORCE)')
    pg(RESTORE_PG, "DROP ROLE IF EXISTS app_owner, app_reader, reporting, dbbackup")
    maria(RESTORE_MARIA, "DROP DATABASE IF EXISTS shop; DROP DATABASE IF EXISTS `wiki-db`;")
    maria(RESTORE_MARIA, "DROP USER IF EXISTS 'shop_app'@'%', 'wiki_ro'@'%', 'dbbackup'@'%'; DROP ROLE IF EXISTS reporting;")
    with MongoClient(RESTORE_MONGO_URI) as client:
        for db in client.list_database_names():
            if db not in ("admin", "config", "local"):
                client.drop_database(db)
        for user in client.admin["system.users"].find({"user": {"$ne": "root"}}):
            client[user["db"]].command("dropUser", user["user"])


def table_counts_pg(server, db: str) -> dict[str, int]:
    tables = pg(server, "SELECT quote_ident(schemaname) || '.' || quote_ident(relname) FROM pg_stat_user_tables", db=db)
    return {t: int(pg(server, f"SELECT count(*) FROM {t}", db=db)[0]) for t in tables}


def table_counts_maria(server, db: str) -> dict[str, int]:
    tables = [
        r[0]
        for r in maria(server, f"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA='{db}' AND TABLE_TYPE='BASE TABLE'")
    ]
    return {t: int(maria(server, f"SELECT COUNT(*) FROM `{db}`.`{t}`")[0][0]) for t in tables}


def collection_counts(uri: str, dbs: list[str]) -> dict[str, int]:
    with MongoClient(uri) as client:
        return {
            f"{db}.{c['name']}": client[db][c["name"]].count_documents({})
            for db in dbs
            for c in client[db].list_collections()
            if c.get("type", "collection") == "collection" and not c["name"].startswith("system.")
        }


# -- the backup run -----------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def backup_run():
    reset_restore_servers()
    return dbbackup("backup")


def test_failed_target_fails_the_run_but_not_the_others(backup_run):
    assert backup_run.returncode == 1
    assert "backup failed for 1 of 5 target(s): broken" in backup_run.stderr
    assert sets("broken") == []
    for target in GOOD_TARGETS:
        assert len(sets(target)) == 1, target
        assert manifest(target)["status"] == "ok"


def test_no_partial_sets_left_behind():
    assert not list(BACKUPS.glob("*/*.partial"))
    assert not list(BACKUPS.glob("*/.lock"))


def test_postgres_manifest():
    m = manifest("pg")
    assert m["databases"] == ["app1", "odd.name/db", "postgres"]  # scratch excluded
    assert m["contents"]["role_passwords"] is True  # pg_read_all_data can read pg_authid
    assert m["inventory"]["counts"] == "estimated"
    assert m["server_version"].startswith("18.")
    objects = m["inventory"]["databases"]["app1"]["objects"]
    assert objects["billing.invoices"] == {"kind": "table", "rows": 20000}
    assert objects["billing.big_invoices"]["kind"] == "view"
    assert {"app_owner", "app_reader", "reporting"} <= set(m["inventory"]["users"])
    assert m["warnings"] == []


def test_mariadb_manifest():
    m = manifest("maria")
    assert m["databases"] == ["shop", "wiki-db"]
    assert any("shop.legacy_log uses Aria" in w for w in m["warnings"])
    # exact_counts: real COUNT(*)s, where InnoDB's TABLE_ROWS estimate can be wildly off.
    assert m["inventory"]["counts"] == "exact"
    assert m["inventory"]["databases"]["shop"]["objects"]["products"] == {"kind": "table", "rows": 3000}
    assert m["inventory"]["databases"]["wiki-db"]["objects"]["pages"]["rows"] == 500
    assert {"shop_app@%", "wiki_ro@%", "reporting"} <= set(m["inventory"]["users"])


def test_mongodb_manifests():
    m = manifest("mongo")
    assert m["contents"] == {"mode": "instance", "oplog": True, "archive": "mongodb.archive.zst"}
    assert m["databases"] == ["logs", "scratch", "shop"]
    assert m["inventory"]["databases"]["shop"]["objects"]["big_orders"]["kind"] == "view"
    assert "shop_app@shop" in m["inventory"]["users"]
    f = manifest("mongo-filtered")
    assert f["contents"]["mode"] == "per-database"
    assert f["databases"] == ["logs", "shop"]
    assert any("without --oplog" in w for w in f["warnings"])


def test_verify_command():
    assert dbbackup("verify", *GOOD_TARGETS).returncode == 0


def test_list_command():
    result = dbbackup("list")
    assert result.returncode == 0
    for target in GOOD_TARGETS:
        assert target in result.stdout


def test_mqtt_status_and_discovery():
    statuses = retained_messages("dbbackup-test/#")
    pg_status = statuses["dbbackup-test/pg/status"]
    assert pg_status["state"] == "ok"
    assert pg_status["last_success"] == manifest("pg")["finished"]
    broken = statuses["dbbackup-test/broken/status"]
    assert broken["state"] == "failed"
    assert "no-such-host.invalid" in broken["error"]
    assert broken["last_success"] is None
    assert statuses["dbbackup-test/status"]["state"] == "failed"

    discovery = retained_messages("homeassistant/sensor/+/config")
    config = discovery["homeassistant/sensor/dbbackup_mongo_filtered_status/config"]
    assert config["default_entity_id"] == "sensor.dbbackup_mongo_filtered_status"
    assert config["state_topic"] == "dbbackup-test/mongo-filtered/status"


# -- restores -----------------------------------------------------------------


def test_restore_postgres():
    result = dbbackup("restore", "pg", "--host", "postgres-restore", "--username", "postgres", "--password-env", "RESTORE_PASSWORD")
    assert result.returncode == 0
    assert "left existing role(s) as they are: postgres" in result.stderr
    for db in ("app1", "odd.name/db"):
        assert table_counts_pg(RESTORE_PG, db) == table_counts_pg(SOURCE_PG, db)
    # Role passwords, memberships, functions, materialized views and database settings came back.
    assert pg(RESTORE_PG, "SELECT billing.invoice_count(7)", db="app1", user="app_owner", password="app-owner-pw") == ["4"]
    assert pg(
        RESTORE_PG, "SET ROLE reporting; SELECT count(*) FROM billing.totals", db="app1", user="app_reader", password="reader-pw"
    ) == ["5000"]
    assert pg(RESTORE_PG, "SHOW work_mem", db="app1") == ["8MB"]
    # ...and the restoring account's own password was left alone (pg() connects with it).
    assert pg(RESTORE_PG, "SELECT 1") == ["1"]


def test_restore_refuses_to_overwrite_without_force():
    result = dbbackup("restore", "pg", "--host", "postgres-restore", "--username", "postgres", "--password-env", "RESTORE_PASSWORD")
    assert result.returncode == 1
    assert "already exist on postgres-restore: app1, odd.name/db" in result.stderr


def test_restore_force_replaces_one_database():
    pg(RESTORE_PG, "DELETE FROM things", db="odd.name/db")
    result = dbbackup(
        "restore",
        "pg",
        "-d",
        "odd.name/db",
        "--force",
        "--host",
        "postgres-restore",
        "--username",
        "postgres",
        "--password-env",
        "RESTORE_PASSWORD",
    )
    assert result.returncode == 0
    assert pg(RESTORE_PG, "SELECT count(*) FROM things", db="odd.name/db") == ["1000"]


def test_restore_mariadb():
    result = dbbackup("restore", "maria", "--host", "mariadb-restore", "--username", "root", "--password-env", "RESTORE_PASSWORD")
    assert result.returncode == 0
    for db in ("shop", "wiki-db"):
        assert table_counts_maria(RESTORE_MARIA, db) == table_counts_maria(SOURCE_MARIA, db)
    assert maria(RESTORE_MARIA, "SELECT COUNT(*) FROM shop.products WHERE img IS NOT NULL") == [["3000"]]
    assert maria(RESTORE_MARIA, "SELECT ROUTINE_NAME FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='shop'") == [["order_count"]]
    assert maria(RESTORE_MARIA, "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA='shop'") == [["orders_qty"]]
    assert maria(RESTORE_MARIA, "SELECT EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_SCHEMA='shop'") == [["nightly_noop"]]
    # Users come back with their passwords, roles and (table-level) grants...
    assert maria(RESTORE_MARIA, "SET ROLE reporting; SELECT COUNT(*) FROM shop.cheap_products", user="shop_app", password="shop-pw") == [
        ["10"]
    ]
    assert maria(RESTORE_MARIA, "SELECT COUNT(*) FROM `wiki-db`.pages", user="wiki_ro", password="wiki-pw") == [["500"]]
    # ...while the server's own existing accounts keep theirs (maria() connects as root).
    assert maria(RESTORE_MARIA, "SELECT 1") == [["1"]]
    assert "root@localhost" in result.stderr and "healthcheck@localhost" in result.stderr


def test_restore_mongodb_whole_instance():
    result = dbbackup("restore", "mongo", "--uri-env", "RESTORE_MONGO_URI")
    assert result.returncode == 0
    dbs = ["logs", "scratch", "shop"]
    assert collection_counts(RESTORE_MONGO_URI, dbs) == collection_counts(SOURCE_MONGO_URI, dbs)
    with MongoClient(RESTORE_MONGO_URI) as client:
        assert [c["name"] for c in client.shop.list_collections(filter={"type": "view"})] == ["big_orders"]
        assert "item_1" in client.shop.orders.index_information()
    with MongoClient("mongodb://shop_app:shop-pw@mongodb-restore:27017/shop?authSource=shop&directConnection=true") as client:
        assert client.shop.orders.estimated_document_count() == 5000
    # The restore server's root password is untouched (RESTORE_MONGO_URI still works).
    with MongoClient(RESTORE_MONGO_URI) as client:
        client.admin.command("ping")


def test_restore_mongodb_per_database_with_force():
    with MongoClient(RESTORE_MONGO_URI) as client:
        client.logs.events.delete_many({})
    result = dbbackup("restore", "mongo-filtered", "-d", "logs", "--force", "--uri-env", "RESTORE_MONGO_URI")
    assert result.returncode == 0
    with MongoClient(RESTORE_MONGO_URI) as client:
        assert client.logs.events.count_documents({}) == 2000


# -- retention, locking, corruption ---------------------------------------------


def test_second_backup_same_day_replaces_the_first():
    first = sets("pg")
    time.sleep(1)  # set names have one-second resolution
    assert dbbackup("backup", "pg").returncode == 0
    after = sets("pg")
    # GFS keeps the newest set per day, so the earlier one from today is pruned.
    assert len(after) == 1 and after != first


def test_locked_target_is_not_backed_up():
    lock = BACKUPS / "pg" / ".lock"
    lock.write_text('{"host": "elsewhere", "pid": 1}')
    try:
        result = dbbackup("backup", "pg")
        assert result.returncode == 1
        assert "locked by another run" in result.stderr
        assert lock.exists()
    finally:
        lock.unlink()


def test_verify_detects_corruption():
    victim = next((sets("maria")[-1]).glob("*.sql.zst"))
    data = bytearray(victim.read_bytes())
    data[len(data) // 2] ^= 0xFF
    victim.write_bytes(bytes(data))
    result = dbbackup("verify", "maria")
    assert result.returncode == 1
    assert f"checksum mismatch: {victim.name}" in result.stderr
