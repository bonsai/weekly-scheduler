# weekly-scheduler — 設計文書

> セッションrecap → issue作成 → 構造化 → 週間スケジュール生成
> 自律化パイプライン（hermes cron + LangGraph + networkx）

---

## 1. 概要・目的

### 問題
- opencode セッションの振り返り（recap）は `wiki/raw/recap-MMDD.md` に手動保存されるが、そこから**アクションに繋がらない**
- タスクは tasque の3DB（daily/coding/business）に管理されるが、**recapとDBが接続されていない**
- スケジュールは mito の timebox + GWS Calendar に登録されるが、**手動で時間枠を割り当てる必要がある**

### 解決
recapから「やるべきこと」を自動抽出 → GitHub Issue / DB に投入 → priority/見積/依存を構造化 → networkx で1週間の時間枠に最適配置 → カレンダー登録。

### 設計原則
- **自律化**: 人間の介入なしで日曜夜にcron実行。失敗時はチェックポイントからリジューム
- **既存資産を活かす**: tasque DB schema、mito timer/ingest、recap形式をそのまま使う
- **LLMは必要な所だけ**: ①抽出と③構造化はLLM。②投入と④スケジューリングはアルゴリズム
- **on-device / on-cloud 分離**: wiki（ローカル）から抽出 → second-brains（クラウド）のDBに投入 → スケジュールはローカル+GWS

---

## 2. パイプライン全体図

```
                      wiki/raw/recap-MMDD.md (on device)
                              │
                    ┌─────────▼─────────┐
              ①     │   extract          │  LangChain structured_output
                    │   recap → items    │  Pydantic検証
                    └─────────┬──────────┘
                              │
                    ┌─────────▼─────────┐
              ②     │   create_issues   │  tasque DB + gh issue create
                    │   items → DB      │  冪等upsert
                    └─────────┬──────────┘
                              │
                    ┌─────────▼─────────┐
              ③     │   structure       │  LangChain structured_output
                    │   priority/est/dep│  task_links更新
                    └─────────┬──────────┘
                              │
                    ┌─────────▼─────────┐
              ④     │   schedule        │  networkx topo sort + bin packing
                    │   DAG → 1週間slots │  GWS Calendar登録
                    └─────────┬──────────┘
                              │
                    weekly_schedule.json
                    + GWS Calendar events
```

### データフロー

```
wiki/raw/        →   ActionItem[]    →   DB issues + task_links   →   StructuredTask[]   →   weekly_schedule
(recap)              (LangChain)         (tasque DB)                  (LangChain)            (networkx)
```

---

## 3. State Schema

```python
from typing import TypedDict
from pydantic import BaseModel

class ActionItem(BaseModel):
    """①の抽出単位"""
    title: str
    type: Literal["todo", "issue", "ticket"]
    project_hint: str | None       # リポジトリ or カテゴリ推定
    source_recap: str              # 元recapファイル名
    source_section: str            # recap内の見出し
    raw_text: str                  # 抽出元原文

class StructuredTask(BaseModel):
    """③の構造化単位"""
    db_id: int                     # tasque DB内のissue.id or todo.id
    db_type: Literal["todo", "issue"]
    title: str
    priority: int                  # -2..2 (tasque準拠)
    duration_min: int              # 見積もり分数
    dependencies: list[int]        # 依存先のdb_id
    due_date: str | None           # ISO8601
    labels: list[str]

class DaySchedule(BaseModel):
    """④の1日分"""
    date: str                      # YYYY-MM-DD
    tasks: list[TimeSlot]

class TimeSlot(BaseModel):
    """④の時間枠"""
    db_id: int
    title: str
    start: str                     # HH:MM
    end: str                       # HH:MM
    depends_on: list[int]          # 前提タスクのdb_id

class PipelineState(TypedDict):
    """LangGraph全体状態"""
    recap_paths: list[str]
    action_items: list[ActionItem]
    created_ids: list[int]         # DB挿入後のID
    structured_tasks: list[StructuredTask]
    weekly_schedule: list[DaySchedule]
    errors: list[str]
    run_id: str                    # checkpoint用の一意ID
```

---

## 4. 各ノード詳細

### ① extract — recap → ActionItem

| 項目 | 内容 |
|------|------|
| 入力 | `recap_paths: list[str]` |
| 出力 | `action_items: list[ActionItem]` |
| ツール | LangChain `with_structured_output(list[ActionItem])` |
| LLM | `opencode-go/deepseek-v4-flash` (Go定額) / fallback: `opencode-go/big-pickle` (zen無料) |

**処理**:
1. recap ファイル群を読み込み
2. セクション毎に「残タスク」「次回やること」「P1/P2」等の見出しからアクション抽出
3. Pydanticで型検証
4. `type` 判定: repo名含む → issue、個人メモ → todo

