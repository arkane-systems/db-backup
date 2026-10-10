"""Configuration loading and validation.

The config file is YAML (normally a mounted ConfigMap). Secrets never appear in it
directly: each one is named by an environment variable (``*_env``) or a file path
(``*_file``, e.g. a mounted Secret), and resolved when the config is loaded.
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = "/etc/dbbackup/config.yaml"

TARGET_TYPES = ("postgres", "mariadb", "mongodb")
DEFAULT_PORTS = {"postgres": 5432, "mariadb": 3306, "mongodb": 27017}

# Target names become directory names and MQTT topic segments.
TARGET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Retention:
    """GFS retention: keep the newest set in each of the last N periods of each kind."""

    last: int = 0
    daily: int = 7
    weekly: int = 4
    monthly: int = 6
    yearly: int = 0

    FIELDS = ("last", "daily", "weekly", "monthly", "yearly")

    def merged(self, overrides: dict[str, Any] | None, where: str) -> Retention:
        if not overrides:
            return self
        _check_keys(overrides, set(self.FIELDS), where)
        values = {}
        for key, value in overrides.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError(f"{where}.{key}: must be a non-negative integer")
            values[key] = value
        return replace(self, **values)


@dataclass(frozen=True)
class Target:
    name: str
    type: str
    host: str | None
    port: int
    username: str | None
    password: str | None = field(repr=False)
    uri: str | None = field(repr=False)  # MongoDB only; overrides host/port/username/password
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    retention: Retention
    compression_level: int
    retries: int
    retry_delay_s: int
    jobs: int  # postgres: pg_dump/pg_restore -j
    maintenance_db: str  # postgres: database to connect to for enumeration and globals
    sslmode: str | None  # postgres: PGSSLMODE
    ssl: bool | None  # mariadb: None = client default, True = --ssl, False = --skip-ssl
    uri_options: dict[str, str]  # mongodb: extra URI query options when built from host/port
    dump_args: tuple[str, ...]  # extra args appended to each per-database dump command
    exact_counts: bool = False  # record COUNT(*)s in the inventory (scans every table) instead of estimates


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    username: str | None
    password: str | None = field(repr=False)
    tls: bool
    topic_prefix: str
    ha_discovery: bool
    discovery_prefix: str
    client_id: str


@dataclass(frozen=True)
class Config:
    backup_root: Path
    targets: tuple[Target, ...]
    mqtt: MqttConfig | None
    stale_partial_hours: int
    lock_stale_hours: int

    def select(self, names: list[str] | None) -> list[Target]:
        if not names:
            return list(self.targets)
        by_name = {t.name: t for t in self.targets}
        unknown = [n for n in names if n not in by_name]
        if unknown:
            raise ConfigError(f"unknown target(s): {', '.join(unknown)}")
        return [by_name[n] for n in names]


def load_config(
    path: str | os.PathLike, environ: dict[str, str] | None = None, *, secrets_for: Collection[str] | None = None, mqtt_secrets: bool = True
) -> Config:
    """Load and validate a config file.

    Secrets are resolved only for the targets named in ``secrets_for`` (all of them if
    None), so e.g. a restore needs only its own target's credentials to be available.
    """
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    return parse_config(raw, os.environ if environ is None else environ, secrets_for=secrets_for, mqtt_secrets=mqtt_secrets)


def parse_config(raw: Any, environ: dict[str, str], *, secrets_for: Collection[str] | None = None, mqtt_secrets: bool = True) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping")
    _check_keys(raw, {"backup_root", "defaults", "mqtt", "targets"}, "config")

    defaults = raw.get("defaults") or {}
    _check_keys(
        defaults,
        {"retention", "compression_level", "retries", "retry_delay_s", "stale_partial_hours", "lock_stale_hours", "jobs", "exact_counts"},
        "defaults",
    )
    base_retention = Retention().merged(defaults.get("retention"), "defaults.retention")

    raw_targets = raw.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigError("targets: must be a non-empty list")

    targets = []
    seen = set()
    for i, rt in enumerate(raw_targets):
        target = _parse_target(rt, f"targets[{i}]", defaults, base_retention, environ, secrets_for)
        if target.name in seen:
            raise ConfigError(f"targets[{i}].name: duplicate target name {target.name!r}")
        seen.add(target.name)
        targets.append(target)

    mqtt = _parse_mqtt(raw["mqtt"], environ, mqtt_secrets) if raw.get("mqtt") else None

    return Config(
        backup_root=Path(raw.get("backup_root", "/backups")),
        targets=tuple(targets),
        mqtt=mqtt,
        stale_partial_hours=_int(defaults, "stale_partial_hours", 24, "defaults", minimum=1),
        lock_stale_hours=_int(defaults, "lock_stale_hours", 24, "defaults", minimum=1),
    )


def _parse_target(
    rt: Any, where: str, defaults: dict, base_retention: Retention, environ: dict[str, str], secrets_for: Collection[str] | None
) -> Target:
    if not isinstance(rt, dict):
        raise ConfigError(f"{where}: must be a mapping")
    _check_keys(
        rt,
        {
            "name",
            "type",
            "host",
            "port",
            "username",
            "password_env",
            "password_file",
            "uri_env",
            "uri_file",
            "include",
            "exclude",
            "retention",
            "compression_level",
            "retries",
            "retry_delay_s",
            "jobs",
            "maintenance_db",
            "sslmode",
            "ssl",
            "uri_options",
            "dump_args",
            "exact_counts",
        },
        where,
    )

    name = rt.get("name")
    if not isinstance(name, str) or not TARGET_NAME_RE.match(name):
        raise ConfigError(f"{where}.name: must match {TARGET_NAME_RE.pattern}")
    where = f"target {name!r}"

    ttype = rt.get("type")
    if ttype not in TARGET_TYPES:
        raise ConfigError(f"{where}.type: must be one of {', '.join(TARGET_TYPES)}")

    resolve = secrets_for is None or name in secrets_for
    has_uri = "uri_env" in rt or "uri_file" in rt
    if has_uri and ttype != "mongodb":
        raise ConfigError(f"{where}: uri_env/uri_file are only valid for mongodb targets")
    uri = resolve_secret(rt, "uri", where, environ) if resolve else None

    host = rt.get("host")
    if not has_uri and not (isinstance(host, str) and host):
        raise ConfigError(f"{where}.host: required" + (" (or uri_env/uri_file)" if ttype == "mongodb" else ""))

    password = resolve_secret(rt, "password", where, environ) if resolve else None
    username = rt.get("username")
    if ("password_env" in rt or "password_file" in rt) and not username:
        raise ConfigError(f"{where}.username: required when a password is given")

    for key, only_for in (
        ("sslmode", "postgres"),
        ("maintenance_db", "postgres"),
        ("jobs", "postgres"),
        ("ssl", "mariadb"),
        ("uri_options", "mongodb"),
    ):
        if key in rt and ttype != only_for:
            raise ConfigError(f"{where}.{key}: only valid for {only_for} targets")
    ssl = rt.get("ssl")
    if ssl is not None and not isinstance(ssl, bool):
        raise ConfigError(f"{where}.ssl: must be true or false")
    uri_options = rt.get("uri_options") or {}
    if not isinstance(uri_options, dict):
        raise ConfigError(f"{where}.uri_options: must be a mapping")

    return Target(
        name=name,
        type=ttype,
        host=host,
        port=_int(rt, "port", DEFAULT_PORTS[ttype], where, minimum=1),
        username=username,
        password=password,
        uri=uri,
        include=_str_list(rt, "include", where),
        exclude=_str_list(rt, "exclude", where),
        retention=base_retention.merged(rt.get("retention"), f"{where}.retention"),
        compression_level=_int(rt, "compression_level", _int(defaults, "compression_level", 3, "defaults", 1, 19), where, 1, 19),
        retries=_int(rt, "retries", _int(defaults, "retries", 1, "defaults", minimum=0), where, minimum=0),
        retry_delay_s=_int(rt, "retry_delay_s", _int(defaults, "retry_delay_s", 30, "defaults", minimum=0), where, minimum=0),
        jobs=_int(rt, "jobs", _int(defaults, "jobs", 2, "defaults", minimum=1), where, minimum=1),
        maintenance_db=rt.get("maintenance_db", "postgres"),
        sslmode=rt.get("sslmode"),
        ssl=ssl,
        uri_options={str(k): str(v) for k, v in uri_options.items()},
        dump_args=_str_list(rt, "dump_args", where),
        exact_counts=_bool(rt, "exact_counts", _bool(defaults, "exact_counts", False, "defaults"), where),
    )


def _parse_mqtt(rm: Any, environ: dict[str, str], resolve: bool) -> MqttConfig:
    where = "mqtt"
    if not isinstance(rm, dict):
        raise ConfigError(f"{where}: must be a mapping")
    _check_keys(
        rm,
        {
            "host",
            "port",
            "username",
            "password_env",
            "password_file",
            "tls",
            "topic_prefix",
            "ha_discovery",
            "discovery_prefix",
            "client_id",
        },
        where,
    )
    if not isinstance(rm.get("host"), str) or not rm["host"]:
        raise ConfigError(f"{where}.host: required")
    tls = bool(rm.get("tls", False))
    return MqttConfig(
        host=rm["host"],
        port=_int(rm, "port", 8883 if tls else 1883, where, minimum=1),
        username=rm.get("username"),
        password=resolve_secret(rm, "password", where, environ) if resolve else None,
        tls=tls,
        topic_prefix=str(rm.get("topic_prefix", "dbbackup")).rstrip("/"),
        ha_discovery=bool(rm.get("ha_discovery", False)),
        discovery_prefix=str(rm.get("discovery_prefix", "homeassistant")).rstrip("/"),
        client_id=str(rm.get("client_id", "dbbackup")),
    )


def resolve_secret(section: dict, base: str, where: str, environ: dict[str, str]) -> str | None:
    """Resolve ``<base>_env`` or ``<base>_file`` to its value; None if neither is set."""
    env_key, file_key = f"{base}_env", f"{base}_file"
    if env_key in section and file_key in section:
        raise ConfigError(f"{where}: set only one of {env_key} and {file_key}")
    if env_key in section:
        var = section[env_key]
        if var not in environ:
            raise ConfigError(f"{where}.{env_key}: environment variable {var} is not set")
        return environ[var]
    if file_key in section:
        try:
            return Path(section[file_key]).read_text().rstrip("\r\n")
        except OSError as e:
            raise ConfigError(f"{where}.{file_key}: cannot read {section[file_key]}: {e.strerror}") from e
    return None


def _check_keys(section: Any, allowed: set[str], where: str) -> None:
    if not isinstance(section, dict):
        raise ConfigError(f"{where}: must be a mapping")
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(unknown)}")


def _int(section: dict, key: str, default: int, where: str, minimum: int | None = None, maximum: int | None = None) -> int:
    value = section.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{where}.{key}: must be an integer")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ConfigError(f"{where}.{key}: must be between {minimum} and {maximum}" if maximum else f"{where}.{key}: must be >= {minimum}")
    return value


def _bool(section: dict, key: str, default: bool, where: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key}: must be true or false")
    return value


def _str_list(section: dict, key: str, where: str) -> tuple[str, ...]:
    value = section.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}.{key}: must be a list of strings")
    return tuple(value)
