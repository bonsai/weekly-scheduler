"""create.py — ② ActionItem → tasque DB + GitHub Issues

ActionItem を ``daily.task.db`` / ``coding.task.db`` に挿入する。
``source_id`` (recap:<filename>:<sha8>) で冪等性を保証。

``type=issue`` かつ ``project_hint`` に ``owner/repo`` 形式が推定されている場合は
``gh issue create`` も実行し、生成された URL をDBに保存する。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

from state import ActionItem, PipelineState  # noqa: E402
from lib.db import (  # noqa: E402
    insert_issue,
    insert_todo,
    set_issue_gh_url,
)


def _source_id(recap_path: str, raw_text: str) -> str:
    """冪等キー ``recap:<filename>:<sha8>`` を生成。"""
    h = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:8]
    return f"recap:{os.path.basename(recap_path)}:{h}"


def _try_create_github_issue(item: ActionItem, db_id: int) -> str | None:
    """``project_hint`` が owner/repo 形式なら GitHub Issue を作成。gh_url を返す。"""
    hint = item.project_hint or ""
    if "/" not in hint:
        return None
    try:
        r = subprocess.run(
            ["gh", "issue", "create", "--repo", hint,
             "--title", item.title,
             "--label", "recap-auto"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if r.returncode == 0:
            url = r.stdout.strip()
            if url.startswith("http"):
                set_issue_gh_url(db_id, url)
                return url
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return None


def create_node(state: PipelineState) -> dict:
    """LangGraph node: ActionItem を tasque DB に投入。"""
    items: list[ActionItem] = state.get("action_items", []) or []
    errors: list[str] = state.get("errors", []) or []
    if not items:
        return {"created_ids": [], "errors": errors}

    created_ids: list[int] = []
    for item in items:
        sid = _source_id(item.source_recap, item.raw_text or item.title)
        if item.type == "todo":
            try:
                db_id = insert_todo(
                    title=item.title,
                    source_id=sid,
                    priority=0,
                )
                created_ids.append(db_id)
            except Exception as e:
                errors.append(f"insert_todo: {e}")
        else:  # "issue" or "ticket"
            try:
                db_id, gh_num = insert_issue(
                    title=item.title,
                    source_id=sid,
                    issue_type="ticket" if item.type == "ticket" else "issue",
                    repo=item.project_hint,
                )
                created_ids.append(db_id)
                # GitHub Issue 作成は repoが "owner/repo" 形式のときのみ
                _try_create_github_issue(item, db_id)
            except Exception as e:
                errors.append(f"insert_issue: {e}")

    return {"created_ids": created_ids, "errors": errors}