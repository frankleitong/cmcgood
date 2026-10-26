import logging
import threading

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request,
                   url_for)

from . import jobs
from .auth import role_required
from .db import connect, get_db, log_event, query, query_one
from .util import now_iso

bp = Blueprint('scans', __name__)
log = logging.getLogger('cmcgood')


def _latest(kind, finished=False):
    sql = 'SELECT * FROM scan_runs WHERE kind = ?'
    if finished:
        sql += " AND status != 'running'"
    return query_one(sql + ' ORDER BY id DESC LIMIT 1', kind)


@bp.route('/scans')
@role_required('approver')
def index():
    run_id = request.args.get('run', type=int)
    sync_run = query_one("SELECT * FROM scan_runs WHERE id = ? AND kind = 'sync'", run_id) if run_id \
        else _latest('sync', finished=True)
    findings = []
    if sync_run:
        findings = query('SELECT * FROM scan_findings WHERE run_id = ? '
                         'ORDER BY resolved_at IS NOT NULL, issue, platform, namespace, name', sync_run['id'])
    platforms = current_app.config['PLATFORMS']
    return render_template(
        'scans/index.html', sync_run=sync_run, findings=findings,
        activity_run=_latest('activity', finished=True),
        running={k: jobs.running_run(get_db(), k) is not None for k in ('sync', 'activity')},
        runs=query('SELECT * FROM scan_runs ORDER BY id DESC LIMIT 30'),
        ignores=query('SELECT * FROM scan_ignores ORDER BY platform, namespace, name'),
        platforms=platforms)


@bp.route('/scans/run', methods=['POST'])
@role_required('admin')
def run():
    kind = request.form.get('kind')
    if kind not in ('sync', 'activity'):
        abort(400)
    if jobs.running_run(get_db(), kind):
        flash('已有同类任务正在运行，请稍后。', 'error')
        return redirect(url_for('scans.index'))
    app = current_app._get_current_object()
    user = g.user['username']
    remind = request.form.get('remind') == '1'

    def work():
        with app.app_context():
            conn = connect(app.config['DATABASE'])
            try:
                if kind == 'sync':
                    jobs.run_sync(conn, app.config, user)
                else:
                    jobs.run_activity(conn, app.config, user, remind=remind)
            except jobs.JobBusy:
                pass
            except Exception:
                log.exception('后台任务异常')
            finally:
                conn.close()

    threading.Thread(target=work, daemon=True).start()
    flash('任务已在后台开始运行，稍后刷新本页查看结果。', 'ok')
    return redirect(url_for('scans.index'))


def _import_finding(conn, f):
    now = now_iso()
    exists = conn.execute('SELECT id FROM repos WHERE platform = ? AND namespace = ? AND name = ?',
                          (f['platform'], f['namespace'], f['name'])).fetchone()
    if exists:
        return exists['id']
    cur = conn.execute(
        '''INSERT INTO repos (platform, namespace, name, url, description, visibility, source, on_platform,
                              last_seen_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'import', 1, ?, ?, ?)''',
        (f['platform'], f['namespace'], f['name'], f['url'], f['description'], f['visibility'], now, now, now))
    log_event(conn, 'create', '从扫描结果导入', repo_id=cur.lastrowid, user_id=g.user['id'])
    return cur.lastrowid


def _resolve(conn, fid, resolution, repo_id=None):
    conn.execute('UPDATE scan_findings SET resolved_at = ?, resolution = ?, repo_id = COALESCE(?, repo_id) '
                 'WHERE id = ?', (now_iso(), resolution, repo_id, fid))


@bp.route('/scans/findings/<int:fid>', methods=['POST'])
@role_required('admin')
def resolve(fid):
    f = query_one('SELECT * FROM scan_findings WHERE id = ?', fid)
    if f is None:
        abort(404)
    action = request.form.get('action')
    conn = get_db()
    if action == 'import' and f['issue'] == 'unregistered':
        repo_id = _import_finding(conn, f)
        _resolve(conn, fid, '已导入', repo_id)
        flash('已导入登记表，请补充负责人和团队信息。', 'ok')
    elif action == 'ignore' and f['issue'] == 'unregistered':
        conn.execute('INSERT OR REPLACE INTO scan_ignores (platform, namespace, name, reason, created_by, created_at) '
                     'VALUES (?, ?, ?, ?, ?, ?)',
                     (f['platform'], f['namespace'], f['name'], request.form.get('reason', '').strip(),
                      g.user['username'], now_iso()))
        _resolve(conn, fid, '已忽略')
        flash('已加入忽略列表，以后扫描不再提示。', 'ok')
    elif action == 'archive' and f['issue'] == 'platform_archived' and f['repo_id']:
        now = now_iso()
        conn.execute("UPDATE repos SET status = 'archived', archived_at = ?, updated_at = ? WHERE id = ?",
                     (now, now, f['repo_id']))
        log_event(conn, 'archive', '平台上已归档，同步登记状态', repo_id=f['repo_id'], user_id=g.user['id'])
        _resolve(conn, fid, '已同步为归档')
        flash('已同步为归档。', 'ok')
    elif action == 'dismiss':
        _resolve(conn, fid, '已知悉')
    else:
        abort(400)
    conn.commit()
    return redirect(url_for('scans.index', run=f['run_id']))


@bp.route('/scans/<int:run_id>/import-all', methods=['POST'])
@role_required('admin')
def import_all(run_id):
    conn = get_db()
    rows = conn.execute("SELECT * FROM scan_findings WHERE run_id = ? AND issue = 'unregistered' "
                        "AND resolved_at IS NULL", (run_id,)).fetchall()
    for f in rows:
        _resolve(conn, f['id'], '已导入', _import_finding(conn, f))
    conn.commit()
    flash('已导入 {} 个仓库，请尽快补充负责人。'.format(len(rows)), 'ok')
    return redirect(url_for('scans.index', run=run_id))


@bp.route('/scans/ignores/remove', methods=['POST'])
@role_required('admin')
def unignore():
    conn = get_db()
    conn.execute('DELETE FROM scan_ignores WHERE platform = ? AND namespace = ? AND name = ?',
                 (request.form.get('platform'), request.form.get('namespace'), request.form.get('name')))
    conn.commit()
    flash('已取消忽略，下次扫描会重新检查。', 'ok')
    return redirect(url_for('scans.index'))
