import json
from datetime import timedelta

from flask import current_app

from .util import days_since, parse_time


def _local(value):
    dt = parse_time(value)
    if dt is None:
        return None
    return dt + timedelta(hours=current_app.config['TZ_OFFSET_HOURS'])


def fmt_time(value):
    dt = _local(value)
    return dt.strftime('%Y-%m-%d %H:%M') if dt else '—'


def fmt_date(value):
    dt = _local(value)
    return dt.strftime('%Y-%m-%d') if dt else '—'


def ago(value):
    days = days_since(value)
    if days is None:
        return '—'
    if days <= 0:
        return '今天'
    if days < 60:
        return '{} 天前'.format(days)
    if days < 730:
        return '{} 个月前'.format(days // 30)
    return '{} 年前'.format(days // 365)


def from_json(value):
    try:
        return json.loads(value or '{}')
    except ValueError:
        return {}


def init_app(app):
    app.jinja_env.filters.update(fmt_time=fmt_time, fmt_date=fmt_date, ago=ago,
                                 days_since=days_since, from_json=from_json)
