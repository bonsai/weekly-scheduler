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

SYSTEM_PROMPT = """あなたは全データソース（セッション履歴・GitHub Issues・タスクDB・Recap）から
アクションアイテムを抽出する専門家である。

以下のルールに従い、**今週中に実行すべき全てのタスク**を漏れなく抽出せよ。

抽出ルール:
1. 以下のセクションから未完了・未着手のタスクを全て抽出する:
   - セッション履歴: 完了した作業は除外、言及された「次やる」「残」は抽出
   - GitHub Issues: 未着手の Issue はすべて抽出（更新日が新しいもの優先）
   - Issues DB / Tasque Tasks: status=open のものはすべて抽出
   - Recap: 「残タスク」「P1」「P2」「todo」等のセクションから抽出
2. 完了済み・クローズ済みのタスクは抽出しない
3. 各アイテムは動詞で始まる簡潔なタイトルにする
4. 数が多い場合も**全て抽出する**（週何もないのはおかしい）

type判定:
- todo: 個人メモ、調査、学習、軽微な作業、設定変更
- issue: 開発タスク、bug fix、機能実装、設計、リポジトリ名を含むコード作業
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

# OpenRouter 用デフォルトモデル (env 設定時、base_url が OpenRouter の場合)
_OPENROUTER_DEFAULT_PRIMARY = "nvidia/nemotron-3-ultra-550b-a55b:free"
_OPENROUTER_DEFAULT_FALLBACK = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"


def _is_openrouter(url: str) -> bool:
    """base_url が OpenRouter か判定。"""
    return "openrouter.ai" in url


def _get_llm_configs() -> list[tuple[str, str, str, str, str]]:
    """利用可能な全 API 設定。env → opencode-zen → OpenRouter (free)。

    LLM_API_KEY + LLM_BASE_URL が最優先。base_url が OpenRouter の場合は
    モデル名も OpenRouter 互換のものをデフォルトにする。
    """
    global _LLM_CONFIGS
    if _LLM_CONFIGS is not None:
        return _LLM_CONFIGS

    configs: list[tuple[str, str, str, str, str]] = []

    # 環境変数による明示的指定 (最優先)
    env_key = os.environ.get("LLM_API_KEY") or ""
    env_base = os.environ.get("LLM_BASE_URL") or ""
    env_prefix = os.environ.get("LLM_MODEL_PREFIX") or ""
    if env_key and env_base:
        if _is_openrouter(env_base):
            # OpenRouter 利用時: model 名は OpenRouter 互換のものをデフォルトに
            m_primary = os.environ.get("WEEKLY_LLM_EXTRACT", _OPENROUTER_DEFAULT_PRIMARY)
            m_fallback = os.environ.get("WEEKLY_LLM_EXTRACT_FB", _OPENROUTER_DEFAULT_FALLBACK)
        else:
            m_primary = os.environ.get("WEEKLY_LLM_EXTRACT", _MODEL_DEFAULT)
            m_fallback = os.environ.get("WEEKLY_LLM_EXTRACT_FB", _MODEL_DEFAULT)
        configs.append((
            env_key, env_base, env_prefix,
            m_primary, m_fallback,
        ))

    # opencode-zen (モデル名は WEEKLY_LLM_* 環境変数で上書き可能)
    zen = _try_opencode_zen()
    if zen:
        z_key, z_base, z_prefix = zen
        configs.append((
            z_key, z_base, z_prefix,
            os.environ.get("WEEKLY_LLM_EXTRACT", _MODEL_DEFAULT),
            os.environ.get("WEEKLY_LLM_EXTRACT_FB", _MODEL_DEFAULT),
        ))

    # OpenRouter free (環境変数 LLM_API_KEY/LLM_BASE_URL が未設定の場合のフォールバック)
    # 既に env 設定で OpenRouter が追加済みならスキップ (重複防止)
    already_has_or = any(_is_openrouter(c[1]) for c in configs)
    if not already_has_or:
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
    """LangGraph node: gathered_doc (全ソース統合) から ActionItem を抽出。

    優先的に ``gathered_doc`` を使い、なければ従来通り recap_paths を読む。
    """
    print(f"  [extract] starting...", flush=True)
    gathered_doc: str = state.get("gathered_doc", "") or ""
    recap_paths: list[str] = state.get("recap_paths", []) or []
    errors: list[str] = state.get("errors", []) or []

    # gathered_doc 優先
    if gathered_doc:
        content_source = gathered_doc
        source_label = "gathered_doc (全ソース統合)"
        print(f"  [extract] using gathered_doc ({len(gathered_doc)} bytes)", flush=True)
    elif recap_paths:
        # 従来互換: recap_paths からの読み取り
        content_source = None
        source_label = "recap_paths"
        print(f"  [extract] using recap_paths ({len(recap_paths)} files)", flush=True)
    else:
        print(f"  [extract] no data source, skipping", flush=True)
        return {"action_items": [], "errors": errors + ["データソースなし (skip extract)"]}

    # プロバイダ設定 (env → opencode-zen → OpenRouter) × モデル (primary → fallback) を総当たり
    structured: Any = None
    llm_errors: list[str] = []
    configs = _get_llm_configs()
    print(f"  [extract] available configs: {len(configs)}", flush=True)
    for cfg_idx, cfg in enumerate(configs):
        _, _, _, m_primary, m_fallback = cfg
        for model_name in (m_primary, m_fallback):
            print(f"  [extract] trying LLM cfg#{cfg_idx} model={model_name}", flush=True)
            try:
                llm = _get_llm(model_name, config_idx=cfg_idx)
                structured = llm.with_structured_output(_ActionItemList)
                print(f"  [extract] LLM ready: {model_name}", flush=True)
                break
            except Exception as e:
                llm_errors.append(f"LLM init cfg#{cfg_idx} {model_name}: {e}")
                print(f"  [extract] LLM init failed: {e}", flush=True)
                continue
        if structured is not None:
            break

    if structured is None:
        print(f"  [extract] NO LLM AVAILABLE, errors: {llm_errors}", flush=True)
        return {
            "action_items": [],
            "errors": errors + llm_errors,
        }

    items: list[ActionItem] = []

    if gathered_doc:
        # 統合ドキュメント全体から一括抽出
        print(f"  [extract] extracting from gathered_doc ({len(gathered_doc)} bytes)...", flush=True)
        try:
            extracted = _extract_one(structured, gathered_doc, source_label)
            items.extend(extracted)
            print(f"  [extract] {len(extracted)} items from gathered_doc", flush=True)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"  [extract] FAILED: {e}", flush=True)
            print(tb, flush=True)
            llm_errors.append(f"extract from gathered_doc: {e}")
    elif recap_paths:
        # 従来互換: recap ファイルごとに抽出
        for path in recap_paths:
            try:
                content = read_recap(path)
                filename = os.path.basename(path)
                print(f"  [extract] extracting from {filename}...", flush=True)
                extracted = _extract_one(structured, content, filename)
                items.extend(extracted)
                print(f"  [extract] {len(extracted)} items from {filename}", flush=True)
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                print(f"  [extract] FAILED {path}: {e}", flush=True)
                print(tb, flush=True)
                llm_errors.append(f"extract {path}: {e}")

    print(f"  [extract] total items: {len(items)}", flush=True)
    return {"action_items": items, "errors": errors + llm_errors}