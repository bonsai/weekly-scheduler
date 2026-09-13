"""schedule.py — ④ StructuredTask → list[DaySchedule]

networkx を使ったトポロジカルソート + priority queue によるKahn's algorithm で
実行可能順序を確定し、1日 ``DAILY_CAP_MIN`` 分以内の bin packing で
1週間 (月-金) の ``DaySchedule`` を構築する。

配置後は ``lib/calendar.register_week`` で GWS Calendar または
``schedule.db`` に発行する。

LLM は使用しない。純アルゴリズム。
"""

from __future__ import annotations

import heapq
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

from state import DaySchedule, PipelineState, StructuredTask, TimeSlot  # noqa: E402

DAILY_CAP_MIN = 360  # 1日 = 6h (9:00-12:00 / 13:00-16:00)
WEEK_DAYS = 5
BASE_HOUR = 9     # 09:00 開始
LUNCH_HOUR = 12   # 12:00-13:00 昼休憩
SLOT_COUNT = 12   # 1日12コマ (6h / 30min = 12)


def _next_weekday_dates(start: datetime | None = None) -> list[str]:
    """次の月-金 5日分の日付 (YYYY-MM-DD) を返す。"""
    now = start or datetime.now()
    # 日曜22時のcron想定なので翌月曜から
    days_until_monday = (7 - now.weekday()) % 7  # Monday=0
    if days_until_monday == 0:
        days_until_monday = 7
    monday = now + timedelta(days=days_until_monday)
    return [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(WEEK_DAYS)]


