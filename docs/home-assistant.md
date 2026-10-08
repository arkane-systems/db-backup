# Home Assistant alerts with Alert Redux

db-backup publishes each target's status to MQTT as a retained message:

- `<topic_prefix>/<target>/status`: one per target.
- `<topic_prefix>/status`: an overall summary.

With `ha_discovery: true`, it also publishes MQTT discovery configs. These create two sensors per target, on a single **DB Backup** device:

| Entity | State | Notes |
|---|---|---|
| `sensor.dbbackup_<target>_status` | `ok` or `failed` | The status message's fields are its attributes: `target`, `error`, `warnings`, `last_success`, `set`, `size_bytes`, `databases`, `duration_s`. |
| `sensor.dbbackup_<target>_last_success` | timestamp | When the newest successful backup finished. |

In entity IDs, a target name is lowercased and anything other than letters, digits and underscores becomes `_`. For example, `postgres-main` gives `sensor.dbbackup_postgres_main_status`.

Every target's sensors follow the same pattern, so two [Alert Redux](https://github.com/arkane-systems/ha-alert-redux) **generators** cover all targets, now and later. A new target gets its alerts as soon as its first run publishes its sensors.

## Generator 1: backup failed

This fires as soon as a run reports a failure.

**Settings → Devices & Services → Alert Redux → Add generator → State**

| Field | Value |
|---|---|
| Name | `Backup failed` |
| Targets → entity ID pattern | `sensor.dbbackup_*_status` |
| Target state | `failed` |
| Priority | Critical |
| On message | `Backup of {{ state_attr(target, 'target') }} failed: {{ state_attr(target, 'error') }}` |

It stops firing by itself once a later run of that target succeeds.

## Generator 2: backup stale (the dead man's switch)

A CronJob that never runs reports nothing, so failure alerts alone can't catch it: nothing ever fails. This generator fires when a target's last success is too old, whatever the reason. The schedule may be broken, the cluster may be down, or a run may have hung.

**Add generator → Template**

| Field | Value |
|---|---|
| Name | `Backup stale` |
| Targets → entity ID pattern | `sensor.dbbackup_*_status` |
| Template | see below |
| Priority | Critical |
| On message | `No successful backup of {{ state_attr(target, 'target') }} since {{ state_attr(target, 'last_success') or 'ever' }}` |

```jinja
{% set last = state_attr(target, 'last_success') %}
{{ last is none or now() - as_datetime(last) > timedelta(hours=26) }}
```

The 26 hours suit a daily schedule: a day, plus slack for a long run. Use your interval plus a margin if you change the schedule. A target that has never succeeded counts as stale.

### Supersession

Both generators target the same status sensor, so they can be linked. In **Backup failed**'s **Supersession** section, add the **Backup stale** generator: each target's *Backup failed* alert then supersedes its *Backup stale* alert. While a target is failing you hear about the failure; you don't get a second alert a day later saying the same thing.

The staleness alert still speaks up on its own when backups simply stop happening, since then nothing is failing to supersede it.

## Notes

- **Retained messages and broker persistence.** The status messages are retained, so the sensors survive Home Assistant restarts. If your broker doesn't persist retained messages across its own restarts, the sensors become `unknown` after a broker restart until the next backup run. Alert Redux shows that as `no_data` rather than silently not firing.
- **Warnings are not failures.** A run with warnings, such as a MariaDB table that isn't InnoDB or a PostgreSQL role that couldn't read passwords, is still `ok`. The warnings are in the status sensor's `warnings` attribute. To be alerted about them, add a template generator on `{{ state_attr(target, 'warnings') | length > 0 }}` at a lower priority.
