#!/usr/bin/env bash
# tgmon 一键部署 / 更新（生产，跑 ghcr 预构建镜像）。
#
# 全新机器首次部署：
#   git clone https://github.com/znjhahaha/tgmon.git /opt/tgmon
#   cd /opt/tgmon && ./deploy.sh
#
# 以后更新版本：
#   cd /opt/tgmon && ./deploy.sh update
#
# 私有仓库/私有镜像：拉取前 export GH_PAT=<GitHub PAT，read:packages 权限>。
# 公开仓库则无需。
set -euo pipefail

cd "$(dirname "$0")"
MODE="${1:-install}"          # install | update
COMPOSE="docker compose -f docker-compose.prod.yml"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少 $1，先安装它"; exit 1; }; }
need docker
docker compose version >/dev/null 2>&1 || { echo "需要 docker compose v2"; exit 1; }

# ---- 1. ghcr 登录（私有镜像需要） ----
if [ -n "${GH_PAT:-}" ]; then
  echo "$GH_PAT" | docker login ghcr.io -u znjhahaha --password-stdin
fi

# ---- 2. 首次部署：准备 .env ----
if [ "$MODE" = "install" ]; then
  if [ ! -f .env ]; then
    cp .env.example .env
    # 随机生成一个后台密码，省得用户自己想；部署完可在网页里改
    PASS=$(tr -dc A-Za-z0-9 </dev/urandom | head -c 16 || openssl rand -hex 8)
    sed -i.bak "s|^TGMON_ADMIN_PASSWORD=.*|TGMON_ADMIN_PASSWORD=$PASS|" .env && rm -f .env.bak
    echo "=========================================================="
    echo " 已生成 .env，后台初始密码：$PASS"
    echo " （登录后请到网页改密码，然后可清空 .env 里这行）"
    echo " 记得把 TGMON_BASE_URL 和 TGMON_DOMAIN 改成实际域名"
    echo " （需要 IP 直连备援再设 TGMON_IP，见 .env 注释）"
    echo "=========================================================="
  fi
fi

# ---- 3. 拉镜像 + 起容器 ----
mkdir -p db sessions media logs caddy/data caddy/config

if [ "$MODE" = "update" ]; then
  # 一次性迁移：把旧版 Caddyfile 里写死的域名/IP 提取进 .env
  #（新版 Caddyfile 改为读 TGMON_DOMAIN / TGMON_IP 环境变量）
  if [ -f .env ] && [ -f Caddyfile ] && ! grep -q '^TGMON_DOMAIN=' .env; then
    _domain=$(grep -oE '^[a-z0-9.-]+\.[a-z]{2,}[[:space:]]*\{' Caddyfile | head -1 | tr -d ' {')
    _ip=$(grep -oE 'default_sni[[:space:]]+[0-9.]+' Caddyfile | awk '{print $2}' | head -1)
    if [ -n "$_domain" ] || [ -n "$_ip" ]; then
      echo "" >> .env
      if [ -n "$_domain" ]; then echo "TGMON_DOMAIN=$_domain" >> .env; fi
      if [ -n "$_ip" ]; then echo "TGMON_IP=$_ip" >> .env; fi
      echo "=== 已把旧 Caddyfile 的站点地址迁移进 .env（TGMON_DOMAIN / TGMON_IP） ==="
    fi
  fi
  echo "=== 拉取最新代码与镜像 ==="
  git pull --ff-only
fi

echo "=== 拉取镜像（首次约 1 GB：含 embedding 权重） ==="
$COMPOSE pull

echo "=== 启动 ==="
$COMPOSE up -d
sleep 8
$COMPOSE ps

echo "=== 健康检查 ==="
for i in $(seq 1 20); do
  if docker exec tgmon-admin python -c "
import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=5)" \
      </dev/null 2>/dev/null; then
    echo "admin 已就绪 ✓  打开 https://<你的地址> 登录后台配置"
    exit 0
  fi
  sleep 3
done
echo "admin 还没就绪，看日志：$COMPOSE logs admin --tail 50"
exit 1
