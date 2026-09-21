"""gather.py — 全データソース収集・統合

opencode.db のセッション履歴、GitHub Issues、issues.sqlite、
coding.task.db、recap ファイルを収集し、extract node が扱う
統合ドキュメント ``gathered_doc`` を生成する。

利用条件:
  - gh CLI が認証済み (bonsai org)
  - opencode.db が存在 (~/.local/share/opencode/opencode.db)
  - second-brains DB 群が存在 (~/repo/second-brains/db/)

使用法:
    from nodes.gather import gather_all
    doc = gather_all(days=14)
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

from state import PipelineState  # noqa: E402

# ---- paths ----
OPCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
ISSUES_DB = Path.home() / "repo" / "second-brains" / "db" / "issues.sqlite"
CODING_DB = Path.home() / "repo" / "second-brains" / "db" / "coding.task.db"
RAW_DIR = Path.home() / "wiki" / "raw"

REPO = "bonsai/second-brains"

# ---- helpers ----


def _db_connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"DB not found: {path}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_dt(ts: int | None) -> str:
    """Unix ms → YYYY-MM-DD HH:MM"""
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M")


def _fmt_cost(cost: float | None) -> str:
    if cost is None:
        return "-"
    return f"${cost:.2f}"


def _model_short(model_json: str | None) -> str:
    """model JSON → short name"""
    if not model_json:
        return "-"
    try:
        m = json.loads(model_json)
        return m.get("id", m.get("name", str(m)[:30]))
    except (json.JSONDecodeError, TypeError):
        return str(model_json)[:30]


# ---- session gather ----

def gather_sessions(days: int = 14) -> str:
    """opencode.db から直近のセッション一覧を markdown で返す。"""
    try:
        conn = _db_connect(OPCODE_DB)
    except FileNotFoundError:
        return "## Sessions\n\n(opencode.db not found)\n"

    since_ms = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
    rows = conn.execute(
        """SELECT id, title, path, time_created, time_updated, cost, model,
                  tokens_input, tokens_output
           FROM session
           WHERE time_created >= ?
           ORDER BY time_created DESC""",
        (since_ms,),
    ).fetchall()

    if not rows:
        conn.close()
        return "## Sessions\n\n(no sessions in last {days} days)\n"

    lines = [
        "## セッション履歴 (直近 {}日)".format(days),
        "",
        "| # | 日時 | セッションタイトル | モデル | 費用 |",
        "|---|------|-------------------|--------|------|",
    ]
    for i, r in enumerate(rows, 1):
        created = _fmt_dt(r["time_created"])
        title = (r["title"] or "-")[:60]
        model = _model_short(r["model"])
        cost = _fmt_cost(r["cost"])
        lines.append(f"| {i} | {created} | {title} | {model} | {cost} |")

    conn.close()
    return "\n".join(lines) + "\n"


# ---- GitHub issues gather ----

def gather_github_issues(max_issues: int = 40) -> str:
    """gh issue list から開いている Issue 一覧を markdown で返す。"""
    try:
        r = subprocess.run(
            ["gh", "issue", "list", "--repo", REPO,
             "--state", "open", "--limit", str(max_issues),
             "--json", "number,title,state,updatedAt,labels",
             "--search", "updated:>=2026-06-01 sort:updated-desc",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return f"## GitHub Issues\n\n(gh failed: {r.stderr.strip()})\n"
        issues = json.loads(r.stdout)
    except (FileNotFoundError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        return f"## GitHub Issues\n\n(gh error: {e})\n"

    if not issues:
        return "## GitHub Issues\n\n(no open issues)\n"

    # Filter out pure noise: [log] prefix issues (auto-generated)
    meaningful = [i for i in issues if not i.get("title", "").startswith("[log]")]
    # Also skip paste-only titles
    meaningful = [i for i in meaningful if not i.get("title", "").startswith("[issue]") or i.get("title","").startswith("[issue] #2")]

    lines = [
        "## GitHub Issues (bonsai/second-brains)",
        "",
        f"全 {len(issues)} 件中、有意なもの {len(meaningful)} 件を表示:",
        "",
    ]
    for i in meaningful[:30]:
        num = i["number"]
        title = i["title"][:80]
        updated = i.get("updatedAt", "")[:10]
        labels = ", ".join(l["name"] for l in i.get("labels", []))
        label_str = f" [{labels}]" if labels else ""
        lines.append(f"- #{num} ({updated}){label_str} {title}")

    return "\n".join(lines) + "\n"


# ---- issues.sqlite gather ----

def gather_issues_db(min_weight: float = 0.1) -> str:
    """issues.sqlite からアクティブなエントリを収集。"""
    try:
        conn = _db_connect(ISSUES_DB)
    except FileNotFoundError:
        return "## Cached Issues (issues.sqlite)\n\n(not found)\n"

    rows = conn.execute(
        """SELECT id, title, content_type, project, agent, weight, state, gh_issue_url,
                  substr(created_at,1,10) as created, substr(updated_at,1,10) as updated
           FROM entries
           WHERE state = 'active' AND weight >= ?
           ORDER BY weight DESC, updated_at DESC
           LIMIT 40""",
        (min_weight,),
    ).fetchall()

    if not rows:
        conn.close()
        return "## Cached Issues\n\n(no active entries)\n"

    lines = [
        "## Issues DB (issues.sqlite — 全ソース同期)",
        "",
        "| weight | project | title | updated |",
        "|--------|---------|-------|---------|",
    ]
    for r in rows:
        w = f"{r['weight']:.1f}"
        proj = (r["project"] or "-")[:15]
        title = (r["title"] or "-")[:50]
        updated = (r["updated"] or "-")[:10]
        lines.append(f"| {w} | {proj} | {title} | {updated} |")

    conn.close()
    return "\n".join(lines) + "\n"


# ---- coding.task.db gather ----

def gather_tasque_tasks() -> str:
    """coding.task.db から未完了タスクを収集。"""
    try:
        conn = _db_connect(CODING_DB)
    except FileNotFoundError:
        return "## Tasque Tasks (coding.task.db)\n\n(not found)\n"

    rows = conn.execute(
        """SELECT id, title, status, priority, duration_min, repo, labels
           FROM issue
           WHERE status = 'open'
           ORDER BY priority DESC, id"""
    ).fetchall()

    if not rows:
        conn.close()
        return "## Tasque Tasks (未完了)\n\n(no open tasks)\n"

    lines = [
        "## Tasque Tasks (coding.task.db — 未完了)",
        "",
        "| ID | priority | duration | title | repo |",
        "|----|----------|----------|-------|------|",
    ]
    for r in rows:
        dur = f"{r['duration_min']}min" if r['duration_min'] else "-"
        repo = (r["repo"] or "-")[:15]
        title = (r["title"] or "-")[:55]
        pri = r["priority"]
        lines.append(f"| {r['id']} | {pri:+d} | {dur} | {title} | {repo} |")

    conn.close()
    return "\n".join(lines) + "\n"


# ---- recap gather ----

def gather_recaps(days: int = 14) -> str:
    """wiki/raw/recap-*.md ファイルの内容を収集。"""
    from lib.recap import find_recaps, read_recap

    paths = find_recaps(days=days, raw_dir=RAW_DIR)
    if not paths:
        return "## Recap ファイル\n\n(no recent recaps)\n"

    sections = ["## Recap ファイル", ""]
    for p in paths:
        name = os.path.basename(p)
        content = read_recap(p)
        # Truncate very long recaps
        if len(content) > 4000:
            content = content[:4000] + "\n\n[...truncated]"
        sections.append(f"### {name}\n\n```\n{content}\n```\n")

    return "\n".join(sections)


# ---- main entry ----

def gather_all(days: int = 14) -> str:
    """全データソースを収集し、1つの統合マークダウンドキュメントを返す。

    gather node がこの結果を ``gathered_doc`` として state にセットし、
    extract node がこれを読んで ActionItem を抽出する。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = (
        f"# 週間スケジュール計画 — 全ソース統合 ({now})\n\n"
        f"以下は直近 {days} 日間の全データソースから収集した情報です。\n"
        f"これらの情報をもとに、今週やるべきアクションアイテムを全て抽出してください。\n"
    )

    parts = [
        header,
        gather_sessions(days=days),
        "",
        gather_github_issues(max_issues=40),
        "",
        gather_issues_db(min_weight=0.1),
        "",
        gather_tasque_tasks(),
        "",
        gather_recaps(days=days),
        "",
        "---",
        "## 抽出指示",
        "",
        "上記の全セクションから、**今すぐまたは今週中に実行すべき全てのタスク**を"
        "アクションアイテムとして抽出してください。",
        "- セッション履歴からは、完了した作業と未完了の作業を区別",
        "- GitHub Issues からは、未着手のタスクを全て抽出",
        "- Issues DB / Tasque からは、まだ完了していないタスクを抽出",
        "- Recap からは、残タスクセクションのアイテムを抽出",
        "- 不明なものは type=todo, コード作業は type=issue に分類",
    ]
    return "\n".join(parts)


def gather_node(state: PipelineState) -> dict:
    """LangGraph node: 全データソースを収集して gathered_doc を生成。"""
    errors: list[str] = state.get("errors", []) or []
    # state から days を取得、なければデフォルト14
    days: int = state.get("days", 14)
    print(f"  [gather] gathering all sources (days={days})...", flush=True)
    try:
        doc = gather_all(days=days)
        doc_len = len(doc)
        print(f"  [gather] gathered_doc: {doc_len} bytes", flush=True)
        return {"gathered_doc": doc, "errors": errors}
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"  [gather] FAILED: {e}", flush=True)
        print(tb, flush=True)
        return {
            "gathered_doc": "",
            "errors": errors + [f"gather failed: {e}"],
        }
