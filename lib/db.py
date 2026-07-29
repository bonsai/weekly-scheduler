"""db.py — tasque DB (daily.task.db / coding.task.db) 接続・task_links 管理

DB は ~/repo/second-brains/db/ に配置 (ON CLOUD 設計準拠)。
テーブルが未作成の場合は初期化スキーマを実行する。

スキーマは tasque skill references/schema.md に準拠しつつ、
issue テーブルに ``duration_min`` カラムを追加で定義する。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DB_DIR = Path.home() / "repo" / "second-brains" / "db"
DAILY_DB = DB_DIR / "daily.task.db"
CODING_DB = DB_DIR / "coding.task.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """テーブルに ``column`` が無ければ ALTER TABLE で追加。"""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def init_daily(db_path: Path | None = None) -> None:
    db = db_path or DAILY_DB
    conn = _connect(db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS todo (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            title           TEXT NOT NULL,
            description     TEXT DEFAULT '',
            source          TEXT NOT NULL DEFAULT 'local',
            status          TEXT NOT NULL DEFAULT 'pending',
            priority        INTEGER DEFAULT 0,
            due_date        TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at    TEXT,
            source_id       TEXT,
            tags            TEXT DEFAULT '[]',
            duration_min    INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_todo_source_id ON todo(source_id);
        CREATE INDEX IF NOT EXISTS idx_todo_status ON todo(status);
        """
    )
    conn.commit()
    conn.close()


