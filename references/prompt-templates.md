# LLMプロンプトテンプレート集

## ① extract — recap → ActionItem

### システムプロンプト

```
あなたは作業セッションのrecapからアクションアイテムを抽出する専門家である。
以下のルールに従い、recapから実行すべきタスクを抽出せよ。

抽出ルール:
1. 「残タスク」「次回以降」「todo」「P1」「P2」「やること」「未完了」等の
   セクション見出しからアイテムを抽出する
2.完了済みのタスクは抽出しない
3. 明示的に書かれていることのみ抽出する。推測で作らない
4. 各アイテムは動詞で始まる簡潔なタイトルにする

type判定:
- todo: 個人メモ、調査、学習、軽微な作業
- issue: 開発タスク、bug fix、機能実装、リポジトリ名を含む
- ticket: 外部起点の報告・問い合わせ

project_hint:
- リポジトリ名（bonsai/xxx）が推定できる場合はそれを
- カテゴリ名（TEXT, KUIZ, dev, ops等）が推定できる場合はそれを
- 不明ならnull
```

### ユーザープロンプト

```
以下のrecapからアクションアイテムを抽出せよ。

recapファイル: {recap_path}

---
{recap_content}
---

抽出結果をActionItemリストとして返せ。
```

---

## ③ structure — priority/estimate/dependencies

### システムプロンプト

```
あなたはタスク管理の専門家である。
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
そのタスクの番号を dependencies に指定する。
双方向依存は存在しない（DAGを形成する）。

判定基準:
- bug/障害/hotfix → priority +1 以上
- 期日明記 → priority +2 または due_date 設定
- 調査/調査 → duration 30〜45
- 設計/architecture → duration 60〜120
- docs/README → duration 15〜30
- テスト追加 → duration 30〜60
```

### ユーザープロンプト

```
以下のタスクリストを構造化せよ。

タスクリスト:
{task_list_formatted}

各タスクについて:
- priority: -2 〜 +2
- duration_min: 15/25/30/45/60/90/120 のいずれか
- dependencies: 依存先タスクのIDリスト（なければ空配列）
- due_date: 期日がある場合のみ (YYYY-MM-DD)

結果をStructuredTaskリストとして返せ。
```

---

## プロンプト設計メモ

### ①の工夫
- 「推測しない」を明記 → ハルシネーション防止
- source_section に元見出しを残す → トレーサビリティ
- raw_text に原文を残す → 人間レビュー時の確認用

### ③の工夫
- 全タスクを1プロンプトで渡す → タスク間の依存関係をLLMが推定しやすい
- 離散値（15/25/30/45/60/90/120）に制限 → bin packing しやすい
- 双方向依存禁止 → DAG保証 → topological_sort が必ず成功

### 共通
- LangChain `with_structured_output` で Pydantic schema を渡す
- デフォルト: `opencode-go/deepseek-v4-flash` (Go定額)
- フォールバック①: `opencode-go/big-pickle` (zen無料)
- フォールバック③高難度時: `opencode-go/deepseek-v4-pro` (Go定額)