"""配置加载。

优先级：进程环境变量 > 配置文件。配置文件按以下顺序查找：
  1. 环境变量 CMCGOOD_CONFIG 指定的路径
  2. /etc/cmcgood/cmcgood.env（服务器部署）
  3. <项目目录>/instance/cmcgood.env（本地开发）
"""
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_env_file(path):
    data = {}
    if not path or not os.path.isfile(path):
        return data
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                value = value[1:-1]
            data[key] = value
    return data


def config_path():
    path = os.environ.get('CMCGOOD_CONFIG')
    if path:
        return path
    for cand in ('/etc/cmcgood/cmcgood.env',
                 os.path.join(PROJECT_DIR, 'instance', 'cmcgood.env')):
        if os.path.isfile(cand):
            return cand
    return None


def _bool(value):
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _list(value):
    return [x.strip() for x in str(value or '').split(',') if x.strip()]


def load():
    path = config_path()
    # 写回 os.environ，使 HTTPS_PROXY 等变量对 requests 同样生效
    for key, value in read_env_file(path).items():
        os.environ.setdefault(key, value)
    env = os.environ.get

    data_dir = env('DATA_DIR') or os.path.join(PROJECT_DIR, 'instance')
    return {
        'CONFIG_PATH': path,
        'SECRET_KEY': env('SECRET_KEY', ''),
        'DATA_DIR': data_dir,
        'DATABASE': env('DATABASE') or os.path.join(data_dir, 'cmcgood.db'),
        'SITE_NAME': env('SITE_NAME', '开发部代码仓管理'),
        'SITE_URL': env('SITE_URL', '').rstrip('/'),
        'ALLOW_REGISTER': _bool(env('ALLOW_REGISTER', '1')),
        'SESSION_COOKIE_SECURE': _bool(env('SESSION_COOKIE_SECURE', '0')),
        'TZ_OFFSET_HOURS': float(env('TZ_OFFSET_HOURS', '8')),
        'PLATFORMS': {
            'github': {
                'api': env('GITHUB_API', 'https://api.github.com').rstrip('/'),
                'web': env('GITHUB_WEB', 'https://github.com').rstrip('/'),
                'token': env('GITHUB_TOKEN', ''),
                'namespaces': _list(env('GITHUB_NAMESPACES', '')),
            },
            'gitcode': {
                'api': env('GITCODE_API', 'https://api.gitcode.com/api/v5').rstrip('/'),
                'web': env('GITCODE_WEB', 'https://gitcode.com').rstrip('/'),
                'token': env('GITCODE_TOKEN', ''),
                'namespaces': _list(env('GITCODE_NAMESPACES', '')),
            },
        },
        'AUTO_CREATE_REPO': _bool(env('AUTO_CREATE_REPO', '0')),
        'INACTIVE_DAYS': int(env('INACTIVE_DAYS', '180')),
        'ACTIVITY_WINDOW_DAYS': int(env('ACTIVITY_WINDOW_DAYS', '90')),
        'REMIND_INTERVAL_DAYS': int(env('REMIND_INTERVAL_DAYS', '14')),
        'SNOOZE_DAYS': int(env('SNOOZE_DAYS', '90')),
        'SMTP_HOST': env('SMTP_HOST', ''),
        'SMTP_PORT': int(env('SMTP_PORT', '465')),
        'SMTP_USER': env('SMTP_USER', ''),
        'SMTP_PASSWORD': env('SMTP_PASSWORD', ''),
        'SMTP_SECURITY': env('SMTP_SECURITY', 'ssl').lower(),
        'MAIL_FROM': env('MAIL_FROM', ''),
        'WEBHOOK_URL': env('WEBHOOK_URL', ''),
        'WEBHOOK_TYPE': env('WEBHOOK_TYPE', 'wecom').lower(),
    }


def parse_namespace(raw):
    """'user:alice' -> ('user', 'alice')；'my-org' -> ('org', 'my-org')"""
    if raw.lower().startswith('user:'):
        return 'user', raw[5:].strip()
    return 'org', raw.strip()


def namespace_kind(cfg, platform, namespace):
    for raw in cfg['PLATFORMS'].get(platform, {}).get('namespaces', []):
        kind, ns = parse_namespace(raw)
        if ns.lower() == namespace.lower():
            return kind
    return 'org'
