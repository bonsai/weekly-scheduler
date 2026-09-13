# weekly-scheduler フロー

## パイプライン

```
gather → extract → create → structure → schedule
```

| Node | 処理 | ツール |
|------|------|--------|
| gather | opencode.db / GitHub Issues / issues.sqlite / coding.task.db / recap を統合 | SQLite, gh CLI, ファイル読込 |
| extract | 統合ドキュメントから ActionItem を LLM 抽出 | LangChain `with_structured_output` |
| create | ActionItem → tasque DB (daily.task.db / coding.task.db) + GitHub Issue | SQLite, gh CLI |
| structure | LLM で priority / duration / dependencies 推定 | LangChain `with_structured_output` |
| schedule | networkx bin packing で週間スケジュール配置 | networkx (topo sort + bin packing) |

## データソース

| ソース | 取得方法 | 内容 |
|--------|----------|------|
| opencode.db | SQLite (`~/.local/share/opencode/opencode.db`) | セッション履歴（タイトル・モデル・費用） |
| GitHub Issues | `gh issue list --repo bonsai/second-brains` | 未完了 Issue |
| issues.sqlite | SQLite (`~/repo/second-brains/db/issues.sqlite`) | 全ソース同期 Issue |
| coding.task.db | SQLite (`~/repo/second-brains/db/coding.task.db`) | tasque 未完了タスク |
| wiki/raw/recap-*.md | ファイル読込 (`~/wiki/raw/`) | 週次レキャップ |

## LLM 設定

チェーン: `LLM_API_KEY + LLM_BASE_URL (env)` → `opencode-zen (auth.json)` → `OpenRouter free`

モデル名は環境変数で上書き:

| 変数 | 用途 | デフォルト (OpenRouter) |
|------|------|------------------------|
| `WEEKLY_LLM_EXTRACT` | extract ノード用モデル | `nvidia/nemotron-3-ultra-550b-a55b:free` |
| `WEEKLY_LLM_EXTRACT_FB` | extract フォールバック | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` |
| `WEEKLY_LLM_STRUCTURE` | structure ノード用モデル | `nvidia/nemotron-3-ultra-550b-a55b:free` |
| `WEEKLY_LLM_STRUCTURE_FB` | structure フォールバック | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` |
| `LLM_API_KEY` | API キー | `$OPENROUTER_API_KEY` |
| `LLM_BASE_URL` | API ベース URL | `https://openrouter.ai/api/v1` |
| `LLM_MODEL_PREFIX` | モデル名プレフィックス | 未設定 |

`base_url` に `openrouter.ai` を含む場合、モデル名は自動的に OpenRouter 互換になる。
明示的に `WEEKLY_LLM_EXTRACT` を設定すれば任意のモデルに変更可能。

## 実行

```bash
cd ~/.hermes/skills/weekly-scheduler
LLM_API_KEY=$OPENROUTER_API_KEY LLM_BASE_URL=https://openrouter.ai/api/v1 \
  .venv/bin/python3 pipeline.py --days 14
```

### オプション

| 引数 | デフォルト | 説明 |
|------|-----------|------|
| `--days` | 7 | recap 対象日数 |
| `--run-id` | 現在時刻 | 実行 ID (checkpoint resume 用) |

## 出力フォーマット

### 1日 = 6h = 12コマ

```
09:00-12:00  ─┬─ tab 1  (09:00-09:30)
               ├─ tab 2  (09:30-10:00)
               ├─ tab 3  (10:00-10:30)
               ├─ tab 4  (10:30-11:00)
               ├─ tab 5  (11:00-11:30)
               └─ tab 6  (11:30-12:00)
12:00-13:00    昼休憩
13:00-16:00  ─┬─ tab 7  (13:00-13:30)
               ├─ tab 8  (13:30-14:00)
               ├─ tab 9  (14:00-14:30)
               ├─ tab 10 (14:30-15:00)
               ├─ tab 11 (15:00-15:30)
               └─ tab 12 (15:30-16:00)
```

### 表示例

```
■ 2026-08-03
  [  tab 1] 09:00-10:30  agents.db を復旧または移行する
  [  tab 4] 10:30-11:00  .auto-recap cron の内容を充実化する
```

### JSON 保存

`checkpoints/schedule-{run_id}.json` に保存 (Pydantic `model_dump`)

## ファイル構成

```
~/.hermes/skills/weekly-scheduler/
├── FLOW.md                 # ← 本ファイル (指示書フロー)
├── design.md               # 設計文書 (詳細)
├── SKILL.md                # hermes スキル定義
├── state.py                # Pydantic models + TypedDict State
├── pipeline.py             # LangGraph StateGraph 定義・実行
├── nodes/
│   ├── __init__.py
│   ├── gather.py           # 全データソース収集
│   ├── extract.py          # LLM 抽出
│   ├── create.py           # DB + GitHub Issue 作成
│   ├── structure.py        # LLM 構造化
│   └── schedule.py         # networkx スケジューリング
├── lib/
│   ├── db.py               # tasque DB 接続
│   ├── calendar.py         # GWS Calendar / schedule.db
│   └── recap.py            # recap ファイル発見
├── checkpoints/            # checkpoint DB + JSON 出力
├── references/
│   └── prompt-templates.md # LLM プロンプト集
└── requirements.txt
```

## 依存関係

```
langchain>=0.3, langchain-openai>=0.3, langgraph>=0.2
langgraph-checkpoint-sqlite>=2.0, networkx>=3.0, pydantic>=2.0
gh CLI (GitHub認証済み)
```
