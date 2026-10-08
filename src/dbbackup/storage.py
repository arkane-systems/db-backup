"""On-disk layout of backup sets.

    <root>/<target>/<YYYYMMDDTHHMMSSZ>/manifest.json   a finished, verified set
    <root>/<target>/<YYYYMMDDTHHMMSSZ>.partial/        a set being written
    <root>/<target>/.lock                              held while a run is in progress

A set is written into its ``.partial`` directory, verified, given its manifest
(which includes a SHA-256 for every file), fsynced, and only then renamed into
place. Finished sets are never modified or renamed again, only deleted by
retention, so the off-site sync of the share only ever sees new files.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Retention
from .retention import select_keep

log = logging.getLogger(__name__)

TS_FORMAT = "%Y%m%dT%H%M%SZ"
TS_RE = re.compile(r"^\d{8}T\d{6}Z$")
PARTIAL_SUFFIX = ".partial"
MANIFEST = "manifest.json"
LOCK = ".lock"


class StorageError(Exception):
    pass


class LockedError(StorageError):
    pass


def format_ts(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime(TS_FORMAT)


def parse_ts(name: str) -> datetime:
    return datetime.strptime(name, TS_FORMAT).replace(tzinfo=UTC)


@dataclass
class BackupSet:
    target: str
    timestamp: datetime
    path: Path
    manifest: dict | None

    @property
    def ok(self) -> bool:
        return bool(self.manifest) and self.manifest.get("status") == "ok"

    @property
    def name(self) -> str:
        return self.path.name


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    def target_dir(self, target: str) -> Path:
        return self.root / target

    # -- reading ---------------------------------------------------------------

    def sets(self, target: str) -> list[BackupSet]:
        """Finished sets for ``target``, oldest first."""
        tdir = self.target_dir(target)
        if not tdir.is_dir():
            return []
        result = []
        for entry in tdir.iterdir():
            if not (entry.is_dir() and TS_RE.match(entry.name)):
                continue
            result.append(BackupSet(target, parse_ts(entry.name), entry, _load_manifest(entry)))
        result.sort(key=lambda s: s.timestamp)
        return result

    def latest_ok(self, target: str) -> BackupSet | None:
        ok = [s for s in self.sets(target) if s.ok]
        return ok[-1] if ok else None

    def find(self, target: str, which: str = "latest") -> BackupSet:
        if which == "latest":
            found = self.latest_ok(target)
            if found is None:
                raise StorageError(f"no successful backup sets for target {target!r} in {self.target_dir(target)}")
            return found
        for s in self.sets(target):
            if s.name == which:
                if not s.ok:
                    raise StorageError(f"backup set {target}/{which} has no valid manifest")
                return s
        raise StorageError(f"backup set {target}/{which} not found")

    # -- writing ---------------------------------------------------------------

    def begin(self, target: str, ts: datetime) -> Path:
        tdir = self.target_dir(target)
        tdir.mkdir(parents=True, exist_ok=True)
        partial = tdir / (format_ts(ts) + PARTIAL_SUFFIX)
        partial.mkdir()
        return partial

    def finalize(self, partial: Path, manifest: dict) -> BackupSet:
        """Checksum every file, write the manifest, fsync, and rename the set into place."""
        if not partial.name.endswith(PARTIAL_SUFFIX):
            raise StorageError(f"not a partial set: {partial}")
        manifest = dict(manifest)
        manifest["files"] = checksum_tree(partial)
        manifest["size_bytes"] = sum(f["size"] for f in manifest["files"].values())

        tmp = partial / (MANIFEST + ".tmp")
        with open(tmp, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, partial / MANIFEST)
        _fsync_tree(partial)

        final = partial.with_name(partial.name.removesuffix(PARTIAL_SUFFIX))
        os.rename(partial, final)
        _fsync_dir(final.parent)
        return BackupSet(final.parent.name, parse_ts(final.name), final, manifest)

    def discard(self, partial: Path) -> None:
        shutil.rmtree(partial, ignore_errors=True)

    # -- locking ---------------------------------------------------------------

    @contextlib.contextmanager
    def lock(self, target: str, stale_after: timedelta) -> Iterator[None]:
        """Per-target lock file, created with O_EXCL (which NFSv3+ honours)."""
        tdir = self.target_dir(target)
        tdir.mkdir(parents=True, exist_ok=True)
        path = tdir / LOCK
        info = json.dumps({"host": socket.gethostname(), "pid": os.getpid(), "started": datetime.now(UTC).isoformat()})
        for attempt in (1, 2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                try:
                    age = datetime.now(UTC) - datetime.fromtimestamp(path.stat().st_mtime, UTC)
                    holder = path.read_text().strip()
                except FileNotFoundError:
                    continue  # released while we looked
                if attempt == 1 and age > stale_after:
                    log.warning("%s: removing stale lock (%s old): %s", target, age, holder)
                    path.unlink(missing_ok=True)
                    continue
                raise LockedError(f"target {target!r} is locked by another run: {holder}") from None
            with os.fdopen(fd, "w") as f:
                f.write(info + "\n")
            break
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    # -- retention -------------------------------------------------------------

    def prune(
        self, target: str, policy: Retention, *, stale_partial_after: timedelta, now: datetime | None = None, dry_run: bool = False
    ) -> list[Path]:
        """Delete sets the policy no longer keeps, and abandoned partial sets. Returns what was (or would be) deleted."""
        now = now or datetime.now(UTC)
        doomed: list[Path] = []

        sets = self.sets(target)
        ok = [s for s in sets if s.ok]
        keep = select_keep((s.timestamp for s in ok), policy)
        doomed += [s.path for s in ok if s.timestamp not in keep]
        for s in sets:
            if not s.ok:
                log.warning("%s: leaving set %s alone: it has no valid manifest", target, s.name)

        tdir = self.target_dir(target)
        if tdir.is_dir():
            for entry in tdir.iterdir():
                if entry.is_dir() and entry.name.endswith(PARTIAL_SUFFIX):
                    with contextlib.suppress(ValueError):
                        if now - parse_ts(entry.name.removesuffix(PARTIAL_SUFFIX)) > stale_partial_after:
                            doomed.append(entry)

        for path in doomed:
            log.info("%s: %s %s", target, "would delete" if dry_run else "deleting", path.name)
            if not dry_run:
                shutil.rmtree(path)
        return doomed


def checksum_tree(root: Path) -> dict[str, dict]:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in (MANIFEST, MANIFEST + ".tmp"):
            files[path.relative_to(root).as_posix()] = {"size": path.stat().st_size, "sha256": sha256_file(path)}
    return files


def verify_checksums(bset: BackupSet) -> list[str]:
    """Problems found re-checking a set's files against its manifest (empty if none)."""
    problems = []
    expected = (bset.manifest or {}).get("files", {})
    actual = {p.relative_to(bset.path).as_posix() for p in bset.path.rglob("*") if p.is_file() and p.name != MANIFEST}
    for rel in sorted(set(expected) - actual):
        problems.append(f"missing file: {rel}")
    for rel in sorted(actual - set(expected)):
        problems.append(f"unexpected file: {rel}")
    for rel in sorted(set(expected) & actual):
        if sha256_file(bset.path / rel) != expected[rel]["sha256"]:
            problems.append(f"checksum mismatch: {rel}")
    return problems


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest(set_dir: Path) -> dict | None:
    try:
        manifest = json.loads((set_dir / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _fsync_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        elif path.is_dir():
            _fsync_dir(path)
    _fsync_dir(root)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
