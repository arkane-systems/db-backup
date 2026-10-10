"""The interface every database engine implements."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import quote

from ..config import Target


class EngineError(Exception):
    pass


@dataclass(frozen=True)
class Connection:
    """Where to connect. Built from a target, but restore can point it elsewhere."""

    host: str | None
    port: int
    username: str | None
    password: str | None = field(repr=False)
    uri: str | None = field(default=None, repr=False)

    @classmethod
    def for_target(cls, target: Target) -> Connection:
        return cls(target.host, target.port, target.username, target.password, target.uri)

    def overridden(self, **changes) -> Connection:
        return replace(self, **{k: v for k, v in changes.items() if v is not None})


@dataclass
class DumpResult:
    """What an engine's backup produced: engine-specific ``contents`` for the manifest, and any warnings."""

    contents: dict
    warnings: list[str] = field(default_factory=list)


class Engine(ABC):
    type: str
    min_version: tuple[int, ...]
    # Databases that are never backed up per-database (server internals).
    system_databases: frozenset[str] = frozenset()

    def __init__(self, target: Target, conn: Connection | None = None):
        self.target = target
        self.conn = conn or Connection.for_target(target)

    @abstractmethod
    def server_version(self) -> str:
        """The server's version string, e.g. ``18.6``."""

    @abstractmethod
    def list_databases(self) -> list[str]:
        """Every user database on the server, system databases excluded."""

    @abstractmethod
    def inventory(self, databases: list[str], *, exact: bool = False) -> dict:
        """Objects, row counts and users, recorded so a restore can be checked against them.

        Shape: ``{"counts": "exact" | "estimated", "databases": {db: {"objects": {name: {"kind": str, "rows": int | None}}}},
        "users": [str] | None}``. With ``exact``, row counts are COUNT(*)s (a scan of every table); otherwise they're
        the server's cheap estimates, or None where those are too unreliable to record.
        """

    @abstractmethod
    def backup(self, set_dir: Path, databases: list[str]) -> DumpResult:
        """Dump ``databases`` (plus server-level objects such as users) into ``set_dir``."""

    @abstractmethod
    def verify(self, set_dir: Path, manifest: dict) -> None:
        """Check the dump files are complete and readable; raise EngineError if not."""

    @abstractmethod
    def restore(self, set_dir: Path, manifest: dict, databases: list[str], *, force: bool, include_globals: bool) -> list[str]:
        """Restore ``databases`` (and, if asked, users/roles) from a set. Returns warnings."""

    def select_databases(self) -> list[str]:
        """The databases to back up: all of them, or ``include``, minus ``exclude``."""
        available = self.list_databases()
        if self.target.include:
            missing = [d for d in self.target.include if d not in available]
            if missing:
                raise EngineError(f"included database(s) not found on server: {', '.join(missing)}")
            chosen = list(self.target.include)
        else:
            chosen = available
        return [d for d in chosen if d not in self.target.exclude]

    def version_warning(self, version: str) -> str | None:
        if version_tuple(version) < self.min_version:
            minimum = ".".join(map(str, self.min_version))
            return f"server version {version} is older than the minimum supported {minimum}; backups are best-effort"
        return None


def version_tuple(version: str) -> tuple[int, ...]:
    m = re.match(r"\d+(?:\.\d+)*", version.strip())
    return tuple(int(p) for p in m.group(0).split(".")) if m else ()


def safe_filename(name: str) -> str:
    """A filesystem-safe, reversible file name for a database name (which may contain anything)."""
    return quote(name, safe="-_").replace(".", "%2E").replace("~", "%7E")
