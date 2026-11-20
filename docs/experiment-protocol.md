# Dartmouth 比較実験 Phase 1

議題3件と条件4件を組み合わせ、各組み合わせを1回ずつ実行する。本実験は計12セッション。先に議題1件と条件2件でパイロットを行う。議題の内容と正解リストは本人が確定する。

| 条件 | 参加者 | モデル系統 |
| --- | --- | --- |
| A | 高エフォート2名 | Gemini |
| B | 低エフォート4名 | Gemini |
| C | 高エフォート2名 | Claude |
| D | 低エフォート4名 | Claude |

各セッションは独立回答1ラウンドと相互批判2ラウンドで構成する。独立回答は最大4並列とし、相互批判は席順に直列で行う。最後に議長が議論を整理する。議長の割当ては各条件の `roles_standard` と `roles_reversed` から選ぶ。

## 実行

ローカルの `configs/experiment.yaml` を準備する。構造はテスト用の [YAML フィクスチャ](../tests/fixtures/experiment.yaml) を参照する。議題IDは台帳の計画行に合わせて文字列の `"1"`、`"2"`、`"3"` とする。本実験の順序は議題ID順、条件A〜Dの直積を `random.Random(20261119).shuffle` で固定する。パイロットの議題と条件は設定の `pilot_topic` と `pilot_conditions` で指定する。

- 模擬パイロットは `PYTHONPATH=src python -m experiments.runner --dry-run --pilot --config tests/fixtures/experiment.yaml`。
- パイロット実行時は `--live --pilot`、本実験の実行時は `--live`。実行前にサブスクリプションで認証済みの CLI とローカル設定を用意する。
- 逆順の議長割当てを使うには `--roles roles_reversed` を追加する。
- 初回が ABORTED のセッションは `--live --retry-session <ID>` で再実行する。設定を保持して行い、議長割当ては台帳の値を引き継ぐ。
- 本実験の実行後に `PYTHONPATH=src python -m experiments.validate_output` で条件ごとに最初の正常終了1件を抜き取る。確認用 Markdown は `local/experiment/review.md` に生成する。

## 記録と評価

[実行・記録表](experiment-sessions.csv) の計画行に対応する結果を1試行1行で追記する。既存の初回試行を上書きせず、再実行は新しいIDと `retry_of` を記録する。再実行は各セッション1回、実験全体で最大2セッション。初回合格率の母数には初回 ABORTED を含め、品質と消費の通常比較からは ABORTED を除外する。

総トークン数を消費指標とする。取得できない値は空欄にし、理由を `notes` に記録する。書式の補正とフォールバックは `format_error` に件数を記録する。公開範囲 `share_scope` は既定で `private` とし、明示されたものだけを `public` とする。実行時の台帳とローカル成果物は公開前の確認用データとして扱う。seat_history は公開しない。

抜取レビューでは、採用可能な決定事項または ToDo が1件以上あり、重大な見落としと規約違反がともに0件なら合格とする（D-011）。品質比較では、条件を伏せた最終議事録を事前の正解リストと照合し、議題・条件別に合致数と総トークンを記録する。具体的な品質差と消費比の採用閾値は本人確認後に固定する。

各ペルソナの版は `git hash-object prompts/personas/<id>.md` で記録する。検証は `python scripts/lint_personas.py` で行う。切替と停止は [実行時の安全ルール](experiment-safety.md) に従う。
