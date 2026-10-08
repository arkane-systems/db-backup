# db-backup

A containerized database backup solution: logical backups of any number of **MariaDB**, **PostgreSQL** and **MongoDB** servers onto a shared filesystem (an NFS share, say), with grandfather-father-son retention and MQTT status reporting. It is built to run as a Kubernetes CronJob, and it can be run with Docker too.

- **Finds every database itself.** Each server's databases are enumerated on every run, minus any you exclude. It also saves the server-level objects needed to rebuild the server: PostgreSQL roles and tablespaces, MariaDB users and grants, MongoDB users and roles.
- **Makes restorable, consistent dumps:**
  - PostgreSQL: parallel directory-format `pg_dump`, plus `pg_dumpall --globals-only`.
  - MariaDB: `mariadb-dump --single-transaction`, plus `--system=users`.
  - MongoDB: a whole-instance `mongodump --oplog`, consistent to a single point in time.
- **Never leaves a half-finished set looking finished.** Each set is written to a `.partial` directory and verified, then given a manifest with SHA-256 checksums and renamed into place. Finished sets are never modified or renamed again, so an off-site sync of the share only ever uploads new files.
- **Keeps real GFS history:** the newest backup from each of the last N days, ISO weeks, months and years.
- **Isolates failures.** One server's failure never stops the others. A target is pruned only after one of its own backups succeeds, and the newest good set is always kept.
- **Reports to MQTT,** with Home Assistant discovery. Failures, and backups that have *stopped happening*, can raise [Alert Redux](https://github.com/arkane-systems/ha-alert-redux) alerts; see [docs/home-assistant.md](docs/home-assistant.md).
- **Restores with one command,** `dbbackup restore`. Every set can also be restored with stock client tools; see [docs/restore.md](docs/restore.md).

Supported servers: MariaDB 11.4+, PostgreSQL 18+, MongoDB 8.3+ (as a replica set, for `--oplog`). Older versions may well work: the tool logs a warning and carries on. The image ships the PostgreSQL 18 client, the MariaDB 11.8 client and MongoDB Database Tools 100.19. It is built for linux/amd64 only, because MongoDB publishes its Debian tools packages only for x86-64.

## How it works

Each run backs up every target in turn (or just the ones named on the command line):

1. Take the target's lock on the share.
2. Connect, enumerate the databases, and record an inventory: tables or collections with approximate row counts, plus users and roles.
3. Dump into `<root>/<target>/<timestamp>.partial/`.
4. Verify the dumps:
   - completion trailers and zstd integrity
   - `pg_restore --list`
   - the mongodump archive header
5. Write `manifest.json`, fsync, and rename the set to `<root>/<target>/<timestamp>/`.
6. Apply retention, and clear out abandoned partial sets.

A failed attempt is retried (`retries`), and failing that the run goes on to the next target. At the end, every target's status is published to MQTT. The process exits non-zero if any target failed, so the Kubernetes Job shows as failed too.

```
/backups/
  postgres-main/20261009T021500Z/
    manifest.json   globals.sql.zst   app1.pgdump/   postgres.pgdump/
  mariadb-main/20261009T021512Z/
    manifest.json   system-users.sql.zst   shop.sql.zst   wiki-db.sql.zst
  mongodb-main/20261009T021530Z/
    manifest.json   mongodb.archive.zst
```

## Configuration

A YAML file, at `/etc/dbbackup/config.yaml` by default (override with `--config` or `$DBBACKUP_CONFIG`). [deploy/k8s/config.yaml](deploy/k8s/config.yaml) is a complete, commented example.

Secrets never go in the file. Each one is named by an environment variable (`password_env`, `uri_env`) or a file (`password_file`, `uri_file`), such as a mounted Secret. Secrets are only resolved for the targets a command needs, so a restore needs only its own target's credentials. Secrets are never passed on a command line; they reach the client tools through the environment or `0600` temporary files.

| Setting | Where | Default | Meaning |
|---|---|---|---|
| `backup_root` | top level | `/backups` | Where sets are written. |
| `retention` | `defaults` or target | `{daily: 7, weekly: 4, monthly: 6}` | GFS policy. Also `yearly`, and `last` (keep the N newest outright). A target's values override the defaults key by key. |
| `compression_level` | `defaults` or target | `3` | zstd level, 1–19. |
| `retries`, `retry_delay_s` | `defaults` or target | `1`, `30` | Per-target retries within a run. |
| `jobs` | `defaults` or postgres target | `2` | Parallel `pg_dump` and `pg_restore` workers. |
| `stale_partial_hours`, `lock_stale_hours` | `defaults` | `24`, `24` | When abandoned partial sets and locks are cleaned up. |
| `name`, `type` | target | (required) | Name: letters, digits, `-`, `_`. Type: `postgres`, `mariadb` or `mongodb`. |
| `host`, `port`, `username`, `password_env`/`password_file` | target | (type's default port) | Connection details. |
| `include`, `exclude` | target | all databases | Limit which databases are backed up. |
| `sslmode` | postgres target | (libpq default) | `PGSSLMODE`. |
| `maintenance_db` | postgres target | `postgres` | Database used for enumeration and globals. |
| `ssl` | mariadb target | (client default: TLS on) | `true` or `false`. |
| `uri_env`/`uri_file` | mongodb target | (none) | A full connection URI, instead of host/port/username. |
| `uri_options` | mongodb target | (none) | Extra URI options when connecting by host, e.g. `replicaSet: rs0`. |
| `dump_args` | target | (none) | Extra arguments for each per-database dump command. |
| `mqtt` | top level | (none) | `host`, `port`, `username`, `password_env`/`password_file`, `tls`, `topic_prefix` (`dbbackup`), `ha_discovery` (`false`), `discovery_prefix` (`homeassistant`). |

For MongoDB, `include` or `exclude` changes how the backup is made. `mongodump` can't leave databases out of a whole-instance dump, so such a target gets one archive per database instead. Those dumps can't use `--oplog`, so they are consistent only per collection, and the manifest records a warning saying so. Leave both unset for point-in-time consistency.

## Database accounts

Each server needs a read-only account for backups. These are the privileges the integration tests run with.

**PostgreSQL.** Membership in `pg_read_all_data` lets the account read everything, including `pg_authid`. That means role passwords are saved too, without the account being a superuser. Also allow the account in `pg_hba.conf`.

```sql
CREATE ROLE dbbackup LOGIN PASSWORD '...' IN ROLE pg_read_all_data;
```

**MariaDB:**

```sql
CREATE USER 'dbbackup'@'%' IDENTIFIED BY '...';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES, PROCESS, RELOAD, SHOW DATABASES ON *.* TO 'dbbackup'@'%';
```

**MongoDB:**

```js
db.getSiblingDB("admin").createUser({ user: "dbbackup", pwd: "...", roles: ["backup"] })
```

Restores need an administrative account (`postgres`, `root`, MongoDB `root`), passed with `restore --username/--password-env` or `--uri-env`.

## Deploying to Kubernetes

[deploy/k8s](deploy/k8s) is a kustomize base with these pieces:

- **Namespace.**
- **Static NFS PersistentVolume and claim**, mounted `hard`.
- **ConfigMap** generated from `config.yaml`.
- **CronJob**, which runs daily at 02:15 UTC with:
  - `concurrencyPolicy: Forbid` and `backoffLimit: 0` (the tool retries per target itself)
  - a read-only root filesystem, non-root, no capabilities

```bash
# 1. Edit deploy/k8s/config.yaml and deploy/k8s/nfs-volume.yaml for your servers and share,
#    and set runAsUser/runAsGroup in cronjob.yaml to the NFS export's owner.
# 2. Create the credentials Secret (see deploy/k8s/secret.example.yaml):
kubectl create namespace db-backup
kubectl -n db-backup create secret generic db-backup-credentials \
  --from-literal=MARIADB_PASSWORD=... --from-literal=POSTGRES_PASSWORD=... \
  --from-literal=MONGODB_PASSWORD=... --from-literal=MQTT_PASSWORD=...
# 3. Deploy, then try a run now rather than waiting for 02:15:
kubectl apply -k deploy/k8s
kubectl -n db-backup create job --from=cronjob/db-backup db-backup-manual
kubectl -n db-backup logs -f job/db-backup-manual
```

The schedule is in `cronjob.yaml`. If you change it, adjust the staleness threshold in your Home Assistant alert to match.

## Commands

```
dbbackup backup  [TARGET ...]            back up (all targets by default), prune, publish status
dbbackup list    [TARGET ...]            list backup sets
dbbackup verify  [TARGET ...] [--set S | --all]   re-check checksums and dump integrity
dbbackup prune   [TARGET ...] [--dry-run]         apply retention without backing up
dbbackup restore TARGET [--set S] [-d DB ...] [--host H --port P --username U --password-env VAR | --uri-env VAR]
                        [--no-globals] [--force]
```

Exit status: 0 on success, 1 if anything failed, 2 for configuration or usage errors.

## Development

```bash
make venv          # .venv with the package, pytest and ruff
make lint test     # ruff, and unit tests (no servers needed)
make integration   # start compose.yaml's servers, then back up and restore them for real
make stack-down    # remove the test servers
```

The integration tests run in the `test` image target against [compose.yaml](compose.yaml). That stack has seeded source servers, empty restore servers with different admin passwords, and an MQTT broker. The tests check that backups are complete, that every manifest is right, and that restores reproduce the data, users, grants and passwords. They also check that restores leave the destination server's own accounts alone, and that failures, locking, pruning and corruption detection behave.

To run the tool by hand against the test servers:

```bash
make stack-up
docker compose --profile tool run --rm dbbackup backup     # writes to ./.backups
docker compose --profile tool run --rm dbbackup list
```

**Slow-to-start MariaDB on WSL.** On some WSL2 kernels, `mariadb:11.4` stalls for 15–40 s during first-boot initialization, and its entrypoint gives up after 30 s ("Unable to start server"). The compose file starts each restore server after its source to avoid this. If it still happens, `docker compose rm -sfv mariadb-restore && make stack-up` retries it.
