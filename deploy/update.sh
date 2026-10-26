#!/usr/bin/env bash
# 升级到远端最新代码：sudo bash deploy/update.sh
set -euo pipefail
APP_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$APP_DIR"
git pull --ff-only
exec bash "$APP_DIR/deploy/install.sh"
