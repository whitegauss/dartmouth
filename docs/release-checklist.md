# 公開前の固定監査チェックリスト（4層12項目）

公開ミラーへ出す前に、毎回この12項目を確かめる。10項目は `src/dartmouth/release_audit.py` が機械判定し、
残り2項目は [サインオフシート](release-signoff.md) で人が確かめる。

**公開してよい条件**（2026-11-19 experiments-3 D-005）: 二段階 CI（leakscan の generic と strict）のヒットが0件で、
下の12項目がすべて ✓（手動の2項目はサインオフ済み）であること。どれか1つでも欠けたら公開しない。

調べる対象は「公開ミラーに実際に置かれる形」のツリー。作業リポジトリの中身から `.github/workflows` を除き、
`.github/public/`（見本の設定 `config.example.yaml` と合成サンプル `data/sample/`）をルートへ移したもの。
見本の設定とサンプルは公開ミラーにだけ置き、作業リポジトリのルートには置かない。

| 層 | 番号 | 項目 | 判定 | どう確かめるか |
|---|---|---|---|---|
| メタデータ | M1 | タグ名が `release-vX.Y.Z` の形 | 機械 | タグ名の正規表現 |
| メタデータ | M2 | ファイル名・ディレクトリ名に個人の情報・固有語がない | 機械 | パスに leakscan の汎用パターンと固有語リストを当てる |
| メタデータ | M3 | 公開ミラーのコミット名義がプロジェクト共通名義（Dartmouth Experimenter） | **手動** | サインオフ 3 |
| 名前 | N1 | 汎用パターン（ホームのパス・メール・API キー・秘密鍵・Tailscale のアドレスなど）が0件 | 機械 | leakscan の汎用パターン（L2） |
| 名前 | N2 | 固有語リストのヒットが0件 | 機械 | leakscan の固有語リスト（L3、Secrets の `LEAKSCAN_TERMS`）。CI ではリストが無ければ ✗ |
| 名前 | N3 | `leakscan:ignore` が上限（5件）以内 | 機械 | 除外した行の数 |
| 構成 | C1 | 個人設定ファイル（`config.yaml`・`agents.yaml`・`*.local.yaml`・`pricing.yaml`・`configs/experiment.yaml`・固有語リストなど）がない | 機械 | ファイル名の一覧 |
| 構成 | C2 | `.gitignore` に個人用の除外（`config.yaml`・`agents.yaml`・`*.local.yaml`・`exports/`・`local/`・`meetings/*`）が全部ある | 機械 | `.gitignore` の行 |
| 構成 | C3 | 見本の設定（`*.example.yaml`）に実モデル名がない | 機械 | `model:` の値にモデル名の系統（claude・gemini・gpt など）が出ないこと |
| 実データ | D1 | 会議記録・台帳・書き出し（サンプル以外）がない | 機械 | `transcript.jsonl`（`data/sample/` 以外）・`*.sqlite`・`exports/`・`meetings/` の中身 |
| 実データ | D2 | サンプルは合成の2件（`normal`・`abnormal`）だけ | 機械 | `data/sample/` のディレクトリ（experiments-3 D-011） |
| 実データ | D3 | CI（leakscan generic / strict と、この監査）が全部合格している | **手動** | サインオフ 1 |

## 実行のしかた

```sh
python src/dartmouth/release_audit.py --worktree --strict --terms ~/dartmouth-personal/leak_terms.txt   # コミット前の確認
python src/dartmouth/release_audit.py --tag release-v0.1.0 --strict      # HEAD から組んで確認（CI と同じ判定）
```

公開ミラーへの同期（`.github/workflows/sync-public.yml`）は、`release-v*` のタグで動き、組み上げたツリーにこの監査を
`--strict` で掛ける。機械判定が1つでも ✗ なら、何も push せずに止まる（2026-11-20 D-005）。
