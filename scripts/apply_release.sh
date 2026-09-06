#!/usr/bin/env bash
# Requires a successful stage_release.sh. Keep a rollback image and source tree.
set -euo pipefail
release="${1:?release tag required}"
case "$release" in *[!a-z0-9-]*|'') exit 2 ;; esac
root=/opt/tgmon
stage="$root/releases/$release"
backup="$root/backups/$release"
test "$(realpath "$stage")" = "$stage"
test -f "$stage/preflight-ok"
test ! -e "$backup"
mkdir -p "$backup"
cd "$root"
old_image="$(docker inspect tgmon-admin --format '{{.Image}}')"
docker image tag "$old_image" "tgmon:rollback-$release"
printf '%s\n' "$old_image" > "$backup/image-id"
for file in Dockerfile requirements.txt .dockerignore README.md IMPLEMENTATION_STATUS.md; do
    if test -f "$file"; then cp -p "$file" "$backup/$file"; fi
done

swapped=0
rollback() {
    trap - ERR
    set +e
    printf 'Deployment failed; restoring previous application.\n' >&2
    docker compose stop -t 30 admin worker
    if test "$swapped" = 1 && test -d "$backup/tgmon"; then
        mv "$root/tgmon" "$backup/tgmon-failed"
        mv "$backup/tgmon" "$root/tgmon"
    fi
    for file in Dockerfile requirements.txt .dockerignore README.md IMPLEMENTATION_STATUS.md; do
        if test -f "$backup/$file"; then cp -p "$backup/$file" "$root/$file"; fi
    done
    docker image tag "$old_image" tgmon:local
    docker compose up -d --no-build --force-recreate admin worker
    exit 1
}
trap rollback ERR

# Stop writers for a final consistent backup, then swap only application files.
docker compose stop -t 40 worker admin
docker run --rm -i --network none --env-file "$root/.env" \
    -e TGMON_DB=/app/db/tgmon.db -v "$root/db:/app/db" \
    --entrypoint python "tgmon:$release" - "/app/db/backups/$release-live.sqlite3" \
    < "$stage/scripts/backup_database.py" > "$backup/database.json"
mv "$root/tgmon" "$backup/tgmon"
swapped=1
cp -a "$stage/tgmon" "$root/tgmon"
for file in Dockerfile requirements.txt .dockerignore README.md IMPLEMENTATION_STATUS.md; do
    cp -p "$stage/$file" "$root/$file"
done
docker image tag "tgmon:$release" tgmon:local

# Migrate once before both processes start, preserving the existing env/secrets.
docker compose run --rm --no-deps -T admin python - <<'PY'
from tgmon.bootstrap import init_all
from tgmon import settings
init_all()
settings.set_many({'RETRIEVAL_ENABLED': True, 'EMBEDDING_ENABLED': True})
print('Schema ready; local embedding enabled.')
PY
docker compose up -d --no-build --force-recreate admin worker
healthy=0
for attempt in $(seq 1 30); do
    if docker exec tgmon-admin python -c \
        "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200" \
        >/dev/null 2>&1; then
        healthy=1
        break
    fi
    sleep 2
done
test "$healthy" = 1
new_image="$(docker image inspect "tgmon:$release" --format '{{.Id}}')"
for container in tgmon-admin tgmon-worker; do
    test "$(docker inspect "$container" --format '{{.Image}}')" = "$new_image"
    test "$(docker inspect "$container" --format '{{.State.Running}}')" = true
done
trap - ERR
printf 'Deployed %s\n' "$release"
docker ps --format '{{.Names}} {{.Status}}'
printf 'Rollback files: %s\n' "$backup"
