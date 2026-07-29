"""state.py — weekly-scheduler pipeline state & models

LangGraph StateGraph が扱う全体状態 (PipelineState TypedDict) と
各ノード間で受け渡しされる Pydantic models を定義する。

設計文書: ~/.hermes/skills/weekly-scheduler/design.md §3
"""

from typing import Literal, TypedDict

from pydantic import BaseModel, Field


# --- ① extract の出力 ----------------------------------------------------


class ActionItem(BaseModel):
    """recap から抽出されたアクションアイテム。"""

    title: str = Field(description="動詞始まりの簡潔なタイトル")
    type: Literal["todo", "issue", "ticket"] = Field(
        default="todo",
        description="todo=個人メモ/調査/学習, issue=開発タスク, ticket=外部起点の報告",
    )
    project_hint: str | None = Field(
        default=None,
        description="リポジトリ名 (bonsai/xxx) またはカテゴリ (TEXT/KUIZ/dev/ops). 不明なら null",
    )
    source_recap: str = Field(default="", description="元 recap ファイル名")
    source_section: str = Field(default="", description="recap 内の見出し")
    raw_text: str = Field(default="", description="抽出元原文")


# --- ③ structure の出力 --------------------------------------------------


class StructuredTask(BaseModel):
    """構造化タスク。tasque DB のレコードに対応。"""

    db_id: int = Field(description="tasque DB 内の issue.id or todo.id")
    db_type: Literal["todo", "issue"] = Field(default="issue")
    title: str
    priority: int = Field(
        default=0,
        ge=-2,
        le=2,
        description="-2..+2 (tasque準拠). +2=緊急, +1=高, 0=既定, -1=低, -2=任意",
    )
    duration_min: int = Field(
        default=30,
        description="見積もり分数。15/25/30/45/60/90/120 のいずれかを推奨",
    )
    dependencies: list[int] = Field(
        default_factory=list,
        description="依存先タスクの db_id リスト (DAG)",
    )
    due_date: str | None = Field(
        default=None,
        description="ISO8601 date (YYYY-MM-DD). なしは null",
    )
    labels: list[str] = Field(default_factory=list)


# --- ④ schedule の出力 ---------------------------------------------------


class TimeSlot(BaseModel):
    """1つの時間枠。"""

    db_id: int
    title: str
    start: str = Field(description="HH:MM")
    end: str = Field(description="HH:MM")
    depends_on: list[int] = Field(default_factory=list)


class DaySchedule(BaseModel):
    """1日分のスケジュール。"""

    date: str = Field(description="YYYY-MM-DD")
    tasks: list[TimeSlot] = Field(default_factory=list)


# --- LangGraph State -----------------------------------------------------


class PipelineState(TypedDict, total=False):
    """LangGraph が保持するパイプライン全体状態。
    `total=False` で部分更新を許可 (ノードは必要な key のみ返す)。
    """

    recap_paths: list[str]
    action_items: list[ActionItem]
    created_ids: list[int]
    structured_tasks: list[StructuredTask]
    weekly_schedule: list[DaySchedule]
    errors: list[str]
    run_id: str