**プロンプト設計**:
```
以下は作業セッションのrecapである。
「残タスク」「次回以降」「todo」「P1/P2」等のセクションから、
実行すべきアクションアイテムを抽出せよ。

各アイテムについて:
- title: 簡潔なタイトル（動詞始まり）
- type: "todo"(個人メモ) | "issue"(開発タスク) | "ticket"(外部起点)
- project_hint: リポジトリ名 or カテゴリ（推定可）
- source_section: 抽出元の見出し

推測しない。明示的に書かれていることのみ抽出する。
```

### ② create_issues — ActionItem → DB

| 項目 | 内容 |
|------|------|
| 入力 | `action_items: list[ActionItem]` |
| 出力 | `created_ids: list[int]` |
| ツール | sqlite3, gh CLI |

**処理**:
1. 各ActionItemごとにDB判定:
   - `type=todo` → `daily.task.db` の `todo` テーブルにINSERT
   - `type=issue` → `gh issue create` → `coding.task.db` の `issue` テーブルにINSERT
   - `type=ticket` → `coding.task.db` の `issue` (type='ticket') にINSERT
2. **冪等性**: `source_id` に `recap:<filename>:<hash>` を設定。同一 `source_id` が存在すればスキップ
3. 作成されたDB IDを `created_ids` に蓄積

**DB配置** (second-brains/cloud):
```
db/
├── daily.task.db         # todo
├── coding.task.db        # issue + task_links
└── business.task.db      # (当面使用しない)
```

**task_links テーブル** (coding.task.db に新規追加):
```sql
CREATE TABLE IF NOT EXISTS task_links (
    parent_id INTEGER REFERENCES issue(id) ON DELETE CASCADE,
    child_id  INTEGER REFERENCES issue(id) ON DELETE CASCADE,
    PRIMARY KEY (parent_id, child_id)
);
```

### ③ structure — priority/estimate/dependencies

| 項目 | 内容 |
|------|------|
| 入力 | `created_ids: list[int]` |
| 出力 | `structured_tasks: list[StructuredTask]` |
| ツール | LangChain `with_structured_output`, sqlite3 |
| LLM | `opencode-go/deepseek-v4-flash` (Go定額) / fallback: `opencode-go/deepseek-v4-pro` (高難度時) |

**処理**:
1. `created_ids` からDB経由で全タスクのtitle/description/labelsを取得
2. **バッチ構造化**: 全タスクを1プロンプトで渡し、以下を推定:
   - `priority`: -2..2 (tasque準拠。緊急+2 / 高+1 / 既定0 / 低-1 / 任意-2)
   - `duration_min`: 15/25/30/45/60/90/120 から推定
   - `dependencies`: 他タスクのtitleを参照して依存関係を推定
3. 結果をDBに書き戻し:
   - `issue.priority` / `todo.priority` 更新
   - `issue.duration_min` / `todo.duration_min` 更新
   - `task_links` に依存関係INSERT
4. `StructuredTask` リストを生成

**プロンプト設計**:
```
以下のタスクリストについて、各タスクの優先度・見積もり時間・依存関係を推定せよ。

優先度スケール:
  +2: 緊急（今日必須）
  +1: 高（今週中）
   0: 既定
  -1: 低
  -2: やらなくてもいい

見積もり: 15/25/30/45/60/90/120 分のいずれか

依存関係: 他のタスクが完了してから着手すべき場合、そのタスク番号を指定。

タスクリスト:
  [1] title: ... desc: ...
  [2] title: ... desc: ...
```

### ④ schedule — DAG → 1週間slots

| 項目 | 内容 |
|------|------|
| 入力 | `structured_tasks: list[StructuredTask]` |
| 出力 | `weekly_schedule: list[DaySchedule]` |
| ツール | networkx, GWS Calendar API (mito MCP) |

**処理**:
1. `networkx.DiGraph` 構築:
   - ノード = StructuredTask
   - エッジ = dependencies (parent → child)
2. `topological_sort` で実行可能順を取得
3. **優先度付き bin packing**:
   - 1日 = 480分 (8時間)
   - 1週間 = 5日 (月-金) = 2400分
   - スコア = `priority * 100 - duration_min`
   - high priority を早期日に配置
4. **制約チェック**:
   - 依存タスクは必ず前日以前に配置
   - 同日の合計が480分を超えない
   - `due_date` があるタスクは該当日以前に配置
5. `DaySchedule` リストを生成
6. GWS Calendar に events 登録（mito MCP `mito_create_event` 経由）

