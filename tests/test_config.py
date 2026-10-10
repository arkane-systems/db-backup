import pytest

from dbbackup.config import ConfigError, Retention, parse_config


def minimal(**target_overrides):
    target = {"name": "pg", "type": "postgres", "host": "db", "username": "u", "password_env": "PW"}
    target.update(target_overrides)
    return {"targets": [target]}


ENV = {"PW": "s3cret", "URI": "mongodb://u:p@h/"}


def test_minimal_config_gets_defaults():
    config = parse_config(minimal(), ENV)
    (t,) = config.targets
    assert str(config.backup_root) == "/backups"
    assert t.port == 5432
    assert t.password == "s3cret"
    assert t.retention == Retention(last=0, daily=7, weekly=4, monthly=6, yearly=0)
    assert t.compression_level == 3
    assert t.retries == 1
    assert config.mqtt is None


def test_default_ports_by_type():
    raw = {
        "targets": [
            {"name": "a", "type": "postgres", "host": "h"},
            {"name": "b", "type": "mariadb", "host": "h"},
            {"name": "c", "type": "mongodb", "host": "h"},
        ]
    }
    assert [t.port for t in parse_config(raw, {}).targets] == [5432, 3306, 27017]


def test_retention_merges_defaults_then_target():
    raw = minimal(retention={"daily": 14})
    raw["defaults"] = {"retention": {"monthly": 12, "yearly": 3}}
    (t,) = parse_config(raw, ENV).targets
    assert t.retention == Retention(last=0, daily=14, weekly=4, monthly=12, yearly=3)


def test_password_from_file_strips_trailing_newline(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("from-file\n")
    raw = minimal()
    del raw["targets"][0]["password_env"]
    raw["targets"][0]["password_file"] = str(secret)
    (t,) = parse_config(raw, ENV).targets
    assert t.password == "from-file"


def test_secret_never_appears_in_repr():
    (t,) = parse_config(minimal(), ENV).targets
    assert "s3cret" not in repr(t)


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"targets": []}, "non-empty list"),
        (minimal(type="oracle"), "type: must be one of"),
        (minimal(name="bad name"), "name: must match"),
        (minimal(password_env="MISSING"), "MISSING is not set"),
        (minimal(host=None), "host: required"),
        (minimal(colour="blue"), "unknown key(s): colour"),
        (minimal(ssl=True), "ssl: only valid for mariadb"),
        (minimal(uri_env="URI"), "only valid for mongodb"),
        (minimal(retention={"daily": -1}), "non-negative integer"),
        (minimal(retention={"hourly": 1}), "unknown key(s): hourly"),
        (minimal(compression_level=25), "between 1 and 19"),
        (minimal(username=None), "username: required"),
        ({"targets": [{"name": "x", "type": "mongodb"}]}, "host: required (or uri_env/uri_file)"),
    ],
)
def test_invalid_configs(raw, message):
    with pytest.raises(ConfigError, match=None) as exc:
        parse_config(raw, ENV)
    assert message in str(exc.value)


def test_duplicate_target_names():
    raw = minimal()
    raw["targets"].append(dict(raw["targets"][0]))
    with pytest.raises(ConfigError, match="duplicate target name"):
        parse_config(raw, ENV)


def test_both_env_and_file_is_an_error():
    with pytest.raises(ConfigError, match="set only one"):
        parse_config(minimal(password_file="/x"), ENV)


def test_mongodb_uri_target():
    raw = {"targets": [{"name": "m", "type": "mongodb", "uri_env": "URI"}]}
    (t,) = parse_config(raw, ENV).targets
    assert t.uri == "mongodb://u:p@h/"
    assert t.host is None


def test_secrets_resolved_only_for_named_targets():
    raw = minimal()
    raw["targets"].append({"name": "other", "type": "mariadb", "host": "h", "username": "u", "password_env": "UNSET"})
    raw["mqtt"] = {"host": "mq", "username": "u", "password_env": "ALSO_UNSET"}
    with pytest.raises(ConfigError, match="UNSET"):
        parse_config(raw, ENV)
    config = parse_config(raw, ENV, secrets_for=["pg"], mqtt_secrets=False)
    assert [t.password for t in config.targets] == ["s3cret", None]
    assert config.mqtt.password is None


def test_select_targets():
    raw = minimal()
    raw["targets"].append({"name": "two", "type": "mariadb", "host": "h"})
    config = parse_config(raw, ENV)
    assert [t.name for t in config.select(None)] == ["pg", "two"]
    assert [t.name for t in config.select(["two"])] == ["two"]
    with pytest.raises(ConfigError, match="unknown target"):
        config.select(["nope"])


def test_mqtt_defaults():
    raw = minimal()
    raw["mqtt"] = {"host": "mq"}
    mqtt = parse_config(raw, ENV).mqtt
    assert (mqtt.port, mqtt.topic_prefix, mqtt.ha_discovery, mqtt.discovery_prefix) == (1883, "dbbackup", False, "homeassistant")
    raw["mqtt"] = {"host": "mq", "tls": True, "topic_prefix": "x/y/"}
    mqtt = parse_config(raw, ENV).mqtt
    assert (mqtt.port, mqtt.topic_prefix) == (8883, "x/y")


def test_exact_counts_default_and_override():
    raw = minimal()
    raw["targets"].append({"name": "two", "type": "mariadb", "host": "h", "exact_counts": True})
    assert [t.exact_counts for t in parse_config(raw, ENV).targets] == [False, True]
    raw["defaults"] = {"exact_counts": True}
    raw["targets"][1]["exact_counts"] = False
    assert [t.exact_counts for t in parse_config(raw, ENV).targets] == [True, False]
    raw["targets"][1]["exact_counts"] = "yes"
    with pytest.raises(ConfigError, match="must be true or false"):
        parse_config(raw, ENV)