def _m_to_hhmm(minutes_from_base: int) -> str:
    """ベース(09:00)からの経過分数 → HH:MM。昼休憩(12:00-13:00)を跨ぐ場合は+60分。"""
    total = BASE_HOUR * 60 + minutes_from_base
    # 12:00以降なら昼休憩60分追加
    if total >= LUNCH_HOUR * 60:
        total += 60
    h = (total // 60) % 24
    mm = total % 60
    return f"{h:02d}:{mm:02d}"


def _min_to_tab(minutes_from_base: int) -> int:
    """ベースからの経過分数 → スロット番号 (1-12)。30分=1スロット。"""
    # minutes_from_base: 0-180 (AM), 180-360 (PM, lunch skipped)
    return (minutes_from_base // 30) + 1


def _build_dag(tasks: list[StructuredTask]):
    import networkx as nx

    G = nx.DiGraph()
    by_id = {t.db_id: t for t in tasks}
    for t in tasks:
        G.add_node(t.db_id)
    for t in tasks:
        for dep_id in t.dependencies:
            if dep_id in by_id:
                G.add_edge(dep_id, t.db_id)  # dep → t
    return G


def _sorted_order(G, by_id: dict[int, StructuredTask]) -> list[int]:
    """Kahn's algorithm with priority queue (high priority first)。"""
    in_deg = dict(G.in_degree())
    counter = 0
    queue: list[tuple] = []
    for nid, deg in in_deg.items():
        if deg == 0:
            t = by_id.get(nid)
            score = -t.priority if t else 0  # min-heap → priority 大優先
            heapq.heappush(queue, (score, counter, nid))
            counter += 1

    order: list[int] = []
    while queue:
        _, _, nid = heapq.heappop(queue)
        order.append(nid)
        for succ in G.successors(nid):
            in_deg[succ] -= 1
            if in_deg[succ] == 0:
                t = by_id.get(succ)
                score = -t.priority if t else 0
                heapq.heappush(queue, (score, counter, succ))
                counter += 1
    return order


def schedule_week(
    tasks: list[StructuredTask], start: datetime | None = None
) -> list[DaySchedule]:
    """1週間のスケジュールを構築。"""
    if not tasks:
        return []

    G = _build_dag(tasks)
    by_id = {t.db_id: t for t in tasks}
    order = _sorted_order(G, by_id)

    # order に無い孤立ノードがもしあれば追加
    for nid in G.nodes:
        if nid not in order:
            order.append(nid)

    dates = _next_weekday_dates(start)
    day_minutes = [0] * WEEK_DAYS
    day_slots: list[list[TimeSlot]] = [[] for _ in range(WEEK_DAYS)]
    placed_day: dict[int, int] = {}

    for nid in order:
        t = by_id.get(nid)
        if t is None:
            continue

        dep_days = [placed_day[d] for d in t.dependencies if d in placed_day]
        earliest = max(dep_days) + 1 if dep_days else 0

        placed = False
        for i in range(max(0, earliest), WEEK_DAYS):
            # due_date 制約
            if t.due_date:
                try:
                    due_dt = datetime.strptime(t.due_date, "%Y-%m-%d")
                    day_dt = datetime.strptime(dates[i], "%Y-%m-%d")
                    if day_dt.date() > due_dt.date():
                        break  # これ以降は置けない → 前日 bucketに overflow 扱い
                except ValueError:
                    pass

            if day_minutes[i] + t.duration_min <= DAILY_CAP_MIN:
                start_min = day_minutes[i]
                end_min = start_min + t.duration_min
                day_slots[i].append(
                    TimeSlot(
                        db_id=t.db_id,
                        title=t.title,
                        start=_m_to_hhmm(start_min),
                        end=_m_to_hhmm(end_min),
                        tab_start=_min_to_tab(start_min),
                        tab_end=_min_to_tab(end_min - 1) if end_min > start_min else _min_to_tab(start_min),
                        depends_on=list(t.dependencies),
                    )
                )
                day_minutes[i] = end_min
                placed_day[nid] = i
                placed = True
                break

        if not placed:
            # overflow → 最終日の末尾にマーク付で残す
            last = WEEK_DAYS - 1
            start_min_ov = day_minutes[last]
            end_min_ov = start_min_ov + t.duration_min
            day_slots[last].append(
                TimeSlot(
                    db_id=nid,
                    title=f"[overflow] {t.title}",
                    start=_m_to_hhmm(start_min_ov),
                    end=_m_to_hhmm(end_min_ov),
                    tab_start=_min_to_tab(start_min_ov),
                    tab_end=_min_to_tab(end_min_ov - 1) if end_min_ov > start_min_ov else _min_to_tab(start_min_ov),
                    depends_on=list(t.dependencies),
                )
            )
            day_minutes[last] += t.duration_min
            placed_day[nid] = last

    return [DaySchedule(date=dates[i], tasks=day_slots[i]) for i in range(WEEK_DAYS)]


def schedule_node(state: PipelineState) -> dict:
    """LangGraph node: 構造化タスクを1週間スケジュールに配置。"""
    tasks: list[StructuredTask] = state.get("structured_tasks", []) or []
    errors: list[str] = state.get("errors", []) or []
    run_id: str = state.get("run_id", "") or ""

    if not tasks:
        print(f"  [schedule] no tasks, skipping", flush=True)
        return {"weekly_schedule": [], "errors": errors}

    print(f"  [schedule] scheduling {len(tasks)} tasks (cap={DAILY_CAP_MIN}min/day, {SLOT_COUNT} slots)", flush=True)
    schedule = schedule_week(tasks)

    # 集計
    total_slots = sum(len(d.tasks) for d in schedule)
    total_mins = sum(
        sum(
            (int(s.end.split(":")[0]) * 60 + int(s.end.split(":")[1]))
            - (int(s.start.split(":")[0]) * 60 + int(s.start.split(":")[1]))
            for s in d.tasks
        )
        for d in schedule
    )
    print(f"  [schedule] {len(schedule)} days, {total_slots} slots, ~{total_mins}min scheduled", flush=True)
    for d in schedule:
        task_tabs = [f"tab {s.tab_start}" if s.tab_start == s.tab_end else f"tab {s.tab_start}-{s.tab_end}" for s in d.tasks]
        print(f"  [schedule]   {d.date}: {len(d.tasks)} tasks, tabs: {', '.join(task_tabs) if task_tabs else '(empty)'}", flush=True)

    # カレンダー登録
    from lib.calendar import register_week

    reg = register_week(schedule, use_gws=True, run_id=run_id)
    errors = errors + reg.get("errors", [])
    return {"weekly_schedule": schedule, "errors": errors}