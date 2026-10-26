"""定时任务：登记一致性扫描（sync）与活跃度检查（activity）。

既可以由 systemd timer 通过 CLI 调用，也可以在网页上手动触发（后台线程）。
函数显式接收 sqlite 连接和配置，不依赖请求上下文。
"""
import json
import logging
from collections import OrderedDict

from . import constants as C
from . import notify
from .config import parse_namespace
from .db import log_event
from .platforms import PlatformError, get_client
from .util import days_since, iso_days_ago, iso_hours_ago, now_iso, repo_key

log = logging.getLogger('cmcgood')


class JobBusy(Exception):
    pass


def running_run(conn, kind):
    # 超过 2 小时仍为 running 的视为进程异常退出遗留，不再阻塞
    return conn.execute("SELECT id FROM scan_runs WHERE kind = ? AND status = 'running' AND started_at > ?",
                        (kind, iso_hours_ago(2))).fetchone()


def _start_run(conn, kind, triggered_by):
    if running_run(conn, kind):
        raise JobBusy('已有同类任务正在运行')
    cur = conn.execute('INSERT INTO scan_runs (kind, status, triggered_by, started_at) VALUES (?, ?, ?, ?)',
                       (kind, 'running', triggered_by, now_iso()))
    conn.commit()
    return cur.lastrowid


def _finish_run(conn, run_id, summary, error=''):
    if error:
        status = 'failed'
    elif summary.get('errors'):
        status = 'partial'
    else:
        status = 'ok'
    conn.execute('UPDATE scan_runs SET status = ?, finished_at = ?, summary = ?, error = ? WHERE id = ?',
                 (status, now_iso(), json.dumps(summary, ensure_ascii=False), error, run_id))
    conn.commit()
    return status


