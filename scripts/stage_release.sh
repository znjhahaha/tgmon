#!/usr/bin/env bash
# Build and validate a release while the current containers keep running.
set -euo pipefail
release="${1:?release tag required}"
case "$release" in *[!a-z0-9-]*|'') exit 2 ;; esac
root=/opt/tgmon
stage="$root/releases/$release"
test "$(realpath "$stage")" = "$stage"
cd "$stage"

python3 - <<'PY'
import hashlib, json
from pathlib import Path
manifest = json.loads(Path('release-manifest.json').read_text())
for name, digest in manifest['sha256'].items():
    assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest, name
print('Manifest verified:', len(manifest['sha256']), 'files')
PY

docker build --progress=plain -t "tgmon:$release" . > build.log 2>&1 || {
    tail -n 50 build.log
    exit 1
}
printf 'Image built: '
docker image inspect "tgmon:$release" --format '{{.Id}}'

docker exec -i tgmon-admin python - "/app/db/backups/$release-preflight.sqlite3" \
    < scripts/backup_database.py > backup-preflight.json
mkdir -p preflight-db
cp "$root/db/backups/$release-preflight.sqlite3" preflight-db/tgmon.db
docker run --rm -i --network none --env-file "$root/.env" \
    -e TGMON_DB=/app/db/tgmon.db -v "$stage/preflight-db:/app/db" \
    --entrypoint python "tgmon:$release" - preflight \
    < scripts/verify_upgrade.py > preflight.json 2> preflight.log || {
    tail -n 60 preflight.log
    exit 1
}
touch preflight-ok
cat preflight.json
