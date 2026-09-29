#!/bin/bash
# 在 NAS(Ubuntu) 上执行：用 Docker 部署 PostgreSQL 并创建基金信号专用库
# 用法: bash deploy_nas.sh   (可先设置环境变量覆盖默认值)
#   PG_PASSWORD=你的密码 bash deploy_nas.sh
set -euo pipefail

PG_PASSWORD="${PG_PASSWORD:-change-this-database-password}"
PG_USER="fund_signal"
PG_DB="fund_signal"
PG_PORT="${PG_PORT:-5432}"
DATA_DIR="/srv/fund-signal-postgres"
CONTAINER="fund-signal-postgres"

if ! command -v docker >/dev/null 2>&1; then
  echo "未检测到 Docker，先安装:"
  echo "  sudo apt update && sudo apt install -y docker.io docker-compose-v2"
  echo "  sudo systemctl enable --now docker"
  exit 1
fi

if sudo docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "已存在容器 $CONTAINER，跳过创建"
  echo "  sudo docker logs $CONTAINER   # 查看日志"
else
  sudo mkdir -p "$DATA_DIR"
  sudo docker run -d --name "$CONTAINER" --restart unless-stopped \
    -e POSTGRES_USER="$PG_USER" \
    -e POSTGRES_PASSWORD="$PG_PASSWORD" \
    -e POSTGRES_DB="$PG_DB" \
    -p "$PG_PORT:5432" \
    -v "$DATA_DIR:/var/lib/postgresql/data" \
    postgres:16-alpine
  echo "容器已创建，等待初始化..."
  sleep 8
fi

echo ""
echo "========== PostgreSQL 部署完成 =========="
echo "  地址: <NAS 内网IP>:$PG_PORT"
echo "  用户: $PG_USER"
echo "  密码: $PG_PASSWORD"
echo "  数据库: $PG_DB"
echo "  数据目录: $DATA_DIR (NAS 上持久化，容器重建不丢数据)"
echo "=========================================="
echo ""
echo "下一步(在本机 Mac 上):"
echo "  1. 打开后台 http://127.0.0.1:8787 → 数据库设置"
echo "  2. 填上面信息，点【保存并连接】即可从 NAS 库读写"
echo ""
echo "防火墙注意: 若 NAS 启用了 ufw，放行内网访问:"
echo "  sudo ufw allow from 192.168.0.0/16 to any port $PG_PORT proto tcp"
echo "容器状态: sudo docker ps"
