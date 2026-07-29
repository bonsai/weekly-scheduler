---
name: weekly-scheduler
description: >
  セッションrecapから週間スケジュールを自動生成する自律パイプライン。
  recap → issue作成 → 構造化 → networkx最適配置 → カレンダー登録。
  LangGraph StateGraph + LangChain structured_output + networkxヒューリスティック。
  hermes cron で日曜22時自動実行。チェックポイントで失敗リジューム対応。
license: MIT
compatibility: hermes
metadata:
  audience: developers
  trigger: "週間スケジュール", "weekly schedule", "recapから予定", "スケジュール生成"
---

## What I do

`wiki/raw/recap-MMDD.md` から action items を抽出し、
tasque DB (daily/coding) に Issue として投入、
LLM で priority/見積/依存を構造化し、
networkx で1週間の時間枠に最適配置して
GWS Calendar に登録する。

## Pipeline

```
① extract    recap → ActionItem[]        LangChain structured_output
② create     ActionItem → DB + gh issue  tasque DB, 冪等upsert
③ structure  DB → StructuredTask[]       LangChain, task_links更新
④ schedule   DAG → 1週間slots           networkx topo sort + bin packing
```

## State (LangGraph TypedDict)

```python
class PipelineState(TypedDict):
    recap_paths: list[str]
    action_items: list[ActionItem]
    created_ids: list[int]
    structured_tasks: list[StructuredTask]
    weekly_schedule: list[DaySchedule]
    errors: list[str]
    run_id: str
```

## Grammar

```
hermes run-skill weekly-scheduler
hermes run-skill weekly-scheduler --days 7
hermes run-skill weekly-scheduler --resume <run_id>
```

## DB

| DB | 場所 | 用途 |
|----|------|------|
| daily.task.db | `repo/second-brains/db/` | todo |
| coding.task.db | `repo/second-brains/db/` | issue + task_links |
| checkpoints/weekly.db | 本スキル内 | LangGraph checkpointer |

## Cron

```json
{
  "name": "weekly-scheduler",
  "schedule": "0 22 * * 0",
  "skill": "weekly-scheduler",
  "prompt": "先週のrecapから週間スケジュールを生成し、カレンダーに登録せよ"
}
```

## LLM 割当

| ノード | デフォルト | フォールバック |
|--------|-----------|---------------|
| ①extract | `opencode-go/deepseek-v4-flash` (Go定額) | `opencode-go/big-pickle` (zen無料) |
| ②create | — (LLM不要) | — |
| ③structure | `opencode-go/deepseek-v4-flash` (Go定額) | `opencode-go/deepseek-v4-pro` (Go定額) |
| ④schedule | — (LLM不要) | — |

## Dependencies

```
langchain>=0.3
langgraph>=0.2
langgraph-checkpoint-sqlite>=2.0
networkx>=3.0
pydantic>=2.0
```

## 参照

- [設計文書](design.md) — パイプライン詳細・State schema・アルゴリズム
- [プロンプトテンプレート](references/prompt-templates.md) — LLMプロンプト集
- [tasque SKILL.md](../tasque/SKILL.md) — 3DB schema
- [mitoのtimer.py](../../../agent/mito/timer.py) — timebox管理
- [MITO_AGENTS.md](../../../wiki/agent/MITO_AGENTS.md) — GWS連携

## 統合先

```
wiki/raw/recap-MMDD.md  →  (this skill)  →  GWS Calendar + schedule.db
                                          →  tasque DB (daily/coding)
                                          →  checkpoints/weekly.db
```

## 環境変数

| 変数 | 用途 |
|------|------|
| `DEEPSEEK_API_KEY` | LangChain LLM (kimi-k3 via opencode-go) |
| `OPENROUTER_API_KEY` | フォールバックLLM |