**ヒューリスティックアルゴリズム**:
```python
def schedule_week(tasks: list[StructuredTask]) -> list[DaySchedule]:
    G = build_dag(tasks)
    order = list(nx.topological_sort(G))
    # 優先度でソート（トポロジカル順序を保ちつつ）
    order = stable_sort_by_priority(order, tasks)
    
    days = [{date: d, tasks: []} for d in WEEK_DAYS]
    day_minutes = [0] * 5
    
    for tid in order:
        task = tasks_by_id[tid]
        dep_day = max_dep_completion_day(tid, days)
        for i in range(dep_day, 5):
            if day_minutes[i] + task.duration_min <= DAILY_CAP:
                assign(days[i], task)
                day_minutes[i] += task.duration_min
                break
        else:
            # 今週入りきらない → 余った分を翌週 or 今日残りに
            overflow(days, task)
    
    return days
```

---

## 5. 既存assetsとの統合

### recap (wiki/raw/)
```
入力ソース:
  ~/wiki/raw/recap-0729.md
  ~/wiki/raw/recap-0722.md
  ~/wiki/raw/0630.md  etc.

処理対象:
  直近7日以内のrecapファイル（cron実行日に基づく）
```

### tasque (3DB)
```
出力先:
  daily.task.db    ← type=todo
  coding.task.db   ← type=issue/ticket + task_links
  business.task.db  ← (将来: 顧客・収益付き)

冪等キー: source_id = "recap:recap-0729.md:<sha8>"
```

### mito (timer + GWS)
```
連携:
  timer.py         ← ④のスケジュール結果を timebox に事前登録
  GWS MCP          ← mito_create_event で Calendar に events 発行
  gen_ics.py       ← ICS出力（オプション）
```

### hermes cron (jobs.json)
```
エントリ追加:
  name: "weekly-scheduler"
  schedule: "0 22 * * 0"  (毎週日曜22時 JST)
  skill: "weekly-scheduler"
  prompt: "先週のrecapから週間スケジュールを生成し、カレンダーに登録せよ"
```

### second-brains (cloud)
```
DB配置:
  ~/repo/second-brains/db/daily.task.db
  ~/repo/second-brains/db/coding.task.db

  git push でクラウド同期（不定期的）
```

---

## 6. 自律化戦略

### hermes cron連動

```
              日曜22時
                 │
        ┌────────▼────────┐
        │  hermes cron     │  jobs.json trigger
        │  run-skill       │
        └────────┬─────────┘
                 │
        ┌────────▼─────────┐
        │  pipeline.py      │  LangGraph StateGraph
        │  ├ ① extract     │
        │  ├ ② create      │  失敗時: checkpoint保存
        │  ├ ③ structure  │  エラーを state["errors"] に蓄積
        │  └ ④ schedule   │
        └────────┬─────────┘
                 │
        ┌────────▼─────────┐
        │  weekly_schedule  │  JSON出力 + GWS Calendar
        │  .json            │
        └──────────────────┘
```

### チェックポイント・リジューム

```
checkpoints/weekly.db (SQLite)

flow:
  1. cron起動 → run_id = "2026-07-29T22:00"
  2. pipeline開始 → 各node完了時にcheckpoint保存
  3. ③でLLM timeout → checkpointに状態保存
  4. 月曜8時のcron → run_id復元 → ③から再開
```

### エラー処理

| エラー | 動作 |
|--------|------|
| recap ファイルなし | ①スキップ → END (正常終了) |
| ② DB重複 | source_idでupsert成功 → 続行 |
| ③ LLM timeout | checkpoint保存 → 翌cronでresume |
| ④ 容量オーバー | 当週に収まらない分は「翌週バックログ」へ |
| GWS認証エラー | ローカル schedule.db にフォールバック |

### 冪等性設計

- ②: `source_id` の一意制約で重複回避
- ③: 既に priority/duration が設定済みのタスクはスキップ
- ④: 既存カレンダーイベント（descriptionにrun_id埋め込み）は上書き

---

## 7. LangGraph StateGraph設計

### グラフ定義

```python
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

graph = StateGraph(PipelineState)

graph.add_node("extract",   extract_node)
graph.add_node("create",    create_node)
graph.add_node("structure", structure_node)
graph.add_node("schedule",  schedule_node)

graph.add_edge(START, "extract")

# ①で0件なら終了
graph.add_conditional_edges(
    "extract",
    lambda s: "create" if s["action_items"] else END,
)

graph.add_edge("create", "structure")
graph.add_edge("structure", "schedule")
graph.add_edge("schedule", END)

checkpointer = SqliteSaver.from_conn_string(
    "checkpoints/weekly.db"
)
app = graph.compile(checkpointer=checkpointer)
```

### 実行

