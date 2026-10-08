"""MQTT status publishing, with optional Home Assistant discovery.

Every message is retained, so Home Assistant (or anything else) always sees each
target's latest status, and can tell from ``last_success`` when backups have
stopped happening at all, which a failure notification alone never reveals.
"""

from __future__ import annotations

import json
import logging
import re

from . import __version__
from .config import MqttConfig

log = logging.getLogger(__name__)

DEVICE = {
    "identifiers": ["dbbackup"],
    "name": "DB Backup",
    "manufacturer": "Arkane Systems",
    "model": "db-backup",
    "sw_version": __version__,
}
ORIGIN = {"name": "db-backup", "sw_version": __version__, "support_url": "https://github.com/arkane-systems/db-backup"}


def entity_slug(target: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", target.lower())


def status_topic(cfg: MqttConfig, target: str) -> str:
    return f"{cfg.topic_prefix}/{target}/status"


def discovery_messages(cfg: MqttConfig, target: str) -> list[tuple[str, dict]]:
    """HA MQTT discovery configs for a target's two sensors.

    Entity IDs are fixed (``sensor.dbbackup_<target>_status`` and ``..._last_success``)
    so Alert Redux generators can target them by pattern.
    """
    slug = entity_slug(target)
    state_topic = status_topic(cfg, target)
    sensors = {
        "status": {
            "name": f"{target} status",
            "value_template": "{{ value_json.state }}",
            "json_attributes_topic": state_topic,
            "icon": "mdi:database-check",
        },
        "last_success": {
            "name": f"{target} last success",
            "value_template": "{{ value_json.last_success }}",
            "device_class": "timestamp",
            "icon": "mdi:database-clock",
        },
    }
    messages = []
    for key, extra in sensors.items():
        object_id = f"dbbackup_{slug}_{key}"
        payload = {
            "unique_id": object_id,
            "default_entity_id": f"sensor.{object_id}",
            "state_topic": state_topic,
            "device": DEVICE,
            "origin": ORIGIN,
            **extra,
        }
        messages.append((f"{cfg.discovery_prefix}/sensor/{object_id}/config", payload))
    return messages


def publish_statuses(cfg: MqttConfig | None, statuses: list[dict]) -> bool:
    """Publish each target's status, and an overall summary, retained. Never raises: returns False on failure."""
    if cfg is None:
        return True

    messages: list[tuple[str, dict]] = []
    for status in statuses:
        if cfg.ha_discovery:
            messages += discovery_messages(cfg, status["target"])
        messages.append((status_topic(cfg, status["target"]), status))
    overall = {
        "state": "ok" if all(s["state"] == "ok" for s in statuses) else "failed",
        "finished": max((s["finished"] for s in statuses), default=None),
        "targets": {s["target"]: s["state"] for s in statuses},
    }
    messages.append((f"{cfg.topic_prefix}/status", overall))

    try:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg.client_id)
        if cfg.username:
            client.username_pw_set(cfg.username, cfg.password)
        if cfg.tls:
            client.tls_set()
        client.connect(cfg.host, cfg.port, keepalive=30)
        client.loop_start()
        try:
            infos = [client.publish(topic, json.dumps(payload), qos=1, retain=True) for topic, payload in messages]
            for info in infos:
                info.wait_for_publish(timeout=15)
                if not info.is_published():
                    raise TimeoutError("MQTT broker did not acknowledge a publish in time")
        finally:
            client.disconnect()
            client.loop_stop()
    except Exception as e:  # never let notification problems fail a backup run
        log.error("MQTT: could not publish status to %s:%d: %s", cfg.host, cfg.port, e)
        return False
    log.info("MQTT: published status for %d target(s)", len(statuses))
    return True
