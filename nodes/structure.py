"""structure.py — ③ DB → StructuredTask[] (LangChain)

DB に記録されたバッチを取り出し、LLM で priority / duration_min / dependencies /
due_date を推定する。推定結果はDBに書き戻され、`task_links` に依存関係が保存される。

デフォルト LLM: opencode-go/deepseek-v4-flash (Go定額)
高難度時にフォールバック: opencode-go/deepseek-v4-pro (Go定額)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

from state import StructuredTask, PipelineState  # noqa: E402
from lib.db import (  # noqa: E402
    add_task_link,
    fetch_issues,
    update_issue_struct,
    update_todo_struct,
)

_MODEL_DEFAULT = "opencode-go/deepseek-v4-flash"
_MODEL_DEFAULT_FB = "opencode-go/deepseek-v4-pro"
MODEL_PRIMARY = os.environ.get("WEEKLY_LLM_STRUCTURE", _MODEL_DEFAULT)
MODEL_FALLBACK = os.environ.get("WEEKLY_LLM_STRUCTURE_FB", _MODEL_DEFAULT_FB)

SYSTEM_PROMPT = """あなたはタスク管理の専門家である。
以下のタスクリストについて、各タスクの優先度・見積もり時間・依存関係を推定せよ。

優先度スケール (tasque準拠):
  +2: 緊急（今日必須、本日中に完了すべき）
  +1: 高（今週中に完了すべき）
   0: 既定（通常優先度）
  -1: 低（来週以降でよい）
  -2: やらなくてもいい（バックログ）

見積もり時間: 以下のいずれかを選択
  15, 25, 30, 45, 60, 90, 120 (分)

依存関係: 他のタスクが完了してから着手すべき場合、
そのタスクの番号を ``dependencies`` (番号 [1], [2] 等) に指定する。
双方向依存は存在しない（DAGを形成する）。

