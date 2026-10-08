import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from dbbackup.config import Retention
from dbbackup.storage import LockedError, StorageError, Store, format_ts, verify_checksums

T0 = datetime(2026, 10, 1, 2, 0, tzinfo=UTC)


def make_set(store: Store, target: str, when: datetime, files=None, manifest_extra=None):
    partial = store.begin(target, when)
    for rel, data in (files or {"dump.sql.zst": b"data"}).items():
        (partial / rel).parent.mkdir(parents=True, exist_ok=True)
        (partial / rel).write_bytes(data)
    return store.finalize(partial, {"status": "ok", "finished": when.isoformat(), **(manifest_extra or {})})


def test_finalize_writes_manifest_and_renames(tmp_path):
    store = Store(tmp_path)
    bset = make_set(store, "pg", T0, {"a.sql.zst": b"aaa", "db.pgdump/toc.dat": b"toc"})
    assert bset.path == tmp_path / "pg" / "20261001T020000Z"
    assert not (tmp_path / "pg" / "20261001T020000Z.partial").exists()
    manifest = json.loads((bset.path / "manifest.json").read_text())
    assert set(manifest["files"]) == {"a.sql.zst", "db.pgdump/toc.dat"}
    assert manifest["size_bytes"] == 6
    assert store.sets("pg")[0].ok
    assert verify_checksums(store.sets("pg")[0]) == []


def test_verify_checksums_detects_tampering(tmp_path):
    store = Store(tmp_path)
    bset = make_set(store, "pg", T0, {"a": b"one", "b": b"two"})
    (bset.path / "a").write_bytes(b"ONE")
    (bset.path / "b").unlink()
    (bset.path / "c").write_bytes(b"new")
    assert verify_checksums(store.sets("pg")[0]) == ["missing file: b", "unexpected file: c", "checksum mismatch: a"]


def test_partial_and_broken_sets_are_not_ok(tmp_path):
    store = Store(tmp_path)
    store.begin("pg", T0)  # never finalized
    broken = tmp_path / "pg" / format_ts(T0 + timedelta(days=1))
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json")
    sets = store.sets("pg")
    assert [s.ok for s in sets] == [False]  # the .partial isn't listed at all
    assert store.latest_ok("pg") is None
    with pytest.raises(StorageError, match="no successful backup sets"):
        store.find("pg")


def test_find_latest_and_by_name(tmp_path):
    store = Store(tmp_path)
    make_set(store, "pg", T0)
    newest = make_set(store, "pg", T0 + timedelta(days=1))
    assert store.find("pg").path == newest.path
    assert store.find("pg", "20261001T020000Z").timestamp == T0
    with pytest.raises(StorageError, match="not found"):
        store.find("pg", "20200101T000000Z")


def test_lock_is_exclusive_and_released(tmp_path):
    store = Store(tmp_path)
    with store.lock("pg", timedelta(hours=1)):
        with pytest.raises(LockedError, match="locked by another run"):
            with store.lock("pg", timedelta(hours=1)):
                pass
    assert not (tmp_path / "pg" / ".lock").exists()
    with store.lock("pg", timedelta(hours=1)):
        pass


def test_stale_lock_is_broken(tmp_path):
    store = Store(tmp_path)
    lock = tmp_path / "pg" / ".lock"
    lock.parent.mkdir(parents=True)
    lock.write_text('{"host": "gone"}')
    old = time.time() - 3 * 3600
    os.utime(lock, (old, old))
    with store.lock("pg", timedelta(hours=2)):
        assert "gone" not in lock.read_text()


def test_prune_applies_retention(tmp_path):
    store = Store(tmp_path)
    for day in range(10):
        make_set(store, "pg", T0 + timedelta(days=day))
    deleted = store.prune("pg", Retention(daily=3, weekly=0, monthly=0), stale_partial_after=timedelta(hours=24))
    assert len(deleted) == 7
    assert [s.timestamp.day for s in store.sets("pg")] == [8, 9, 10]


def test_prune_dry_run_deletes_nothing(tmp_path):
    store = Store(tmp_path)
    for day in range(5):
        make_set(store, "pg", T0 + timedelta(days=day))
    assert len(store.prune("pg", Retention(daily=1, weekly=0, monthly=0), stale_partial_after=timedelta(hours=1), dry_run=True)) == 4
    assert len(store.sets("pg")) == 5


def test_prune_leaves_unrecognised_sets_and_removes_stale_partials(tmp_path):
    store = Store(tmp_path)
    make_set(store, "pg", T0)
    bad = tmp_path / "pg" / format_ts(T0 - timedelta(days=30))
    bad.mkdir()
    old_partial = store.begin("pg", T0 - timedelta(days=2))
    fresh_partial = store.begin("pg", T0 + timedelta(hours=1))
    store.prune("pg", Retention(daily=1, weekly=0, monthly=0), stale_partial_after=timedelta(hours=24), now=T0 + timedelta(hours=2))
    assert bad.exists()
    assert not old_partial.exists()
    assert fresh_partial.exists()


def test_targets_are_independent(tmp_path):
    store = Store(tmp_path)
    make_set(store, "a", T0)
    make_set(store, "b", T0)
    store.prune("a", Retention(daily=0, weekly=0, monthly=0), stale_partial_after=timedelta(hours=1))
    assert len(store.sets("a")) == 1  # newest always kept
    assert len(store.sets("b")) == 1
