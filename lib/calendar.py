"""calendar.py — 週間スケジュールのカレンダー登録

優先順位:
  1. GWS Calendar (mito MCP 経由) — hermes 実行時に tool として利用可能
  2. ローカル ``schedule.db`` (MITO_AGENTS.md schema 準拠) — フォールバック
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

SCHEDULE_DB = Path.home() / ".hermes" / "mito" / "schedule.db"


def init_schedule_db(db_path: Path | None = None) -> None:
    db = db_path or SCHEDULE_DB
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schedule (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            time        TEXT,
            title       TEXT NOT NULL,
            detail      TEXT,
            tags        TEXT,
            status      TEXT DEFAULT 'pending',
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()


def add_to_schedule_db(day_schedule: Any) -> list[int]:
    """DaySchedule を schedule.db に挿入。挿入された id リストを返す。"""
    init_schedule_db()
    conn = sqlite3.connect(str(SCHEDULE_DB))
    ids: list[int] = []
    for slot in day_schedule.tasks:
        detail = json.dumps({"db_id": slot.db_id, "depends_on": slot.depends_on})
        cur = conn.execute(
            "INSERT INTO schedule (date, time, title, detail, tags, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', datetime('now'), datetime('now'))",
            (day_schedule.date, slot.start, slot.title, detail, "weekly-scheduler"),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return ids


def _emit_gws_event(day_schedule: Any) -> int:
    """mito MCP ``mito_create_event`` を介して GWS Calendar に event を発行。

    hermes skill 実行コンテキスト外では呼び出せない。フォールバックを返す場合は 0。
    """
    # 環境変数 ``WEEKLY_SCHEDULER_NO_GWS=1`` で明示的に無効化
    if os.environ.get("WEEKLY_SCHEDULER_NO_GWS"):
        return 0
    # mito MCP は hermes が tool として公開している。本モジュールは直で叩かず、
    #上位 (パイプライン実行コード / hermes agent) に委譲する。
    # ここでは foxor直接実行環境では GWS に触れないため 0 を返す。
    return 0


def register_week(
    weekly_schedule: list[Any], use_gws: bool = True, run_id: str | None = None
) -> dict[str, Any]:
    """週間スケジュールをカレンダーに登録。

    Returns:
        {"gws": count, "local": count, "errors": [str]}
    """
    result: dict[str, Any] = {"gws": 0, "local": 0, "errors": []}
    if not weekly_schedule:
        return result

    gws_count = 0
    if use_gws:
        for day in weekly_schedule:
            try:
                gws_count += _emit_gws_event(day)
            except Exception as e:
                result["errors"].append(f"GWS {day.date}: {e}")
    result["gws"] = gws_count

    # GWS が一つも発行できなかったら、ローカルDBにフォールバック
    if gws_count == 0:
        try:
            for day in weekly_schedule:
                ids = add_to_schedule_db(day)
                result["local"] += len(ids)
        except Exception as e:
            result["errors"].append(f"schedule.db: {e}")
    return result