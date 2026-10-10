#!/usr/bin/env bash
# Restore-test backup sets in throwaway local database servers.
#
# For each target, starts a scratch server matching the backed-up server's version
# (in its own compose project), restores the chosen set into it with
# `dbbackup restore-test`, checks the result against the set's recorded inventory,
# and removes the scratch server and its data. Needs only Docker on the host.
#
#   scripts/restore-test.sh [options] BACKUP_ROOT [TARGET...]
#
# BACKUP_ROOT is the backup share (mounted locally) or a copy of it; it is mounted
# read-only. With no TARGETs, every target directory with a finished set is tested.
#
# Options:
#   -s, --set SET       set to test: "latest" (default) or a timestamp like 20261009T021500Z
#   -i, --image IMAGE   db-backup image to test with (default: $DBBACKUP_IMAGE, else
#                       ghcr.io/arkane-systems/db-backup:latest)
#   -b, --build         build the image from this checkout instead
#   -t, --tmpfs         keep the scratch servers' data in RAM: faster, but it must fit
#   -k, --keep          leave each scratch server running afterwards, for inspection
#   -h, --help          show this help
#
# Scratch images follow the recorded server version (postgres:<major>,
# mariadb:<major.minor>, mongo:<major.minor>); override them with
# SCRATCH_IMAGE_POSTGRES, SCRATCH_IMAGE_MARIADB or SCRATCH_IMAGE_MONGODB.
#
# Exits non-zero if any target fails.

set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$here/compose.restore-test.yaml"

usage() { sed -n '2,/^$/{s/^# \{0,1\}//;p}' "${BASH_SOURCE[0]}"; }
die() { echo "restore-test: $*" >&2; exit 2; }

set_name=latest
image=${DBBACKUP_IMAGE:-ghcr.io/arkane-systems/db-backup:latest}
build=false
storage=volume
keep=false
positional=()
while (($#)); do
    case $1 in
        -s|--set) set_name=${2:?--set needs a value}; shift 2 ;;
        -i|--image) image=${2:?--image needs a value}; shift 2 ;;
        -b|--build) build=true; shift ;;
        -t|--tmpfs) storage=tmpfs; shift ;;
        -k|--keep) keep=true; shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift; positional+=("$@"); break ;;
        -*) die "unknown option $1 (see --help)" ;;
        *) positional+=("$1"); shift ;;
    esac
