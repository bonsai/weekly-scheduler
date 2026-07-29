"""pipeline.py — weekly-scheduler LangGraph pipeline

4 node 直線パイプライン:
    START → extract → create → structure → schedule → END

extract で action_items が 0 件の場合は END へ直行する conditional edge。

実行:
    python3 pipeline.py                    # 直近7日 recap
    python3 pipeline.py --days 14          # 14日分
    python3 pipeline.py --run-id 2026-07-29T22:00

チェックポイント:
    checkpoints/weekly.db (SqliteSaver)
    失敗時は同じ run_id で再invokeすると checkpoint から resume。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from state import PipelineState  # noqa: E402
from nodes.create import create_node  # noqa: E402
from nodes.extract import extract_node  # noqa: E402
from nodes.schedule import schedule_node  # noqa: E402
from nodes.structure import structure_node  # noqa: E402


def build_app():
    """LangGraph StateGraph を構築して返す。checkpointer 付き。"""
    from langgraph.graph import END, START, StateGraph

    graph: StateGraph = StateGraph(PipelineState)
    graph.add_node("extract", extract_node)
    graph.add_node("create", create_node)
    graph.add_node("structure", structure_node)
    graph.add_node("schedule", schedule_node)

    graph.add_edge(START, "extract")
    graph.add_conditional_edges(
        "extract",
        lambda s: "create" if s.get("action_items") else END,
    )
    graph.add_edge("create", "structure")
    graph.add_edge("structure", "schedule")
    graph.add_edge("schedule", END)

    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        cp_path = HERE / "checkpoints" / "weekly.db"
        cp_path.parent.mkdir(parents=True, exist_ok=True)
        checkpointer = SqliteSaver.from_conn_string(str(cp_path))
        return graph.compile(checkpointer=checkpointer)
    except ImportError:
        # langgraph-checkpoint-sqlite 未インストール時は checkpointer 無し
        return graph.compile()
    except Exception:
        return graph.compile()


def run(days: int = 7, run_id: str | None = None) -> dict:
    """パイプラインを実行。結果 dict を返す。"""
    from lib.recap import find_recaps

    recap_paths = find_recaps(days=days)
    if not recap_paths:
        print(f"recap ファイルが見つかりません (days={days})")
        return {"weekly_schedule": [], "errors": ["no recaps"]}

    run_id = run_id or datetime.now().strftime("%Y-%m-%dT%H:%M")
    print(f"weekly-scheduler run_id={run_id}")
    print(f"  recap files: {recap_paths}")

    app = build_app()
    initial_state: PipelineState = {
        "recap_paths": recap_paths,
        "action_items": [],
        "created_ids": [],
        "structured_tasks": [],
        "weekly_schedule": [],
        "errors": [],
        "run_id": run_id,
    }
    config = {"configurable": {"thread_id": run_id}}
    result = app.invoke(initial_state, config=config)

    # 結果表示
    print("\n=== 週間スケジュール ===")
    for day in result.get("weekly_schedule", []) or []:
        print(f"\n■ {day.date}")
        if not day.tasks:
            print("  (空き)")
        for slot in day.tasks:
            print(f"  {slot.start}-{slot.end}  {slot.title}")
    if result.get("errors"):
        print("\n=== エラー ===")
        for e in result["errors"]:
            print(f"  ! {e}")

    # JSON 保存
    out_dir = HERE / "checkpoints"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_id = run_id.replace(":", "")
    out_path = out_dir / f"schedule-{safe_id}.json"
    serializable = {
        "run_id": run_id,
        "recap_paths": recap_paths,
        "weekly_schedule": [
            day.model_dump() for day in result.get("weekly_schedule", []) or []
        ],
        "errors": result.get("errors", []),
    }
    out_path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n保存: {out_path}")
    return result


# モジュールレベルの app (テスト・インポート用)
app = build_app()


def main() -> int:
    parser = argparse.ArgumentParser(description="weekly-scheduler pipeline")
    parser.add_argument("--days", type=int, default=7, help="何日分のrecapを対象とするか")
    parser.add_argument("--run-id", type=str, default=None, help="実行ID (checkpoint resume 用)")
    args = parser.parse_args()
    result = run(days=args.days, run_id=args.run_id)
    return 0 if result.get("weekly_schedule") else 1


if __name__ == "__main__":
    sys.exit(main())