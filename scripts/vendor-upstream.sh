#!/usr/bin/env bash
# 把 TelegramForwarder 拉到 vendor/ 作为参考代码（不参与运行）。
#
# 为什么留着：它的 Telethon 用法、过滤器链、RSS 条目生成是有价值的参照，
# 将来若想接它的 TG 内交互菜单也从这儿看。vendor/ 已 gitignore。
set -euo pipefail

REPO="https://github.com/Heavrnl/TelegramForwarder.git"
DIR="$(cd "$(dirname "$0")/.." && pwd)/vendor/TelegramForwarder"

if [ -d "$DIR/.git" ]; then
	echo "已存在，拉取更新: $DIR"
	git -C "$DIR" pull --ff-only
else
	mkdir -p "$(dirname "$DIR")"
	git clone --depth 1 "$REPO" "$DIR"
fi
echo "参考代码在 $DIR"