done
((${#positional[@]})) || { usage >&2; exit 2; }

command -v docker >/dev/null || die "docker is not installed"
root=$(cd "${positional[0]}" 2>/dev/null && pwd) || die "${positional[0]}: no such directory"
targets=("${positional[@]:1}")

# Read the backups as their owner (pg_dump's directory archives are mode 0700).
run_as=$(stat -c %u:%g "$root" 2>/dev/null || echo "$(id -u):$(id -g)")

if $build; then
    image=dbbackup:restore-test
    echo "==> building $image from $here"
    if ! build_log=$(docker build --load --quiet --target runtime -t "$image" "$here" 2>&1); then
        echo "$build_log" >&2
        die "image build failed"
    fi
fi

if ((${#targets[@]} == 0)); then
    for dir in "$root"/*/; do
        name=$(basename "$dir")
        compgen -G "$dir[0-9]*T[0-9]*Z/manifest.json" >/dev/null && targets+=("$name")
    done
    ((${#targets[@]})) || die "no target directories with finished sets in $root"
fi

export BACKUP_ROOT=$root DBBACKUP_IMAGE=$image SCRATCH_STORAGE=$storage RUN_AS=$run_as

current_project=""
cleanup() {
    if [[ -n $current_project ]] && ! $keep; then
        docker compose -p "$current_project" -f "$compose_file" --profile '*' down -v --remove-orphans >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT
trap 'echo; echo "restore-test: interrupted" >&2; exit 130' INT TERM

results=()
failed=0
for target in "${targets[@]}"; do
    project="dbbackup-rt-$(tr -c 'a-z0-9_\n-' '_' <<<"${target,,}")"
    dc=(docker compose --progress quiet -p "$project" -f "$compose_file")
    current_project=$project
    echo
    echo "==> $target"

    if ! info=$("${dc[@]}" run --rm --no-deps -T restore-test inspect "/backups/$target" --set "$set_name" --format env); then
        results+=("$target|-|-|ERROR (no usable set)|-")
        failed=1
        cleanup
        continue
    fi
    DBB_TYPE="" DBB_SET="" DBB_SERVER_VERSION=""
    while IFS= read -r line; do
        [[ $line =~ ^DBB_[A-Z_]+= ]] && eval "$line"  # values are shell-quoted by dbbackup
    done <<<"$info"

    version=${DBB_SERVER_VERSION%%-*}   # 11.4.13-MariaDB-ubu2404 -> 11.4.13
    major=${version%%.*}
    minor=${version#*.}; minor=${minor%%.*}
    case $DBB_TYPE in
        postgres)
            scratch_image=${SCRATCH_IMAGE_POSTGRES:-postgres:$major}
            conn=(--host scratch-postgres --username postgres --password-env SCRATCH_PASSWORD) ;;
        mariadb)
            scratch_image=${SCRATCH_IMAGE_MARIADB:-mariadb:$major.$minor}
            conn=(--host scratch-mariadb --username root --password-env SCRATCH_PASSWORD) ;;
        mongodb)
            scratch_image=${SCRATCH_IMAGE_MONGODB:-mongo:$major.$minor}
            conn=(--uri-env SCRATCH_MONGO_URI) ;;
        *) die "$target: unknown server type '$DBB_TYPE'" ;;
    esac
    echo "    set $DBB_SET ($DBB_TYPE $DBB_SERVER_VERSION), scratch server $scratch_image"

    # Some images' first-boot initialization (notably MariaDB's, on slow or busy
    # hosts) can overrun their entrypoint's own startup limit, so try a few times.
    export SCRATCH_IMAGE=$scratch_image
    started=false
    for attempt in 1 2 3; do
        if "${dc[@]}" --profile "$DBB_TYPE" up -d --quiet-pull --wait "scratch-$DBB_TYPE" >/dev/null 2>&1; then
            started=true
            break
        fi
        echo "    scratch server didn't start (attempt $attempt of 3)" >&2
        if ((attempt == 3)); then
            echo "    its log:" >&2
            "${dc[@]}" --profile "$DBB_TYPE" logs --tail 30 "scratch-$DBB_TYPE" >&2 || true
        fi
        "${dc[@]}" --profile '*' down -v >/dev/null 2>&1 || true
    done
    if ! $started; then
        results+=("$target|$DBB_SET|$scratch_image|ERROR (scratch server didn't start)|-")
        failed=1
        cleanup
        continue
    fi

    log=$(mktemp)
    start=$SECONDS
    if "${dc[@]}" run --rm --no-deps -T restore-test restore-test "/backups/$target/$DBB_SET" "${conn[@]}" 2>&1 | tee "$log"; then
        verdict=PASSED
    else
        verdict=FAILED
        failed=1
    fi
    restore_time=$(grep -o 'restored in [0-9.]*s' "$log" | tail -1 | cut -d' ' -f3 || true)
    rm -f "$log"
    results+=("$target|$DBB_SET|$scratch_image|$verdict|${restore_time:-$((SECONDS - start))s total}")

    if $keep; then
        echo "    kept: SCRATCH_IMAGE=$scratch_image docker compose -p $project -f $compose_file --profile '*' down -v   # to remove it"
        current_project=""
    else
        cleanup
        current_project=""
    fi
done

echo
printf '%-24s %-17s %-16s %-36s %s\n' TARGET SET SCRATCH RESULT RESTORE
for row in "${results[@]}"; do
    IFS='|' read -r t s i r d <<<"$row"
    printf '%-24s %-17s %-16s %-36s %s\n' "$t" "$s" "$i" "$r" "$d"
done
exit $failed