def init_coding(db_path: Path | None = None) -> None:
    db = db_path or CODING_DB
    conn = _connect(db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS issue (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            type            TEXT NOT NULL DEFAULT 'issue',
            title           TEXT NOT NULL,
            description     TEXT DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'open',
            priority        INTEGER DEFAULT 0,
            source          TEXT NOT NULL DEFAULT 'gh',
            owner           TEXT,
            repo            TEXT,
            gh_number       INTEGER,
            gh_url          TEXT,
            parent_id       INTEGER REFERENCES issue(id),
            due_date        TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
            closed_at       TEXT,
            labels          TEXT DEFAULT '[]',
            stack           TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS task_links (
            parent_id INTEGER REFERENCES issue(id) ON DELETE CASCADE,
            child_id  INTEGER REFERENCES issue(id) ON DELETE CASCADE,
            PRIMARY KEY (parent_id, child_id)
        );
        CREATE TABLE IF NOT EXISTS epic (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            title           TEXT NOT NULL,
            description     TEXT DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'active',
            repo            TEXT,
            gh_number       INTEGER,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS issue_epic (
            issue_id INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
            epic_id  INTEGER NOT NULL REFERENCES epic(id) ON DELETE CASCADE,
            PRIMARY KEY (issue_id, epic_id)
        );
        CREATE TABLE IF NOT EXISTS comment (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_id        INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
            author          TEXT NOT NULL DEFAULT 'user',
            body            TEXT NOT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            gh_comment_id   INTEGER
        );
        CREATE TABLE IF NOT EXISTS issue_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_id        INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
            changed_at      TEXT NOT NULL DEFAULT (datetime('now')),
            field           TEXT NOT NULL,
            old_value       TEXT,
            new_value       TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            repo            TEXT NOT NULL,
            direction       TEXT NOT NULL,
            count           INTEGER NOT NULL DEFAULT 0,
            synced_at       TEXT NOT NULL DEFAULT (datetime('now')),
            error           TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_issue_status ON issue(status);
        CREATE INDEX IF NOT EXISTS idx_issue_repo ON issue(repo);
        """
    )
    # issue テーブルに duration_min を追加 (既存DB互換)
    _ensure_column(conn, "issue", "duration_min", "duration_min INTEGER DEFAULT 0")
    conn.commit()
    conn.close()


def insert_todo(
    title: str,
    source_id: str,
    duration_min: int = 0,
    priority: int = 0,
    tags: list[str] | None = None,
) -> int:
    """``daily.task.db`` の ``todo`` に挿入。``source_id`` で冪等。"""
    init_daily()
    conn = _connect(DAILY_DB)
    existing = conn.execute("SELECT id FROM todo WHERE source_id=?", (source_id,)).fetchone()
    if existing:
        conn.close()
        return existing["id"]
    labels_json = str(tags or [])
    cur = conn.execute(
        "INSERT INTO todo (title, source, source_id, status, priority, duration_min, tags) "
        "VALUES (?, 'recap', ?, 'pending', ?, ?, ?)",
        (title, source_id, priority, duration_min, labels_json),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def insert_issue(
    title: str,
    source_id: str,
    issue_type: str = "issue",
    repo: str | None = None,
    duration_min: int = 0,
    priority: int = 0,
    labels: list[str] | None = None,
) -> tuple[int, int | None]:
    """``coding.task.db`` に issue/ticket を挿入。冪等は ``labels`` に埋めた ``source_id`` で判定。

    Returns:
        (db_id, gh_number or None)
    """
    init_coding()
    conn = _connect(CODING_DB)
    label_list = list(labels or [])
    # source_id を labels に仕込む (issue テーブルに source_id カラムが無いため)
    if source_id not in label_list:
        label_list.append(source_id)
    label_str = str(label_list)
    existing = conn.execute(
        "SELECT id FROM issue WHERE title=? AND labels LIKE ?",
        (title, f"%{source_id}%"),
    ).fetchone()
    if existing:
        conn.close()
        return (existing["id"], None)
    cur = conn.execute(
        "INSERT INTO issue (type, title, status, source, repo, priority, labels, duration_min) "
        "VALUES (?, ?, 'open', 'recap', ?, ?, ?, ?)",
        (issue_type, title, repo, priority, label_str, duration_min),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return (rid, None)


def update_issue_struct(
    id_: int, priority: int, duration_min: int, due_date: str | None = None
) -> None:
    """③の構造化結果を ``coding.task.db`` に書き戻す。"""
    init_coding()
    conn = _connect(CODING_DB)
    conn.execute(
        "UPDATE issue SET priority=?, due_date=?, duration_min=?, updated_at=datetime('now') WHERE id=?",
        (priority, due_date, duration_min, id_),
    )
    conn.commit()
    conn.close()


def update_todo_struct(
    id_: int, priority: int, duration_min: int, due_date: str | None = None
) -> None:
    """③の構造化結果を ``daily.task.db`` に書き戻す。"""
    init_daily()
    conn = _connect(DAILY_DB)
    conn.execute(
        "UPDATE todo SET priority=?, duration_min=?, due_date=?, updated_at=datetime('now') WHERE id=?",
        (priority, duration_min, due_date, id_),
    )
    conn.commit()
    conn.close()


def add_task_link(parent_id: int, child_id: int) -> None:
    """依存関係 (parent → child) を ``task_links`` に格納。"""
    init_coding()
    conn = _connect(CODING_DB)
    conn.execute(
        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (parent_id, child_id),
    )
    conn.commit()
    conn.close()


def fetch_issues(ids: list[int]) -> list[dict[str, Any]]:
    """``coding.task.db`` から指定 ID の issue レコードを取得。"""
    if not ids:
        return []
    init_coding()
    conn = _connect(CODING_DB)
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, type, title, description, status, priority, labels, "
        f"stack, due_date, duration_min FROM issue WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_todos(ids: list[int]) -> list[dict[str, Any]]:
    """``daily.task.db`` から指定 ID の todo レコードを取得。"""
    if not ids:
        return []
    init_daily()
    conn = _connect(DAILY_DB)
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, description, status, priority, tags, due_date, duration_min "
        f"FROM todo WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_issue_gh_url(db_id: int, gh_url: str) -> None:
    """GitHub Issue 作成成功後に URL を記録。"""
    init_coding()
    conn = _connect(CODING_DB)
    conn.execute("UPDATE issue SET gh_url=?, updated_at=datetime('now') WHERE id=?", (gh_url, db_id))
    conn.commit()
    conn.close()