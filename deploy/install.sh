#!/usr/bin/env bash
# 安装 / 重新安装（幂等，可重复执行）。用 root 运行：
#   sudo bash deploy/install.sh
# 可通过环境变量覆盖：APP_USER DATA_DIR BIND PYTHON
set -euo pipefail

APP_DIR=$(cd "$(dirname "$0")/.." && pwd)
APP_USER=${APP_USER:-cmcgood}
CONF_DIR=/etc/cmcgood
CONF_FILE=$CONF_DIR/cmcgood.env
DATA_DIR=${DATA_DIR:-/var/lib/cmcgood}
BIND=${BIND:-127.0.0.1:8000}
PYTHON=${PYTHON:-python3}
cd "$APP_DIR"

as_app() { runuser -u "$APP_USER" -- "$@"; }

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行：sudo bash $0"; exit 1; }
command -v systemctl >/dev/null || { echo "需要 systemd"; exit 1; }

echo "==> 检查 Python"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 8)' 2>/dev/null \
  || { echo "需要 Python 3.8 及以上（当前：$("$PYTHON" -V 2>&1)）"; exit 1; }
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null \
  || { echo "缺少 venv 模块。Debian/Ubuntu: apt install python3-venv；CentOS/RHEL: yum install python3"; exit 1; }

echo "==> 系统用户 $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell "$(command -v nologin || echo /sbin/nologin)" "$APP_USER"
fi

echo "==> 目录"
install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR" "$DATA_DIR/backups"
install -d -m 755 "$CONF_DIR"
as_app test -r "$APP_DIR/wsgi.py" \
  || { echo "用户 $APP_USER 无法读取 $APP_DIR，请把代码放到 /opt/cmcgood 之类的公共目录"; exit 1; }

echo "==> Python 虚拟环境与依赖"
[ -x "$APP_DIR/venv/bin/python" ] || "$PYTHON" -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> 配置文件 $CONF_FILE"
if [ ! -f "$CONF_FILE" ]; then
  SECRET=$("$PYTHON" -c 'import secrets; print(secrets.token_hex(32))')
  sed -e "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET|" -e "s|^DATA_DIR=.*|DATA_DIR=$DATA_DIR|" \
    "$APP_DIR/config.example.env" > "$CONF_FILE"
  echo "    已生成，请按需填写 token / 扫描范围 / 邮件等配置"
else
  echo "    已存在，保持不变"
fi
chown root:"$APP_USER" "$CONF_FILE"
chmod 640 "$CONF_FILE"

echo "==> 数据库"
as_app env CMCGOOD_CONFIG="$CONF_FILE" "$APP_DIR/venv/bin/flask" --app wsgi init-db

echo "==> systemd 服务与定时器"
for f in "$APP_DIR"/deploy/systemd/*.in; do
  sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@APP_USER@|$APP_USER|g" \
      -e "s|@CONF_FILE@|$CONF_FILE|g" -e "s|@BIND@|$BIND|g" \
      "$f" > "/etc/systemd/system/$(basename "${f%.in}")"
done
systemctl daemon-reload
systemctl enable -q cmcgood.service cmcgood-sync.timer cmcgood-activity.timer cmcgood-backup.timer
systemctl restart cmcgood.service
systemctl restart cmcgood-sync.timer cmcgood-activity.timer cmcgood-backup.timer

echo "==> 检查服务"
for i in 1 2 3 4 5 6 7 8 9 10; do
  if "$APP_DIR/venv/bin/python" -c "import urllib.request; urllib.request.urlopen('http://$BIND/login', timeout=3)" 2>/dev/null; then
    echo "    服务已启动：http://$BIND"
    break
  fi
  if [ "$i" -eq 10 ]; then
    echo "    服务未正常启动，查看日志：journalctl -u cmcgood -n 50 --no-pager"
    exit 1
  fi
  sleep 1
done

cat <<TIP

安装完成。接下来：
  1. 创建管理员：      sudo bash $APP_DIR/deploy/manage.sh create-admin <用户名> --email <邮箱>
  2. 编辑配置：        sudo vi $CONF_FILE   （改完执行 sudo systemctl restart cmcgood）
  3. 配置 nginx：      参考 $APP_DIR/deploy/nginx/ 下的示例
  4. 查看定时任务：    systemctl list-timers 'cmcgood*'
TIP
