"""Backing up targets: one at a time, each isolated from the others' failures."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from . import __version__
from .config import Config, Target
from .engines import engine_for
from .storage import BackupSet, Store

log = logging.getLogger(__name__)

MANIFEST_FORMAT = 1


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def backup_targets(config: Config, targets: list[Target]) -> list[dict]:
    store = Store(config.backup_root)
    return [backup_target(config, store, target) for target in targets]


def backup_target(config: Config, store: Store, target: Target) -> dict:
    """Back up one target, with retries, then apply retention. Returns its status; never raises."""
    started = now_utc()
    status = {
        "target": target.name,
        "type": target.type,
        "state": "failed",
        "started": started.isoformat(),
        "finished": None,
        "duration_s": None,
        "set": None,
        "size_bytes": None,
        "databases": [],
        "warnings": [],
        "error": None,
        "last_success": None,
    }
    log.info("%s: starting %s backup", target.name, target.type)
    try:
        with store.lock(target.name, timedelta(hours=config.lock_stale_hours)):
            attempt = 0
            while True:
                attempt += 1
                try:
                    bset = backup_once(store, target)
                    break
                except Exception as e:
                    if attempt > target.retries:
                        raise
                    log.warning("%s: attempt %d failed (%s); retrying in %ds", target.name, attempt, e, target.retry_delay_s)
                    time.sleep(target.retry_delay_s)

            status.update(
                state="ok",
                set=bset.name,
                size_bytes=bset.manifest["size_bytes"],
                databases=bset.manifest["databases"],
                warnings=list(bset.manifest["warnings"]),
            )
            for warning in status["warnings"]:
                log.warning("%s: %s", target.name, warning)
            log.info("%s: backup set %s complete (%s)", target.name, bset.name, human_size(bset.manifest["size_bytes"]))

            # Only after a success: a failing target keeps every set it has.
            try:
                store.prune(target.name, target.retention, stale_partial_after=timedelta(hours=config.stale_partial_hours))
            except Exception as e:
                log.exception("%s: pruning failed", target.name)
                status["warnings"].append(f"pruning old backup sets failed: {e}")
    except Exception as e:
        log.error("%s: backup failed: %s", target.name, e)
        log.debug("%s: traceback", target.name, exc_info=True)
        status["error"] = str(e)

    finished = now_utc()
    status["finished"] = finished.isoformat()
    status["duration_s"] = int((finished - started).total_seconds())
    latest = store.latest_ok(target.name)
    status["last_success"] = latest.manifest.get("finished") if latest else None
    return status


def backup_once(store: Store, target: Target) -> BackupSet:
    engine = engine_for(target)
    started = now_utc()

    version = engine.server_version()
    warnings = []
    if warning := engine.version_warning(version):
        warnings.append(warning)
    databases = engine.select_databases()
    if not databases:
        warnings.append("no databases selected; only server-level objects (users/roles) were backed up")
    log.info("%s: server %s, %d database(s): %s", target.name, version, len(databases), ", ".join(databases))
    inventory = engine.inventory(databases, exact=target.exact_counts)

    partial = store.begin(target.name, started)
    try:
        result = engine.backup(partial, databases)
        manifest = {
            "format": MANIFEST_FORMAT,
            "tool_version": __version__,
            "target": target.name,
            "type": target.type,
            "host": engine.conn.host,
            "port": engine.conn.port,
            "server_version": version,
            "started": started.isoformat(),
            "databases": databases,
            "contents": result.contents,
            "inventory": inventory,
            "warnings": warnings + result.warnings,
            "status": "ok",
        }
        engine.verify(partial, manifest)
        finished = now_utc()
        manifest["finished"] = finished.isoformat()
        manifest["duration_s"] = int((finished - started).total_seconds())
        return store.finalize(partial, manifest)
    except BaseException:
        store.discard(partial)
        raise


def human_size(n: int | None) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")
