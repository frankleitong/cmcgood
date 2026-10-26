#!/usr/bin/env bash
# 以服务用户身份执行管理命令，例如：
#   sudo bash deploy/manage.sh create-admin admin --email admin@example.com
#   sudo bash deploy/manage.sh sync
#   sudo bash deploy/manage.sh check-activity --no-remind
#   sudo bash deploy/manage.sh backup
#   sudo bash deploy/manage.sh --help
set -euo pipefail
APP_DIR=$(cd "$(dirname "$0")/.." && pwd)
APP_USER=${APP_USER:-cmcgood}
CONF_FILE=${CONF_FILE:-/etc/cmcgood/cmcgood.env}
cd "$APP_DIR"
CMD=(env CMCGOOD_CONFIG="$CONF_FILE" "$APP_DIR/venv/bin/flask" --app wsgi "$@")
if [ "$(id -un)" = "$APP_USER" ]; then
  exec "${CMD[@]}"
elif [ "$(id -u)" -eq 0 ]; then
  exec runuser -u "$APP_USER" -- "${CMD[@]}"
else
  exec sudo -u "$APP_USER" "${CMD[@]}"
fi