```python
config = {"configurable": {"thread_id": run_id}}
result = app.invoke(
    {"recap_paths": find_recaps(days=7)},
    config=config
)
```

### 条件分岐（将来拡張用）

```python
# ④の結果が容量オーバー → ③に戻って見積もりを削る
graph.add_conditional_edges(
    "schedule",
    lambda s: "structure" if s.get("overflow") else END,
)
```

---

## 8. ファイル構成

```
~/.hermes/skills/weekly-scheduler/
├── SKILL.md                      # hermes用スキル定義
├── design.md                     # 本設計文書
├── state.py                      # Pydantic models + TypedDict State
├── pipeline.py                   # LangGraph StateGraph定義・実行
├── nodes/
│   ├── __init__.py
│   ├── extract.py                # ① recap → ActionItem
│   ├── create.py                 # ② ActionItem → DB
│   ├── structure.py              # ③ DB → StructuredTask
│   └── schedule.py              # ④ StructuredTask → DaySchedule
├── lib/
│   ├── db.py                     # tasque DB接続・task_links管理
│   ├── calendar.py               # GWS Calendar / schedule.db fallback
│   └── recap.py                  # recapファイル発見・読み込み
├── checkpoints/
│   └── weekly.db                 # SQLite checkpointer
└── references/
    └── prompt-templates.md       # LLMプロンプトテンプレート集
```

---

## 9. LLM 割当

| ノード | デフォルト | フォールバック | 根拠 |
|--------|-----------|---------------|------|
| ①extract | `opencode-go/deepseek-v4-flash` (Go定額) | `opencode-go/big-pickle` (zen無料) | Pydantic型抽出はmid品質で十分。Go定額で完全無料 |
| ②create | — (LLM不要) | — | sqlite3 + gh CLI のみ |
| ③structure | `opencode-go/deepseek-v4-flash` (Go定額) | `opencode-go/deepseek-v4-pro` (Go定額) | 多タスク1プロンプト推定。品質不足時のみv4-proに昇格 |
| ④schedule | — (LLM不要) | — | networkx ヒューリスティックのみ |

### モデルスペック（models.jsonより）

| provider/model | 課金 | low | mid | high | code |
|---|---|---|---|---|---|
| opencode-zen/big-pickle | zen無料 | 8 | 8 | 8 | 8 |
| opencode-zen/deepseek-v4-flash-free | zen無料 | 7 | 8 | 7 | 8 |
| opencode-zen/deepseek-v4-flash | Go定額 | 9 | 9 | 8 | 9 |
| opencode-zen/deepseek-v4-pro | Go定額 | 8 | 9 | 10 | 9 |
| sakura/preview/Kimi-K2.7-Code | sakura従量 | 7 | 8 | 8 | 10 |

### LangChain provider 設定

```python
# opencode-go provider (OpenAI互換)
from langchain_openai import ChatOpenAI

llm_primary = ChatOpenAI(
    model="opencode-go/deepseek-v4-flash",
    api_key=os.environ["OPENCODE_GO_API_KEY"],
    base_url="https://api.opencode-go.com/v1",  # TODO: 実際のURL確認
)

llm_fallback = ChatOpenAI(
    model="opencode-go/big-pickle",
    api_key=os.environ["OPENCODE_GO_API_KEY"],
    base_url="https://api.opencode-go.com/v1",
)
```

## 10. 依存ライブラリ

```
# requirements.txt (hermes venv 追加)
langchain>=0.3
langgraph>=0.2
langgraph-checkpoint-sqlite>=2.0
networkx>=3.0
pydantic>=2.0
# 既存: sqlite3, gh CLI
```

学習コスト:
- LangGraph: StateGraph, add_node, add_edge, compile, checkpointer（4概念）
- LangChain: with_structured_output（1パターン）
- networkx: DiGraph, topological_sort（2関数）

---

## 11. 将来拡張

### OR-Tools による厳密最適化 (Phase 2)
- CLP（制約線形計画）で1週間の最適配置を解く
- 「タスクAは午前中」「1日に最大3タスク」等のソフト制約
- OR-Tools CP-SAT solver: `model.Add(...)` で制約記述

### human-in-the-loop (Phase 2)
- ②の後に `interrupt_before` で人間がissue確認
- ブラウザUI（FastAPI + React）で編集 → resume

### refiner (Phase 2)
- ③の結果をLLM自己評価: `priority` と `duration_min` が妥当か
- 低信頼度なら別モデル（grok-4.5）で再推定

### business.task.db統合 (Phase 3)
- 顧客・収益情報付きタスクの自動分類
- 収益が大きい案件は priority bonus

### multi-week lookahead (Phase 3)
- 今週収まらないタスクを翌週バックログに送る
- 2-3週間のガントチャート生成