判定基準:
- bug/障害/hotfix → priority +1 以上
- 期日明記 → priority +2 または due_date 設定
- 調査/リサーチ → duration 30〜45
- 設計/architecture → duration 60〜120
- docs/README → duration 15〜30
- テスト追加 → duration 30〜60"""


class _RawStruct:
    """LLMに返させる推定結果。``temp_id`` は ``[1], [2], ...`` の番号。
    Pydanticで定義するためのtypeを動的作成。
    """
    pass


def _make_raw_struct_model():
    from pydantic import BaseModel, Field

    class _RawStruct(BaseModel):
        temp_id: int = Field(description="タスクリストの番号 [1], [2], ...")
        priority: int = Field(ge=-2, le=2)
        duration_min: int = Field(description="15,25,30,45,60,90,120 のいずれか")
        dependencies: list[int] = Field(
            default_factory=list,
            description="依存先の temp_id リスト (例: [2])",
        )
        due_date: str | None = Field(
            default=None, description="YYYY-MM-DD。無ければ null"
        )

    return _RawStruct


def _try_opencode_zen() -> tuple[str, str, str] | None:
    """opencode-zen (auth.json) の設定を読み取る。使えなければ None。"""
    auth_path = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    try:
        data = json.loads(auth_path.read_text())
        key = data.get("opencode-go", {}).get("key", "")
        if key:
            return key, "https://api.opencode.ai/v1", ""
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None


def _try_openrouter() -> tuple[str, str, str, str, str] | None:
    """OpenRouter free model の設定を読み取る。"""
    key = os.environ.get("OPENROUTER_API_KEY") or ""
    if key:
        return (
            key,
            "https://openrouter.ai/api/v1",
            "",  # prefix: free はフルパスなので不要
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        )
    return None


# Config tuple: (api_key, base_url, prefix, model_primary, model_fallback)
_LLM_CONFIGS: list[tuple[str, str, str, str, str]] | None = None


def _get_llm_configs() -> list[tuple[str, str, str, str, str]]:
    """利用可能な全 API 設定。env → opencode-zen → OpenRouter (free)。"""
    global _LLM_CONFIGS
    if _LLM_CONFIGS is not None:
        return _LLM_CONFIGS

    configs: list[tuple[str, str, str, str, str]] = []

    # 環境変数による明示的指定
    env_key = os.environ.get("LLM_API_KEY") or ""
    env_base = os.environ.get("LLM_BASE_URL") or ""
    env_prefix = os.environ.get("LLM_MODEL_PREFIX") or ""
    if env_key and env_base:
        configs.append((
            env_key, env_base, env_prefix,
            os.environ.get("WEEKLY_LLM_STRUCTURE", _MODEL_DEFAULT),
            os.environ.get("WEEKLY_LLM_STRUCTURE_FB", _MODEL_DEFAULT_FB),
        ))

    # opencode-zen
    zen = _try_opencode_zen()
    if zen:
        z_key, z_base, z_prefix = zen
        configs.append((
            z_key, z_base, z_prefix,
            os.environ.get("WEEKLY_LLM_STRUCTURE", _MODEL_DEFAULT),
            os.environ.get("WEEKLY_LLM_STRUCTURE_FB", _MODEL_DEFAULT_FB),
        ))

    # OpenRouter (free models, model names 内蔵)
    or_ = _try_openrouter()
    if or_:
        configs.append(or_)

    _LLM_CONFIGS = configs
    return configs


def _resolve_model(model_name: str, prefix: str) -> str:
    """モデル名を解決。opencode-go/ プレフィックスを除去。"""
    name = model_name.removeprefix("opencode-go/").removeprefix("opencode/")
    if "/" in name:
        return name  # 既にフルパスの場合
    if prefix:
        return f"{prefix}{name}"
    return name


def _get_llm(model_name: str, config_idx: int = 0):
    from langchain_openai import ChatOpenAI

    configs = _get_llm_configs()
    if config_idx >= len(configs):
        raise RuntimeError(f"利用可能な API 設定がありません (config_idx={config_idx})")

    api_key, base_url, prefix, *_ = configs[config_idx]
    return ChatOpenAI(
        model=_resolve_model(model_name, prefix),
        api_key=api_key,
        base_url=base_url,
        temperature=0,
    )


def _format_task_list(issues: list[dict]) -> str:
    lines = []
    for i, iss in enumerate(issues, 1):
        desc = (iss.get("description") or "")[:200]
        lines.append(f"[{i}] title: {iss['title']} desc: {desc}")
    return "\n".join(lines)


def structure_node(state: PipelineState) -> dict:
    """LangGraph node: DB 上のタスクを LLM で優先度・見積もり・依存関係を構造化。"""
    created_ids: list[int] = state.get("created_ids", []) or []
    errors: list[str] = state.get("errors", []) or []
    if not created_ids:
        return {"structured_tasks": [], "errors": errors}

    issues = fetch_issues(created_ids)

    # 既に coding.task.db に入った issues だが、todo 系も混在しうる（簡易: repo/source で判定）
    # ここでは coding.task.db の issue テーブルしか参照しない設計とし、
    # todo 系は将来的に daily.task.db から別途 fetch する拡張とする。
    if not issues:
        return {
            "structured_tasks": [],
            "errors": errors + ["created_ids に該当する issue が見つかりません"],
        }

    # temp_id → db_id マッピング
    temp_to_db = {i + 1: iss["id"] for i, iss in enumerate(issues)}
    task_list_str = _format_task_list(issues)

    # LLM 実行
    raw_struct = _make_raw_struct_model()

    def _make_struct_wrapper(inner_model):
        """list[inner_model] をラップする Pydantic モデルを動的生成。"""
        from pydantic import BaseModel, Field

        class _StructListWrapper(BaseModel):
            items: list[inner_model] = Field(description="構造化されたタスクのリスト")

        return _StructListWrapper

    def _try_invoke(model_name: str, cfg_idx: int = 0) -> list | None:
        try:
            llm = _get_llm(model_name, config_idx=cfg_idx)
            wrapper = _make_struct_wrapper(raw_struct)
            structured = llm.with_structured_output(wrapper)
            import langchain_core.messages as _msg

            user_msg = (
                f"以下のタスクリストを構造化せよ。\n\n"
                f"タスクリスト:\n{task_list_str}\n\n"
                f"各タスクについて:\n"
                f"- priority: -2 〜 +2\n"
                f"- duration_min: 15/25/30/45/60/90/120 のいずれか\n"
                f"- dependencies: 依存先タスクの番号リスト(なければ空配列)\n"
                f"- due_date: 期日がある場合のみ (YYYY-MM-DD)\n\n"
                f"結果を items フィールドにタスクのリストとして返せ。"
            )
            result = structured.invoke(
                [_msg.SystemMessage(content=SYSTEM_PROMPT), _msg.HumanMessage(content=user_msg)]
            )
            if isinstance(result, wrapper):
                return result.items
            elif isinstance(result, dict):
                return result.get("items", [])
            return None
        except Exception as e:
            errors.append(f"LLM invoke cfg#{cfg_idx} {model_name}: {e}")
            return None

    result_raw = None
    configs = _get_llm_configs()
    for cfg_idx, cfg in enumerate(configs):
        _, _, _, m_primary, m_fallback = cfg
        for model_name in (m_primary, m_fallback):
            result_raw = _try_invoke(model_name, cfg_idx=cfg_idx)
            if result_raw is not None:
                break
        if result_raw is not None:
            break

    if not result_raw:
        return {"structured_tasks": [], "errors": errors}

    structured_tasks: list[StructuredTask] = []

    for raw in result_raw:
        # raw は _RawStruct Pydantic モデル
        temp_id = getattr(raw, "temp_id", None)
        if temp_id not in temp_to_db:
            continue
        db_id = temp_to_db[temp_id]
        iss = next((i for i in issues if i["id"] == db_id), None)
        if iss is None:
            continue

        priority = getattr(raw, "priority", 0)
        duration_min = getattr(raw, "duration_min", 30)
        dependencies_temp = getattr(raw, "dependencies", [])
        due_date = getattr(raw, "due_date", None)

        # DB 書き戻し
        db_type = "issue"  # coding.task.db の issue テーブル前提
        update_issue_struct(db_id, priority, duration_min, due_date)

        # 依存関係: dep_temp → db_id (parent → child)
        dep_db_ids: list[int] = []
        for dep_temp in dependencies_temp:
            dep_db = temp_to_db.get(dep_temp)
            if dep_db is None:
                continue
            dep_db_ids.append(dep_db)
            add_task_link(dep_db, db_id)

        structured_tasks.append(
            StructuredTask(
                db_id=db_id,
                db_type=db_type,
                title=iss["title"],
                priority=priority,
                duration_min=duration_min,
                dependencies=dep_db_ids,
                due_date=due_date,
                labels=[],
            )
        )

    return {"structured_tasks": structured_tasks, "errors": errors}