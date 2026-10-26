"""命令行：flask --app wsgi <命令>（服务器上用 deploy/manage.sh <命令>）。"""
import glob
import os
import sqlite3
import sys
import tarfile
from datetime import datetime

import click
from werkzeug.security import generate_password_hash

from . import jobs
from .db import connect, migrate
from .util import now_iso


def init_app(app):
    def _conn():
        return connect(app.config['DATABASE'])

    @app.cli.command('init-db')
    def init_db():
        """创建 / 升级数据库结构（启动时也会自动执行）。"""
        migrate(app.config['DATABASE'])
        click.echo('数据库已就绪：' + app.config['DATABASE'])

    @app.cli.command('create-admin')
    @click.argument('username')
    @click.option('--email', default='', help='管理员邮箱')
    @click.password_option(help='密码（不填则交互输入）')
    def create_admin(username, email, password):
        """创建管理员；用户已存在时提升为管理员并重置密码。"""
        conn = _conn()
        row = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
        if row:
            conn.execute("UPDATE users SET role = 'admin', active = 1, password_hash = ?, "
                         "email = CASE WHEN ? != '' THEN ? ELSE email END WHERE id = ?",
                         (generate_password_hash(password), email, email, row['id']))
            click.echo('已将 {} 设为管理员并重置密码。'.format(username))
        else:
            conn.execute("INSERT INTO users (username, display_name, email, password_hash, role, created_at) "
                         "VALUES (?, ?, ?, ?, 'admin', ?)",
                         (username, username, email, generate_password_hash(password), now_iso()))
            click.echo('已创建管理员 {}。'.format(username))
        conn.commit()
        conn.close()

    @app.cli.command('sync')
    def sync():
        """登记一致性扫描：对比登记表与 GitHub / GitCode。"""
        conn = _conn()
        try:
            run_id, status = jobs.run_sync(conn, app.config, 'cli')
        except jobs.JobBusy as e:
            click.echo(str(e))
            sys.exit(1)
        finally:
            conn.close()
        click.echo('扫描 #{} 完成：{}'.format(run_id, status))
        sys.exit(1 if status == 'failed' else 0)

    @app.cli.command('check-activity')
    @click.option('--no-remind', is_flag=True, help='只更新活跃状态，不发提醒')
    def check_activity(no_remind):
        """活跃度检查，并给长期未合入的仓库负责人发催促归档提醒。"""
        conn = _conn()
        try:
            run_id, status = jobs.run_activity(conn, app.config, 'cli', remind=not no_remind)
        except jobs.JobBusy as e:
            click.echo(str(e))
            sys.exit(1)
        finally:
            conn.close()
        click.echo('活跃度检查 #{} 完成：{}'.format(run_id, status))
        sys.exit(1 if status == 'failed' else 0)

    @app.cli.command('backup')
    @click.option('--dest-dir', default=None, help='备份目录，默认 DATA_DIR/backups')
    @click.option('--keep', default=30, show_default=True, help='保留最近几份')
    def backup(dest_dir, keep):
        """在线备份数据库和配置文件为 tar.gz（不影响网站运行）。"""
        dest_dir = dest_dir or os.path.join(app.config['DATA_DIR'], 'backups')
        os.makedirs(dest_dir, exist_ok=True)
        tmp_db = os.path.join(dest_dir, '.cmcgood.db.tmp')
        src, out = _conn(), sqlite3.connect(tmp_db)
        with out:
            src.backup(out)
        out.close()
        src.close()

        target = os.path.join(dest_dir, 'cmcgood-{}.tar.gz'.format(datetime.now().strftime('%Y%m%d-%H%M%S')))
        with tarfile.open(target, 'w:gz') as tar:
            tar.add(tmp_db, arcname='cmcgood.db')
            conf = app.config.get('CONFIG_PATH')
            if conf and os.access(conf, os.R_OK):
                tar.add(conf, arcname='cmcgood.env')
        os.remove(tmp_db)
        os.chmod(target, 0o600)

        old = sorted(glob.glob(os.path.join(dest_dir, 'cmcgood-*.tar.gz')))[:-keep] if keep > 0 else []
        for path in old:
            os.remove(path)
        click.echo('已备份到 {}（清理旧备份 {} 份）'.format(target, len(old)))
