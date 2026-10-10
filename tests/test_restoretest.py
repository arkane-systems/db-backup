import shlex
from datetime import UTC, datetime

import pytest

from dbbackup.cli import main
from dbbackup.restoretest import compare_inventory, counts_match, target_for_set
from dbbackup.storage import StorageError, Store, open_set

EXPECTED = {
    "databases": {
        "app": {
            "objects": {
                "public.customers": {"kind": "table", "rows": 5000},
                "public.big_customers": {"kind": "view", "rows": None},
                "public.unanalyzed": {"kind": "table", "rows": None},
            }
        },
        "logs": {"objects": {"events": {"kind": "collection", "rows": 2000}}},
    },
    "users": ["app_owner", "postgres"],
}


def restored(**overrides):
    actual = {
        "databases": {
            "app": {
                "objects": {
                    "public.customers": {"kind": "table", "rows": 5000},
                    "public.big_customers": {"kind": "view", "rows": None},
                    "public.unanalyzed": {"kind": "table", "rows": 12},
                    "public.extra": {"kind": "table", "rows": 1},
                }
            },
            "logs": {"objects": {"events": {"kind": "collection", "rows": 2000}}},
        },
        "users": ["app_owner", "postgres", "scratch_admin"],
    }
    actual.update(overrides)
    return actual


def test_a_faithful_restore_passes():
    result = compare_inventory(EXPECTED, restored(), {"app", "logs"})
    assert result.errors == [] and result.warnings == []
    assert (result.objects_checked, result.counts_checked, result.users_checked) == (4, 2, 2)


def test_missing_database_object_and_user_are_errors():
    actual = restored(users=["postgres"])
    del actual["databases"]["app"]["objects"]["public.big_customers"]
    result = compare_inventory(EXPECTED, actual, {"app"})
    assert result.errors == [
        "app: view public.big_customers is missing",
        "database logs is missing",
        "user/role app_owner is missing",
    ]


def test_kind_change_is_an_error():
    actual = restored()
    actual["databases"]["app"]["objects"]["public.big_customers"] = {"kind": "table", "rows": 0}
    assert compare_inventory(EXPECTED, actual, {"app", "logs"}).errors == [
        "app: public.big_customers was a view but was restored as a table"
    ]


def test_count_mismatch_beyond_tolerance_is_only_a_warning():
    actual = restored()
    actual["databases"]["app"]["objects"]["public.customers"]["rows"] = 2000
    result = compare_inventory(EXPECTED, actual, {"app", "logs"})
    assert result.errors == []
    assert result.warnings == ["app: public.customers has 2000 rows, but the backup estimated about 5000"]


def test_exact_counts_are_compared_exactly():
    expected = {**EXPECTED, "counts": "exact"}
    actual = restored()
    actual["databases"]["app"]["objects"]["public.customers"]["rows"] = 4999
    result = compare_inventory(expected, actual, {"app", "logs"})
    assert result.warnings == ["app: public.customers has 4999 rows, but 5000 were counted at backup time"]


def test_old_mariadb_manifests_skip_counts():
    actual = restored()
    actual["databases"]["app"]["objects"]["public.customers"]["rows"] = 41
    result = compare_inventory(EXPECTED, actual, {"app", "logs"}, server_type="mariadb")
    assert result.warnings == [] and result.counts_checked == 0
    assert result.notes and "InnoDB" in result.notes[0]
    # ...but a new-style estimated MariaDB manifest is compared (its InnoDB rows are recorded as None).
    result = compare_inventory({**EXPECTED, "counts": "estimated"}, actual, {"app", "logs"}, server_type="mariadb")
    assert result.counts_checked == 2 and result.warnings


@pytest.mark.parametrize(
    "expected, actual, ok",
    [(5000, 4000, True), (5000, 3000, False), (0, 99, True), (0, 101, False), (1_000_000, 800_001, True)],
)
def test_counts_match(expected, actual, ok):
    assert counts_match(expected, actual, 0.25) is ok


def test_users_not_listable_on_either_side():
    assert compare_inventory({"databases": {}, "users": None}, {"databases": {}, "users": ["x"]}, set()).users_checked == 0
    result = compare_inventory({"databases": {}, "users": ["a"]}, {"databases": {}, "users": None}, set())
    assert result.errors == [] and result.warnings


def make_set(tmp_path, target="pg-main", ttype="postgres"):
    store = Store(tmp_path)
    when = datetime(2026, 10, 9, 2, 15, tzinfo=UTC)
    partial = store.begin(target, when)
    (partial / "globals.sql.zst").write_bytes(b"x")
    manifest = {"status": "ok", "target": target, "type": ttype, "server_version": "18.6", "databases": ["app", "it's"], "size_bytes": 1}
    return store.finalize(partial, manifest)


def test_open_set_by_set_or_target_directory(tmp_path):
    bset = make_set(tmp_path)
    assert open_set(bset.path).path == bset.path
    assert open_set(tmp_path / "pg-main").path == bset.path
    assert open_set(tmp_path / "pg-main", bset.name).path == bset.path
    with pytest.raises(StorageError):
        open_set(tmp_path / "nope")
    with pytest.raises(StorageError, match="not found"):
        open_set(tmp_path / "pg-main", "20200101T000000Z")


def test_target_for_set_uses_type_defaults(tmp_path):
    target = target_for_set(make_set(tmp_path, "maria", "mariadb"))
    assert (target.name, target.type, target.port, target.jobs) == ("maria", "mariadb", 3306, 2)


def test_inspect_env_output_is_shell_safe(tmp_path, capsys):
    bset = make_set(tmp_path)
    assert main(["inspect", str(tmp_path / "pg-main"), "--format", "env"]) == 0
    values = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert {k: shlex.split(v)[0] for k, v in values.items()} == {
        "DBB_TARGET": "pg-main",
        "DBB_TYPE": "postgres",
        "DBB_SET": bset.name,
        "DBB_SERVER_VERSION": "18.6",
        "DBB_DATABASES": "2",
        "DBB_SIZE_BYTES": "1",
    }


def test_restore_test_needs_a_scratch_server(tmp_path):
    make_set(tmp_path)
    assert main(["restore-test", str(tmp_path / "pg-main")]) == 2
