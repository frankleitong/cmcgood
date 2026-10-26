#!/usr/bin/env bash
# 从备份包恢复数据库和配置（换服务器时使用，先执行 install.sh）：
#   sudo bash deploy/restore.sh /path/to/cmcgood-YYYYmmdd-HHMMSS.tar.gz
set -euo pipefail
BACKUP=${1:?用法：sudo bash deploy/restore.sh <备份文件.tar.gz>}
APP_USER=${APP_USER:-cmcgood}
CONF_FILE=/etc/cmcgood/cmcgood.env

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行"; exit 1; }
id "$APP_USER" >/dev/null 2>&1 || { echo "请先执行 deploy/install.sh"; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
tar -xzf "$BACKUP" -C "$TMP"
[ -f "$TMP/cmcgood.db" ] || { echo "备份包里没有 cmcgood.db"; exit 1; }

systemctl stop cmcgood.service || true

if [ -f "$TMP/cmcgood.env" ]; then
  install -m 640 -o root -g "$APP_USER" "$TMP/cmcgood.env" "$CONF_FILE"
  echo "已恢复配置 $CONF_FILE"
fi
DATA_DIR=$(sed -n 's/^DATA_DIR=//p' "$CONF_FILE" | tail -n1)
DATA_DIR=${DATA_DIR:-/var/lib/cmcgood}
install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR" "$DATA_DIR/backups"
rm -f "$DATA_DIR/cmcgood.db-wal" "$DATA_DIR/cmcgood.db-shm"
install -m 640 -o "$APP_USER" -g "$APP_USER" "$TMP/cmcgood.db" "$DATA_DIR/cmcgood.db"
echo "已恢复数据库 $DATA_DIR/cmcgood.db"

systemctl start cmcgood.service
echo "完成。"
