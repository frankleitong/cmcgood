import re
from datetime import datetime, timedelta, timezone

# 时间统一以 UTC ISO 字符串存库（'2024-01-31T08:00:00Z'），可直接按字符串比较大小


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt):
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def now_iso():
    return to_iso(utcnow())


def iso_days_ago(days):
    return to_iso(utcnow() - timedelta(days=days))


def iso_days_later(days):
    return to_iso(utcnow() + timedelta(days=days))


def iso_hours_ago(hours):
    return to_iso(utcnow() - timedelta(hours=hours))


_FRACTION_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$')


def parse_time(value):
    """解析各平台返回的 ISO 时间，失败返回 None。"""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace(' ', 'T', 1)
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            m = _FRACTION_RE.match(s)
            if not m:
                return None
            try:
                dt = datetime.fromisoformat(m.group(1) + m.group(3))
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def days_since(value):
    dt = parse_time(value)
    if dt is None:
        return None
    return (utcnow() - dt).days


def repo_key(platform, namespace, name):
    return '{}:{}/{}'.format(platform, namespace, name).lower()
