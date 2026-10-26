import sqlite3

from flask import current_app, g

from .util import now_iso

# 只追加、不修改：每个元素是一个版本，启动时按 PRAGMA user_version 依次执行未执行的版本。
MIGRATIONS = [
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        display_name TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'developer',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    CREATE TABLE repos (
        id INTEGER PRIMARY KEY,
        platform TEXT NOT NULL,
        namespace TEXT NOT NULL COLLATE NOCASE,
        name TEXT NOT NULL COLLATE NOCASE,
        url TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        team TEXT NOT NULL DEFAULT '',
        visibility TEXT NOT NULL DEFAULT 'private',
        owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'active',
        source TEXT NOT NULL DEFAULT 'manual',
        application_id INTEGER,
        on_platform INTEGER,
        last_seen_at TEXT,
        last_commit_at TEXT,
        recent_commits INTEGER,
        activity_checked_at TEXT,
        last_reminded_at TEXT,
        snooze_until TEXT,
        snooze_reason TEXT NOT NULL DEFAULT '',
        archived_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (platform, namespace, name)
    );

    CREATE TABLE applications (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        applicant_id INTEGER NOT NULL REFERENCES users(id),
        repo_id INTEGER REFERENCES repos(id) ON DELETE SET NULL,
        platform TEXT NOT NULL DEFAULT '',
        namespace TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        team TEXT NOT NULL DEFAULT '',
        visibility TEXT NOT NULL DEFAULT 'private',
        owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        reason TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        approver_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        decision_comment TEXT NOT NULL DEFAULT '',
        create_result TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        decided_at TEXT
    );
    CREATE INDEX idx_applications_status ON applications(status);

    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        repo_id INTEGER REFERENCES repos(id) ON DELETE CASCADE,
        application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        action TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_events_repo ON events(repo_id);

    CREATE TABLE scan_runs (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        triggered_by TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        finished_at TEXT,
        summary TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE scan_findings (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
        issue TEXT NOT NULL,
        platform TEXT NOT NULL,
        namespace TEXT NOT NULL,
        name TEXT NOT NULL,
        url TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        visibility TEXT NOT NULL DEFAULT 'private',
        repo_id INTEGER REFERENCES repos(id) ON DELETE SET NULL,
        resolved_at TEXT,
        resolution TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_findings_run ON scan_findings(run_id);

    CREATE TABLE scan_ignores (
        platform TEXT NOT NULL,
        namespace TEXT NOT NULL COLLATE NOCASE,
        name TEXT NOT NULL COLLATE NOCASE,
        reason TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        PRIMARY KEY (platform, namespace, name)
    );
    """,
]


def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def migrate(path):
    conn = connect(path)
    try:
        conn.execute('PRAGMA journal_mode = WAL')
        conn.isolation_level = None
        conn.execute('BEGIN EXCLUSIVE')
        try:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            for i in range(version, len(MIGRATIONS)):
                for stmt in MIGRATIONS[i].split(';'):
                    if stmt.strip():
                        conn.execute(stmt)
                conn.execute('PRAGMA user_version = %d' % (i + 1))
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
    finally:
        conn.close()


def get_db():
    if 'db' not in g:
        g.db = connect(current_app.config['DATABASE'])
    return g.db


def close_db(exc=None):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()


def init_app(app):
    migrate(app.config['DATABASE'])
    app.teardown_appcontext(close_db)


def query(sql, *args):
    return get_db().execute(sql, args).fetchall()


def query_one(sql, *args):
    return get_db().execute(sql, args).fetchone()


def log_event(conn, action, detail='', repo_id=None, user_id=None, application_id=None):
    """记录一条操作历史，不提交事务。"""
    conn.execute(
        'INSERT INTO events (repo_id, application_id, user_id, action, detail, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (repo_id, application_id, user_id, action, detail, now_iso()))
