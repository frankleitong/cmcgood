import logging
import os
import secrets
from datetime import timedelta

from flask import Flask, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

from . import config as config_mod
from . import db


def _ensure_secret_key(app):
    if app.config['SECRET_KEY']:
        return
    # 未配置时生成一个并持久化到数据目录，保证多 worker / 重启后会话不失效
    path = os.path.join(app.config['DATA_DIR'], 'secret_key')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    with open(path) as f:
        app.config['SECRET_KEY'] = f.read().strip()


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.update(config_mod.load())
    if overrides:
        app.config.update(overrides)
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

    os.makedirs(app.config['DATA_DIR'], exist_ok=True)
    _ensure_secret_key(app)

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    # 部署在 nginx 后面：信任一层代理的 X-Forwarded-* 头（含子路径前缀 X-Forwarded-Prefix）
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    db.init_app(app)

    from . import admin, applications, auth, cli, filters, repos, scans
    auth.init_app(app)
    filters.init_app(app)
    cli.init_app(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(repos.bp)
    app.register_blueprint(applications.bp)
    app.register_blueprint(scans.bp)
    app.register_blueprint(admin.bp)

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def http_error(e):
        return render_template('error.html', error=e), e.code

    return app
