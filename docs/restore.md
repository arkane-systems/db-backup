# Restoring from backup

This is the disaster-recovery runbook. It covers two cases:

- **With the tool**: `dbbackup restore`, the quick path.
- **With stock tools only**: `psql`, `pg_restore`, `mariadb`, `mongorestore` and `zstd`, for when the tool or its image isn't available. Nothing in a backup set needs this project to read it.

Test restores before you need them. A backup that has never been restored is only a hope. [Testing restores](#testing-restores) shows how to do it routinely, on a workstation.

## Finding a backup set

The NFS share, or the off-site copy of it, looks like this:

```
<backup root>/<target>/<YYYYMMDDTHHMMSSZ>/manifest.json
```

- **One directory per target.** A target is one database server from the config.
- **One subdirectory per backup set**, named by its UTC start time.
- **Only sets with a `manifest.json` are complete.** A `*.partial` directory is an interrupted run: ignore it.
- **The manifest lists everything in the set:** the server version, the databases, which file holds which database, an inventory of tables, collections and users, any warnings from the backup, and a SHA-256 for every file.

Check a set's files before restoring it. With the tool:

```bash
dbbackup verify <target> --set <YYYYMMDDTHHMMSSZ>
```

Or with stock tools, from inside the set directory:

```bash
jq -r '.files | to_entries[] | "\(.value.sha256)  \(.key)"' manifest.json | sha256sum -c --quiet
```

File names are the database names, percent-encoded. For example, database `odd.name/db` is stored as `odd%2Ename%2Fdb.pgdump`. The manifest's `contents` section maps each database to its file.

## Restoring with the tool

```bash
dbbackup restore <target> [--set latest|<timestamp>] [-d <database> ...] \
    [--host <host> --port <port> --username <admin user> --password-env <VAR>] \
    [--uri-env <VAR>]   # MongoDB: a full connection URI instead of host/port/username
    [--no-globals]      # skip users/roles
    [--force]           # replace databases that already exist
```

- **Default destination.** Without `--host` or `--uri-env`, it restores to the server the backup came from, using the target's own credentials from the config.
- **Use an admin account for restores.** The backup accounts are deliberately read-only.
- **Existing databases.** The tool refuses to overwrite a database that already exists unless you pass `--force`.
- **Existing accounts are left alone.** Users and roles that already exist on the destination keep their passwords and privileges. That includes the account you restore as, and a fresh server's own `root` and `healthcheck` accounts. Missing users and roles are created with their backed-up passwords and grants.
- **The set is checked first.** The tool re-checks every file's checksum before it restores anything.

### From Kubernetes

During a disaster you may not have the tool installed anywhere. Run it as a one-off Job from the same image, mounting the same share and config:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: db-restore
  namespace: db-backup
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      securityContext: {runAsNonRoot: true, runAsUser: 1004, runAsGroup: 1004}
      containers:
        - name: restore
          image: ghcr.io/arkane-systems/db-backup:0.2.0
          args: [restore, postgres-main, --host, new-postgres.example.lan, --username, postgres, --password-env, ADMIN_PASSWORD]
          env:
            - name: ADMIN_PASSWORD
              valueFrom: {secretKeyRef: {name: db-restore-admin, key: password}}
          volumeMounts:
            - {name: config, mountPath: /etc/dbbackup, readOnly: true}
            - {name: backups, mountPath: /backups, readOnly: true}
      volumes:
        - {name: config, configMap: {name: db-backup-config-<hash>}}   # kubectl -n db-backup get configmap
        - {name: backups, persistentVolumeClaim: {claimName: db-backup-nfs}}
```

### With Docker

```bash
docker run --rm -v /mnt/db-backups:/backups:ro -v $PWD/config.yaml:/etc/dbbackup/config.yaml:ro \
    -e ADMIN_PASSWORD ghcr.io/arkane-systems/db-backup:0.2.0 \
    restore mariadb-main --host new-mariadb.example.lan --username root --password-env ADMIN_PASSWORD
```

## Restoring with stock tools

Use client tools at least as new as the server that was backed up. The manifest's `server_version` field records which version that was.

### PostgreSQL

A set contains `globals.sql.zst` (roles and tablespaces) and one `<db>.pgdump/` directory per database. Each directory is a `pg_dump --format=directory` archive.

1. **Roles first.** Restored objects are owned by these roles and granted to them.

   ```bash
   zstd -dc globals.sql.zst | psql -X -h NEW -U postgres -d postgres
   ```

   "Role already exists" errors are harmless. Be aware, though, that this also replays `ALTER ROLE ... PASSWORD` for roles that **already exist** on the new server, including `postgres` itself. The tool skips those statements; by hand, delete the lines for existing roles first if their passwords must not change.

   If the manifest says `"role_passwords": false`, login roles were saved without passwords. Set them again with `ALTER ROLE ... PASSWORD`.

2. **Each database:**

   ```bash
   pg_restore -h NEW -U postgres -d postgres --create --jobs 4 app1.pgdump
   ```

   The `postgres` database already exists on every cluster, so restore it in place instead of creating it:

   ```bash
   pg_restore -h NEW -U postgres -d postgres --jobs 4 postgres.pgdump
   ```

3. **Refresh planner statistics,** which `pg_restore` doesn't do:

   ```bash
   vacuumdb -h NEW -U postgres --all --analyze-in-stages
   ```

### MariaDB

A set contains `system-users.sql.zst` (users, roles and grants) and one `<db>.sql.zst` per database. Each dump includes its own `CREATE DATABASE` and `USE`.

1. **Each database first.** Table-level grants can only be applied once their tables exist.

   ```bash
   zstd -dc shop.sql.zst | mariadb -h NEW -u root -p --max-allowed-packet=1G
   ```

2. **Then users and grants.** `--force` carries on past individual statements that fail.

   ```bash
   zstd -dc system-users.sql.zst | mariadb -h NEW -u root -p --force
   ```

   As with PostgreSQL, this re-applies the backed-up password of every account in it, including accounts that already exist on the new server, such as `root` or the Docker image's `healthcheck` user. The tool skips those; by hand, remove their lines first if that matters.

A manifest warning such as `shop.legacy_log uses Aria, not InnoDB` means that table wasn't dumped inside the same transaction as the rest. Check it before relying on it.

### MongoDB

**Whole-instance set.** `contents.mode` is `"instance"`, and there's a single `mongodb.archive.zst`:

```bash
zstd -dc mongodb.archive.zst | mongorestore --uri 'mongodb://root:...@NEW:27017/?authSource=admin' --archive --oplogReplay
```

- `--oplogReplay` brings every database to the same point in time. Use it whenever the manifest says `"oplog": true`.
- **Don't add `--drop`.** Besides dropping collections, it replaces existing users, including the one you're restoring as. Drop clashing databases yourself first instead.
- To restore only some databases, add `--nsInclude='shop.*'` (once per database) and leave out `--oplogReplay`, which can't be combined with namespace filters.

**Per-database set.** `contents.mode` is `"per-database"`. This happens when the target has `include` or `exclude` set. There's one `<db>.archive.zst` per database, plus `admin.archive.zst` for users and roles. Restore each one the same way, without `--oplogReplay`:

```bash
zstd -dc shop.archive.zst  | mongorestore --uri '...' --archive
zstd -dc admin.archive.zst | mongorestore --uri '...' --archive
```

## After restoring

- **Point applications at the new server,** and check they can log in. Restored application accounts keep their old passwords.
- **Check the restore against the backup's inventory.** The manifest's `inventory` lists every table and collection with an approximate row count, plus every user and role, as they were at backup time. It's a quick sanity check that nothing went missing.
- **Update the backup config** if the server's address changed, so tonight's backup covers the new server.

## Testing restores

`scripts/restore-test.sh` proves a backup can actually be restored, without touching any real server and without using the cluster. It runs on any machine with Docker and the backup share mounted (or a copy of it):

```bash
scripts/restore-test.sh /mnt/db-backups                         # the latest set of every target
scripts/restore-test.sh /mnt/db-backups postgres-main           # just one target
scripts/restore-test.sh --set 20261009T021500Z /mnt/db-backups mariadb-main
```

For each target, the script:

1. Reads the set's manifest, using the db-backup image, and starts a throwaway scratch server matching the backed-up server's version: `postgres:<major>`, `mariadb:<major.minor>` or `mongo:<major.minor>`. MongoDB runs as a single-node replica set, so the oplog is replayed as it would be in production. Each scratch server has its own compose project and network, and nothing is published on the host.
2. Runs `dbbackup restore-test`, which:
   - re-checks every file's checksum, and the dumps' integrity
   - restores the set exactly as `dbbackup restore` would, users and roles included
   - compares the restored server with the inventory recorded at backup time
3. Removes the scratch server and its data (unless you pass `--keep`), and goes on to the next target.

The comparison treats these as **errors**:

- a database missing from the restored server
- a missing table, view, materialized view or collection
- a missing user or role
- an object restored as a different kind (say, a view that came back as a table)

Row-count differences are only **warnings**. The restored server is always counted exactly; what it's compared with depends on the target's `exact_counts` setting:

- **`exact_counts: true`.** The set recorded a `COUNT(*)` of every table, just before the dump, and the counts must match exactly. Writes between the count and the dump's snapshot show up as small differences.
- **The default.** The set recorded the servers' cheap estimates (`pg_class.reltuples`, `estimatedDocumentCount`, and Aria/MyISAM row counts), and a difference of more than 25% (and more than 100 rows) is reported. InnoDB's estimate (`TABLE_ROWS`) can be off by orders of magnitude, for example straight after a bulk load, so MariaDB InnoDB tables have no recorded count: only their presence is checked, unless you set `exact_counts`. Sets made by 0.1.x recorded those InnoDB estimates anyway; their MariaDB counts are skipped, with a note saying why.

The run ends with a summary table, including how long each restore took, which is a rough figure for how long recovery would take. The script exits non-zero if any target fails.

| Option | Effect |
|---|---|
| `-s`, `--set SET` | Test this set instead of the latest. |
| `-i`, `--image IMAGE` | The db-backup image to use (default `$DBBACKUP_IMAGE`, else `ghcr.io/arkane-systems/db-backup:latest`). |
| `-b`, `--build` | Build the image from the checkout instead. |
| `-t`, `--tmpfs` | Keep the scratch servers' data in RAM. Faster, but the restored data must fit in memory. |
| `-k`, `--keep` | Leave each scratch server running afterwards, to look around in. The script prints how to remove it. |

To use a scratch image other than the one matching the recorded version, set `SCRATCH_IMAGE_POSTGRES`, `SCRATCH_IMAGE_MARIADB` or `SCRATCH_IMAGE_MONGODB`. For example, `SCRATCH_IMAGE_POSTGRES=postgres:19` rehearses an upgrade.

The backups are mounted read-only, and are read as the backup root's owner (`pg_dump`'s directory archives are private to their owner).

To restore-test one set by hand against a server you've started yourself:

```bash
dbbackup inspect /backups/postgres-main               # what's in the latest set
dbbackup restore-test /backups/postgres-main --host scratch --username postgres --password-env SCRATCH_PASSWORD
```
