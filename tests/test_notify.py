from dbbackup.config import parse_config
from dbbackup.notify import discovery_messages, entity_slug, publish_statuses


def mqtt_config(**extra):
    raw = {"targets": [{"name": "t", "type": "postgres", "host": "h"}], "mqtt": {"host": "mq", "ha_discovery": True, **extra}}
    return parse_config(raw, {}).mqtt


def test_entity_slug():
    assert entity_slug("PG-Main_1") == "pg_main_1"


def test_discovery_uses_predictable_entity_ids():
    (status_topic, status), (last_topic, last) = discovery_messages(mqtt_config(), "pg-main")
    assert status_topic == "homeassistant/sensor/dbbackup_pg_main_status/config"
    assert status["default_entity_id"] == "sensor.dbbackup_pg_main_status"
    assert status["state_topic"] == "dbbackup/pg-main/status"
    assert status["json_attributes_topic"] == "dbbackup/pg-main/status"
    assert last_topic == "homeassistant/sensor/dbbackup_pg_main_last_success/config"
    assert last["default_entity_id"] == "sensor.dbbackup_pg_main_last_success"
    assert last["device_class"] == "timestamp"
    assert status["device"] == last["device"]


def test_custom_prefixes():
    ((topic, payload), _) = discovery_messages(mqtt_config(topic_prefix="site/backups", discovery_prefix="ha"), "x")
    assert topic.startswith("ha/sensor/")
    assert payload["state_topic"] == "site/backups/x/status"


def test_publish_failure_is_reported_not_raised():
    # Nothing listens on port 1 of localhost.
    cfg = mqtt_config(host="127.0.0.1", port=1)
    status = {"target": "t", "state": "ok", "finished": "2026-01-01T00:00:00+00:00"}
    assert publish_statuses(cfg, [status]) is False


def test_no_mqtt_config_is_a_no_op():
    assert publish_statuses(None, [{"target": "t", "state": "ok", "finished": None}]) is True
