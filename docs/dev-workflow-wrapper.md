# 開発ワークフロー T-001

## 台帳の実査

ローカルの `ledger.sqlite` を読み取り専用で調べた。スキーマは `meta.schema=1`。課題は `items.uid` が主キーで、同じ `local_id` は会議をまたいで現れ得る。実行指示は UID で照合し、`kind=todo`、`status=open`、`archived=0` を確認する。`status_note` と `status_manual` がある。試行ID・トークン・レビュー結果の専用テーブルはない。既存の `projects`、`project_alias`、`milestones`、`project_milestones`、`deps`、`items_fts`、`agenda_proposals` は現行用途に使われている。

実行ラッパーはファイル出力のみ。反映する場合は Antigravity が `attempt_id` による取り込み済み記録を管理し、既存の `dm ledger set` 相当の更新処理を本人承認後に使う。SQLite のスキーマ変更は初回試行に不要。

## 非対話 CLI

ローカルで確認したバージョンは Claude Code 2.1.328 と Codex CLI 0.170.0。下記は起動案で、モデルへの実リクエストは未実行。

| 項目 | Claude Code（実装） | Codex（レビュー） |
| --- | --- | --- |
| 実行 | worktree をカレントディレクトリにして `claude -p --model sonnet` | 別の空ディレクトリで `codex -a never exec --sandbox read-only --skip-git-repo-check -` |
| 権限 | `--permission-mode dontAsk` と必要最小限のツール許可。ファイル編集ツールに絞り、テストはラッパー側で実行。worktree 外を隔離するには OS 側の権限も使用 | `-a never` と `--sandbox read-only` |
| 上限 | `--max-budget-usd` は API コスト向け。CLI のヘルプではターン上限を確認できなかったため、最低限の強制停止は OS の経過時間制限で実装 | 調査した `exec --help` に時間・ターン・料金の上限は見当たらない。入力差分の上限と OS の経過時間制限を外側で強制 |
| ログ | `--output-format json`。実機試行で利用量フィールドと終了理由を確認 | `--json` でイベントを JSONL、`-o` で最終レビューを記録 |

[Claude Code CLI リファレンス](https://code.claude.com/docs/en/cli-reference) ／ [Codex の非対話実行](https://developers.openai.com/codex/noninteractive)

## 最小の直列ラッパー案

1. 手動実行で承認済みの ToDo UID、承認日時、基点コミット、完了条件とテストコマンドを受け取る。プロセス間ロックで1件だけ実行し、直近5時間にタイムアウト・レート制限が合計2件あれば新規着手を停止する。消費150kトークンは暫定警告として扱う。
2. 基点から1 ToDo につき1 worktree を切る。Claude Code をその中で時間制限付きで実行する。再試行は最大1回。試行ごとにログを分け、再開時は前の変更を把握した状態で続ける。タイムアウトはタイマー情報から判断し、明示的なレート制限以外の判定不能な停止は `unknown` とする。
3. テストを実行し、基点からの差分に新規ファイルも含める。差分が10kトークン超なら人手レビュー。範囲内なら ToDo 本文・完了条件・テスト結果・差分を標準入力に渡して、Codex を読み取り専用で実行する。
4. 試行ごとに `result.json` を一時ファイルから同じディレクトリ内で確定する。必須項目は `task_id`、`attempt_id`、`stop_reason`、`exit_code`、`tokens`、`retry`、`diff_lines`、`test_result`、`approved_at`。不明な利用量は null。失敗ログにも試行IDを持たせる。テスト通過と重大指摘0の人間確認後に、ユーザーの承認でマージする。
