"""extract.py — ① recap → ActionItem[]

LangChain ``with_structured_output`` で Pydantic 型検証付き抽出。
デフォルト LLM は opencode-go/deepseek-v4-flash (Go定額)。
失敗時は opencode-go/big-pickle (zen無料) にフォールバック。

環境変数:
  OPENCODE_GO_API_KEY   — opencode-go APIキー
  OPENCODE_GO_BASE_URL  — opencode-go base URL (default: https://api.opencode-go.com/v1)

LangChain v0.3 API:
  llm.with_structured_output(List[Model]) -> structured_output
  structured_output.invoke([SystemMessage, HumanMessage])
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# 親ディレクトリ (skill root) を sys.path に追加
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

from pydantic import BaseModel

from state import ActionItem, PipelineState  # noqa: E402
from lib.recap import read_recap  # noqa: E402

# with_structured_output 用ラッパー (list[ActionItem] そのままでは非対応のため)
class _ActionItemList(BaseModel):
    items: list[ActionItem]

# モデル名は環境変数で上書き可能。
# opencode-zen 時: opencode-go/deepseek-v4-flash (prefix "")
# OpenRouter free 時: nvidia/nemotron-3-ultra-550b-a55b:free / nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free
_MODEL_DEFAULT = "opencode-go/deepseek-v4-flash"
MODEL_PRIMARY = os.environ.get("WEEKLY_LLM_EXTRACT", _MODEL_DEFAULT)
MODEL_FALLBACK = os.environ.get("WEEKLY_LLM_EXTRACT_FB", _MODEL_DEFAULT)

SYSTEM_PROMPT = """あなたは作業セッションのrecapからアクションアイテムを抽出する専門家である。
以下のルールに従い、recapから実行すべきタスクを抽出せよ。

抽出ルール:
1. 「残タスク」「次回以降」「todo」「P1」「P2」「やること」「未完了」「残タスク」「次にやる」等の
   セクション見出しからアイテムを抽出する
2. 完了済みのタスクは抽出しない
3. 明示的に書かれていることのみ抽出する。推測で作らない
4. 各アイテムは動詞で始まる簡潔なタイトルにする

type判定:
- todo: 個人メモ、調査、学習、軽微な作業
- issue: 開発タスク、bug fix、機能実装、リポジトリ名を含む
- ticket: 外部起点の報告・問い合わせ

project_hint:
- リポジトリ名 (bonsai/xxx) が推定できる場合はそれを
- カテゴリ名 (TEXT/KUIZ/dev/ops) が推定できる場合はそれを
- 不明なら null
"""


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


def _resolve_model(model_name: str, prefix: str) -> str:
    """モデル名を解決。opencode-go/ プレフィックスを除去。"""
    name = model_name.removeprefix("opencode-go/").removeprefix("opencode/")
    if "/" in name:
        return name  # 既にフルパスの場合
    if prefix:
        return f"{prefix}{name}"
    return name


# Config tuple: (api_key, base_url, prefix, model_primary, model_fallback)
_LLM_CONFIGS: list[tuple[str, str, str, str, str]] | None = None


def _get_llm_configs() -> list[tuple[str, str, str, str, str]]:
    """利用可能な全 API 設定。env → opencode-zen → OpenRouter (free)。"""
    global _LLM_CONFIGS
    if _LLM_CONFIGS is not None:
        return _LLM_CONFIGS

    configs: list[tuple[str, str, str, str, str]] = []

    # 環境変数による明示的指定 (最優先)
    env_key = os.environ.get("LLM_API_KEY") or ""
    env_base = os.environ.get("LLM_BASE_URL") or ""
    env_prefix = os.environ.get("LLM_MODEL_PREFIX") or ""
    if env_key and env_base:
        configs.append((
            env_key, env_base, env_prefix,
            os.environ.get("WEEKLY_LLM_EXTRACT", _MODEL_DEFAULT),
            os.environ.get("WEEKLY_LLM_EXTRACT_FB", _MODEL_DEFAULT),
        ))

    # opencode-zen (model_default は env か _MODEL_DEFAULT)
    zen = _try_opencode_zen()
    if zen:
        z_key, z_base, z_prefix = zen
        configs.append((
            z_key, z_base, z_prefix,
            os.environ.get("WEEKLY_LLM_EXTRACT", _MODEL_DEFAULT),
            os.environ.get("WEEKLY_LLM_EXTRACT_FB", _MODEL_DEFAULT),
        ))

    # OpenRouter (free models, model_primary/_fallback 内蔵)
    or_ = _try_openrouter()
    if or_:
        configs.append(or_)

    _LLM_CONFIGS = configs
    return configs


def _get_llm(model_name: str, config_idx: int = 0):
    """LangChain用 ChatOpenAI を構築。"""
    from langchain_openai import ChatOpenAI

    configs = _get_llm_configs()
    if config_idx >= len(configs):
        raise RuntimeError(f"利用可能な API 設定がありません (config_idx={config_idx})")

    api_key, base_url, prefix, *_ = configs[config_idx]
    final_model = _resolve_model(model_name, prefix)
    return ChatOpenAI(
        model=final_model,
        api_key=api_key,
        base_url=base_url,
        temperature=0,
    )


def _extract_one(structured, content: str, filename: str) -> list[ActionItem]:
    """1つのrecapファイルを処理して ActionItem リストを返す。"""
    user_msg = (
        f"以下のrecapからアクションアイテムを抽出せよ。\n\n"
        f"recapファイル: {filename}\n"
        f"---\n{content}\n---\n\n"
        f"抽出結果を ActionItem のリストとして返せ。"
    )
    from langchain_core.messages import HumanMessage, SystemMessage

    result = structured.invoke(
        [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user_msg)]
    )
    # _ActionItemList.items から取り出し
    if isinstance(result, _ActionItemList):
        items = result.items
    elif isinstance(result, dict):
        items = result.get("items", [])
    elif isinstance(result, list):
        items = [i for i in result if isinstance(i, ActionItem)]
    else:
        items = []
    # source_recap 補完
    for it in items:
        if not it.source_recap:
            it.source_recap = filename
    return items


def extract_node(state: PipelineState) -> dict:
    """LangGraph node: recap ファイル群から ActionItem を抽出。"""
    recap_paths: list[str] = state.get("recap_paths", []) or []
    errors: list[str] = state.get("errors", []) or []
    if not recap_paths:
        return {"action_items": [], "errors": errors + ["recap ファイルなし (skip extract)"]}

    # プロバイダ設定 (env → opencode-zen → OpenRouter) × モデル (primary → fallback) を総当たり
    structured: Any = None
    llm_errors: list[str] = []
    configs = _get_llm_configs()
    for cfg_idx, cfg in enumerate(configs):
        _, _, _, m_primary, m_fallback = cfg
        for model_name in (m_primary, m_fallback):
            try:
                llm = _get_llm(model_name, config_idx=cfg_idx)
                structured = llm.with_structured_output(_ActionItemList)
                break
            except Exception as e:
                llm_errors.append(f"LLM init cfg#{cfg_idx} {model_name}: {e}")
                continue
        if structured is not None:
            break

    if structured is None:
        return {
            "action_items": [],
            "errors": errors + llm_errors,
        }

    items: list[ActionItem] = []
    for path in recap_paths:
        try:
            content = read_recap(path)
            filename = os.path.basename(path)
            extracted = _extract_one(structured, content, filename)
            items.extend(extracted)
        except Exception as e:
            llm_errors.append(f"extract {path}: {e}")

    return {"action_items": items, "errors": errors + llm_errors}