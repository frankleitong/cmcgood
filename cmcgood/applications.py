from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request,
                   url_for)

from . import constants as C
from . import notify
from .auth import active_users, approver_emails, has_role, login_required, role_required
from .config import namespace_kind
from .db import get_db, log_event, query, query_one
from .platforms import PlatformError, get_client, web_url
from .repos import find_registered, namespace_choices, teams, validate_repo_fields
from .util import now_iso

bp = Blueprint('applications', __name__)

APP_SQL = '''SELECT a.*,
                    ap.username AS applicant_username, ap.display_name AS applicant_name, ap.email AS applicant_email,
                    ow.username AS owner_username, ow.display_name AS owner_name,
                    av.username AS approver_username, av.display_name AS approver_name
             FROM applications a
             JOIN users ap ON ap.id = a.applicant_id
             LEFT JOIN users ow ON ow.id = a.owner_id
             LEFT JOIN users av ON av.id = a.approver_id'''


def get_app_or_404(app_id):
    a = query_one(APP_SQL + ' WHERE a.id = ?', app_id)
    if a is None:
        abort(404)
    return a


@bp.route('/apply', methods=['GET', 'POST'])
@login_required
def new():
    choices = namespace_choices()
    if request.method == 'POST':
        form = request.form
        errors = validate_repo_fields(form)
        if not form.get('description', '').strip():
            errors.append('请填写仓库用途描述。')
        if not form.get('reason', '').strip():
            errors.append('请填写申请理由。')
        if not errors and query_one(
                "SELECT 1 FROM applications WHERE kind = 'create' AND status = 'pending' "
                "AND platform = ? AND namespace = ? COLLATE NOCASE AND name = ? COLLATE NOCASE",
                form['platform'], form['namespace'].strip(), form['name'].strip()):
            errors.append('已有同名仓库的申请在审批中。')
        if errors:
            for e in errors:
                flash(e, 'error')
        else:
            conn = get_db()
            cur = conn.execute(
                '''INSERT INTO applications (kind, applicant_id, platform, namespace, name, description, team,
                                             visibility, owner_id, reason, created_at)
                   VALUES ('create', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (g.user['id'], form['platform'], form['namespace'].strip(), form['name'].strip(),
                 form['description'].strip(), form.get('team', '').strip(), form.get('visibility', 'private'),
                 form.get('owner_id') or g.user['id'], form['reason'].strip(), now_iso()))
            conn.commit()
            link = url_for('applications.detail', app_id=cur.lastrowid, _external=True)
            title = '新建仓库申请：{}:{}/{}'.format(form['platform'], form['namespace'].strip(), form['name'].strip())
            body = '{} 申请新建代码仓 {}/{}（{}）\n用途：{}\n理由：{}\n\n审批：{}'.format(
                g.user['display_name'] or g.user['username'], form['namespace'].strip(), form['name'].strip(),
                C.PLATFORMS[form['platform']], form['description'].strip(), form['reason'].strip(), link)
            notify.send_email(current_app.config, approver_emails(), title, body)
            notify.send_webhook(current_app.config, title + '\n' + body)
            flash('申请已提交，等待审批。', 'ok')
            return redirect(url_for('applications.detail', app_id=cur.lastrowid))
    else:
        form = {'platform': choices[0][0] if choices else 'github',
                'namespace': choices[0][1] if choices else '',
                'visibility': 'private', 'owner_id': str(g.user['id'])}
    return render_template('applications/new.html', form=form, users=active_users(), teams=teams(),
                           ns_choices=choices)


@bp.route('/applications')
@login_required
def index():
    is_approver = has_role(g.user, 'approver')
    tab = request.args.get('tab') or ('todo' if is_approver else 'mine')
    if tab == 'todo' and is_approver:
        apps = query(APP_SQL + " WHERE a.status = 'pending' ORDER BY a.id")
    elif tab == 'all' and is_approver:
        apps = query(APP_SQL + ' ORDER BY a.id DESC LIMIT 500')
    else:
        tab = 'mine'
        apps = query(APP_SQL + ' WHERE a.applicant_id = ? ORDER BY a.id DESC', g.user['id'])
    return render_template('applications/list.html', apps=apps, tab=tab, is_approver=is_approver)


@bp.route('/applications/<int:app_id>')
@login_required
def detail(app_id):
    a = get_app_or_404(app_id)
    if not has_role(g.user, 'approver') and a['applicant_id'] != g.user['id'] and a['owner_id'] != g.user['id']:
        abort(403)
    can_decide = (a['status'] == 'pending' and has_role(g.user, 'approver')
                  and (a['applicant_id'] != g.user['id'] or has_role(g.user, 'admin')))
    return render_template('applications/detail.html', a=a, can_decide=can_decide,
                           auto_create=current_app.config['AUTO_CREATE_REPO'])


def _approve_create(conn, a):
    """新建仓库申请通过：（可选）在平台上创建仓库，并写入登记表。返回 (repo_id, 结果说明)。"""
    cfg = current_app.config
    existing = find_registered(a['platform'], a['namespace'], a['name'])
    if existing:
        return existing['id'], '登记表中已存在同名仓库，已直接关联。'

    url, on_platform, result = web_url(cfg, a['platform'], a['namespace'], a['name']), None, ''
    if cfg['AUTO_CREATE_REPO']:
        client = get_client(cfg, a['platform'])
        if not client.token:
            result = '未配置 {} token，未能自动创建，请手动在平台上创建该仓库。'.format(client.label)
        else:
            try:
                info = client.create_repo(a['namespace'], namespace_kind(cfg, a['platform'], a['namespace']),
                                          a['name'], a['description'], a['visibility'] == 'private')
                url, on_platform = info.get('url') or url, 1
                result = '已在 {} 上自动创建仓库。'.format(client.label)
            except PlatformError as e:
                result = '自动创建失败，请手动在平台上创建：{}'.format(e)
    else:
        result = '请管理员在平台上创建该仓库（未开启自动创建）。'

    now = now_iso()
    cur = conn.execute(
        '''INSERT INTO repos (platform, namespace, name, url, description, team, visibility, owner_id, source,
                              application_id, on_platform, last_seen_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'request', ?, ?, ?, ?, ?)''',
        (a['platform'], a['namespace'], a['name'], url, a['description'], a['team'], a['visibility'],
         a['owner_id'], a['id'], on_platform, now if on_platform else None, now, now))
    log_event(conn, 'create', '申请 #{} 审批通过后登记。{}'.format(a['id'], result),
              repo_id=cur.lastrowid, user_id=g.user['id'], application_id=a['id'])
    return cur.lastrowid, result


def _approve_archive(conn, a):
    if a['repo_id'] is None:
        return None, '登记记录已被删除。'
    now = now_iso()
    conn.execute("UPDATE repos SET status = 'archived', archived_at = ?, updated_at = ? WHERE id = ?",
                 (now, now, a['repo_id']))
    log_event(conn, 'archive', '归档申请 #{} 审批通过'.format(a['id']),
              repo_id=a['repo_id'], user_id=g.user['id'], application_id=a['id'])
    return a['repo_id'], '已在登记表中标记为归档，请同步在平台上将仓库设为只读/Archived。'


@bp.route('/applications/<int:app_id>/decide', methods=['POST'])
@role_required('approver')
def decide(app_id):
    a = get_app_or_404(app_id)
    if a['status'] != 'pending':
        flash('该申请已处理。', 'error')
        return redirect(url_for('applications.detail', app_id=app_id))
    if a['applicant_id'] == g.user['id'] and not has_role(g.user, 'admin'):
        abort(403)
    action = request.form.get('action')
    comment = request.form.get('comment', '').strip()
    if action not in ('approve', 'reject'):
        abort(400)
    if action == 'reject' and not comment:
        flash('驳回时请填写原因。', 'error')
        return redirect(url_for('applications.detail', app_id=app_id))

    conn = get_db()
    repo_id, result = a['repo_id'], ''
    if action == 'approve':
        if a['kind'] == 'create':
            repo_id, result = _approve_create(conn, a)
        else:
            repo_id, result = _approve_archive(conn, a)
    conn.execute('''UPDATE applications SET status = ?, approver_id = ?, decision_comment = ?, decided_at = ?,
                    repo_id = ?, create_result = ? WHERE id = ?''',
                 ('approved' if action == 'approve' else 'rejected', g.user['id'], comment, now_iso(),
                  repo_id, result, app_id))
    if action == 'reject' and a['repo_id']:
        log_event(conn, 'archive_rejected', comment, repo_id=a['repo_id'], user_id=g.user['id'], application_id=app_id)
    conn.commit()

    verdict = '已通过' if action == 'approve' else '已驳回'
    body = '你的{}申请（{}:{}/{}）{}。\n审批人：{}\n意见：{}\n{}\n\n详情：{}'.format(
        C.APP_KIND[a['kind']], a['platform'], a['namespace'], a['name'], verdict,
        g.user['display_name'] or g.user['username'], comment or '无', result,
        url_for('applications.detail', app_id=app_id, _external=True))
    notify.send_email(current_app.config, [a['applicant_email']],
                      '{}申请{}：{}/{}'.format(C.APP_KIND[a['kind']], verdict, a['namespace'], a['name']), body)
    flash('已{}。{}'.format('通过' if action == 'approve' else '驳回', result), 'ok')
    return redirect(url_for('applications.detail', app_id=app_id))


@bp.route('/applications/<int:app_id>/withdraw', methods=['POST'])
@login_required
def withdraw(app_id):
    a = get_app_or_404(app_id)
    if a['applicant_id'] != g.user['id'] or a['status'] != 'pending':
        abort(403)
    conn = get_db()
    conn.execute("UPDATE applications SET status = 'withdrawn', decided_at = ? WHERE id = ?", (now_iso(), app_id))
    conn.commit()
    flash('申请已撤回。', 'ok')
    return redirect(url_for('applications.detail', app_id=app_id))
