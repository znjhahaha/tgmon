#!/usr/bin/env bash
# 备份换机器必须搬走的东西：DB + secret.key + sessions。
# 这三样都不在 git 里。丢了 secret.key 已存的密钥解不开，丢了 sessions 要重新登录。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/tgmon-state-$(date +%Y%m%d-%H%M).tar.gz}"

cd "$ROOT"
FILES=()
[ -f secret.key ] && FILES+=(secret.key)
[ -d sessions ] && FILES+=(sessions)
[ -f db/tgmon.db ] && FILES+=(db/tgmon.db)
[ -f .env ] && FILES+=(.env)

if [ ${#FILES[@]} -eq 0 ]; then
	echo "没找到可备份的东西（secret.key / sessions / db / .env 都不存在）" >&2
	exit 1
fi

# SQLite 在 WAL 模式下直接复制主文件可能丢最近写入，先做一次 checkpoint
if [ -f db/tgmon.db ] && command -v sqlite3 >/dev/null 2>&1; then
	sqlite3 db/tgmon.db "PRAGMA wal_checkpoint(TRUNCATE);" || true
fi

tar czf "$OUT" "${FILES[@]}"
chmod 600 "$OUT"
echo "已备份到 $OUT"
echo "内含：${FILES[*]}"
echo
echo "注意：这个包里有凭据和 session，等于账号访问权。别传到不可信的地方。"