def _add_finding(conn, run_id, issue, platform, namespace, name, url='', description='', repo_id=None,
                 visibility='private'):
    conn.execute('INSERT INTO scan_findings (run_id, issue, platform, namespace, name, url, description, '
                 'visibility, repo_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                 (run_id, issue, platform, namespace, name, url, description, visibility, repo_id))


# ---------------------------------------------------------------- sync

def run_sync(conn, cfg, triggered_by='system'):
    """对比登记表与平台上的实际仓库，返回 (run_id, status)。"""
    run_id = _start_run(conn, 'sync', triggered_by)
    summary = {'remote': 0, 'matched': 0, 'unregistered': 0, 'missing': 0, 'platform_archived': 0,
               'scanned_namespaces': [], 'errors': []}
    try:
        now = now_iso()
        local = {repo_key(r['platform'], r['namespace'], r['name']): r
                 for r in conn.execute('SELECT * FROM repos').fetchall()}
        ignores = {repo_key(r['platform'], r['namespace'], r['name'])
                   for r in conn.execute('SELECT * FROM scan_ignores').fetchall()}

        # 1. 拉取配置范围内（组织/用户）的全部仓库
        remote, scanned = {}, set()
        for platform, pcfg in cfg['PLATFORMS'].items():
            if not pcfg['namespaces']:
                continue
            client = get_client(cfg, platform)
            for raw in pcfg['namespaces']:
                _, ns = parse_namespace(raw)
                try:
                    items = client.list_repos(raw)
                except PlatformError as e:
                    summary['errors'].append(str(e))
                    continue
                scanned.add((platform, ns.lower()))
                summary['scanned_namespaces'].append('{}:{} ({})'.format(platform, ns, len(items)))
                for it in items:
                    it['platform'] = platform
                    it['namespace'] = it['namespace'] or ns
                    remote[repo_key(platform, it['namespace'], it['name'])] = it
        summary['remote'] = len(remote)

        # 2. 平台上有的：匹配 / 未登记
        for key, it in remote.items():
            row = local.get(key)
            if row is None:
                if key not in ignores:
                    _add_finding(conn, run_id, 'unregistered', it['platform'], it['namespace'], it['name'],
                                 it['url'], it['description'],
                                 visibility='private' if it['private'] else 'public')
                    summary['unregistered'] += 1
                continue
            summary['matched'] += 1
            conn.execute("UPDATE repos SET on_platform = 1, last_seen_at = ?, "
                         "url = CASE WHEN url = '' THEN ? ELSE url END WHERE id = ?",
                         (now, it['url'], row['id']))
            if it['archived'] and row['status'] != 'archived':
                _add_finding(conn, run_id, 'platform_archived', row['platform'], row['namespace'], row['name'],
                             row['url'] or it['url'], row['description'], row['id'])
                summary['platform_archived'] += 1

        # 3. 登记了但平台上没找到；不在扫描范围内的登记仓逐个查询
        for key, row in local.items():
            if key in remote:
                continue
            if (row['platform'], row['namespace'].lower()) not in scanned:
                if row['status'] == 'archived':
                    continue
                client = get_client(cfg, row['platform'])
                if client is None:
                    continue
                try:
                    info = client.get_repo(row['namespace'], row['name'])
                except PlatformError as e:
                    summary['errors'].append(str(e))
                    continue
                if info is not None:
                    summary['matched'] += 1
                    conn.execute("UPDATE repos SET on_platform = 1, last_seen_at = ?, "
                                 "url = CASE WHEN url = '' THEN ? ELSE url END WHERE id = ?",
                                 (now, info['url'], row['id']))
                    continue
            conn.execute('UPDATE repos SET on_platform = 0 WHERE id = ?', (row['id'],))
            if row['status'] != 'archived':
                _add_finding(conn, run_id, 'missing', row['platform'], row['namespace'], row['name'],
                             row['url'], row['description'], row['id'])
                summary['missing'] += 1

        conn.commit()
        status = _finish_run(conn, run_id, summary)
    except Exception as e:
        conn.rollback()
        log.exception('登记一致性扫描失败')
        status = _finish_run(conn, run_id, summary, error=str(e))
    log.info('sync run #%s %s: %s', run_id, status, summary)
    return run_id, status


# ---------------------------------------------------------------- activity

def run_activity(conn, cfg, triggered_by='system', remind=True):
    """检查每个登记仓默认分支的最近合入时间，更新活跃状态并发送归档提醒。"""
    run_id = _start_run(conn, 'activity', triggered_by)
    summary = {'checked': 0, 'active': 0, 'inactive': 0, 'became_inactive': 0, 'became_active': 0,
               'not_found': 0, 'reminded': 0, 'errors': []}
    try:
        since = iso_days_ago(cfg['ACTIVITY_WINDOW_DAYS'])
        threshold = iso_days_ago(cfg['INACTIVE_DAYS'])
        rows = conn.execute("SELECT * FROM repos WHERE status IN ('active', 'inactive') "
                            "AND (on_platform IS NULL OR on_platform = 1)").fetchall()
        for r in rows:
            client = get_client(cfg, r['platform'])
            if client is None:
                continue
            try:
                last, recent = client.latest_activity(r['namespace'], r['name'], since)
            except PlatformError as e:
                if e.status == 404:
                    conn.execute('UPDATE repos SET on_platform = 0 WHERE id = ?', (r['id'],))
                    summary['not_found'] += 1
                else:
                    summary['errors'].append(str(e))
                continue
            summary['checked'] += 1
            # 从未有过提交的仓库按登记时间算
            reference = last or r['created_at']
            new_status = 'inactive' if reference < threshold else 'active'
            summary[new_status] += 1
            conn.execute('UPDATE repos SET last_commit_at = ?, recent_commits = ?, activity_checked_at = ?, '
                         'status = ?, on_platform = 1, updated_at = ? WHERE id = ?',
                         (last, recent, now_iso(), new_status, now_iso(), r['id']))
            if new_status != r['status']:
                summary['became_' + new_status] += 1
                log_event(conn, 'status', '活跃度检查：{} → {}（最近合入：{}）'.format(
                    C.REPO_STATUS[r['status']][0], C.REPO_STATUS[new_status][0],
                    last[:10] if last else '无'), repo_id=r['id'])
        conn.commit()
        if remind:
            summary['reminded'] = send_reminders(conn, cfg)
        status = _finish_run(conn, run_id, summary)
    except Exception as e:
        conn.rollback()
        log.exception('活跃度检查失败')
        status = _finish_run(conn, run_id, summary, error=str(e))
    log.info('activity run #%s %s: %s', run_id, status, summary)
    return run_id, status


def send_reminders(conn, cfg):
    """给不活跃仓的负责人发催促归档提醒；同一仓库两次提醒至少间隔 REMIND_INTERVAL_DAYS 天。"""
    now = now_iso()
    rows = conn.execute(
        """SELECT r.*, u.email AS owner_email, COALESCE(NULLIF(u.display_name, ''), u.username) AS owner_name
           FROM repos r LEFT JOIN users u ON u.id = r.owner_id AND u.active = 1
           WHERE r.status = 'inactive'
             AND (r.snooze_until IS NULL OR r.snooze_until < ?)
             AND (r.last_reminded_at IS NULL OR r.last_reminded_at < ?)
             AND NOT EXISTS (SELECT 1 FROM applications a
                             WHERE a.repo_id = r.id AND a.kind = 'archive' AND a.status = 'pending')
           ORDER BY r.owner_id, r.platform, r.namespace, r.name""",
        (now, iso_days_ago(cfg['REMIND_INTERVAL_DAYS']))).fetchall()
    if not rows:
        return 0

    def line(r):
        days = days_since(r['last_commit_at'])
        last = '{}（{} 天前）'.format(r['last_commit_at'][:10], days) if r['last_commit_at'] else '从未有提交'
        url = notify.link(cfg, '/repos/{}'.format(r['id']))
        return '- {}:{}/{}  最近合入：{}  {}'.format(r['platform'], r['namespace'], r['name'], last, url).rstrip()

    groups = OrderedDict()
    for r in rows:
        groups.setdefault((r['owner_email'] or '', r['owner_name'] or ''), []).append(r)

    admin_emails = [x['email'] for x in conn.execute(
        "SELECT email FROM users WHERE active = 1 AND role = 'admin' AND email != ''").fetchall()]
    footer = ('\n如仍需保留，请在网站仓库详情页点击「暂不归档」并说明理由；'
              '如已不再维护，请点击「申请归档」。\n')
    for (email, name), repos in groups.items():
        body = '{}你好，\n\n以下代码仓已超过 {} 天没有新的合入，请确认是否可以归档：\n\n{}\n{}'.format(
            name + ' ' if name else '', cfg['INACTIVE_DAYS'], '\n'.join(line(r) for r in repos), footer)
        if email:
            notify.send_email(cfg, [email], '有 {} 个代码仓长期未合入，请确认是否归档'.format(len(repos)), body)
        else:
            notify.send_email(cfg, admin_emails, '有 {} 个无负责人的代码仓长期未合入'.format(len(repos)),
                              '以下代码仓没有指定负责人（或负责人账号已停用），请指定负责人或直接处理：\n\n'
                              + '\n'.join(line(r) for r in repos) + '\n')

    notify.send_webhook(cfg, '{} 个代码仓超过 {} 天无合入，请负责人确认是否归档：\n{}'.format(
        len(rows), cfg['INACTIVE_DAYS'],
        '\n'.join('{} @{}'.format(line(r), r['owner_name'] or '未指定负责人') for r in rows)))

    for r in rows:
        conn.execute('UPDATE repos SET last_reminded_at = ? WHERE id = ?', (now, r['id']))
        log_event(conn, 'remind', '已发送催促归档提醒', repo_id=r['id'])
    conn.commit()
    return len(rows)
