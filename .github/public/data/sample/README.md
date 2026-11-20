# 合成の会議記録（サンプル）

`adapter: dummy` の合成エージェントで作った会議記録です。実際の会議・実運用ログは含みません。

| ディレクトリ | 中身 |
|---|---|
| `normal/` | 正常系: 全員が答え、議長のまとめが自動承認されて終わる |
| `abnormal/` | 異常系: 軽い席（seat-L2）が相互批判で毎回失敗し、欠席として扱われる |

参加者は抽象ID（`chair-A` = 議長 tier-H、`seat-H1` = 重い席 tier-H、`seat-L1`・`seat-L2` = 軽い席 tier-L）だけで表しています。
`dm stats data/sample/*` や `dm export-md --meetings-dir data/sample` でそのまま読めます。
