"""入力欄のコマンド解釈。TUI と簡易表示で共通。

/edit /save /quit のように表示側の都合が絡むものは、Result.ui_action で表示側に任せる。
"""

from __future__ import annotations

from dataclasses import dataclass

from .flow import Engine

HELP = """コマンド（/ で始まらない入力は割り込み発言）
  /topic <議題>         議題を追加（議題待ちならすぐ開始）
  /next                 次のラウンドへ（Ctrl+N）
  /approve              議長のまとめを承認（Ctrl+A）
  /edit                 まとめを $EDITOR で修正してから承認
  /reject <理由>        却下して相互批判からやり直す
  /skip <参加者>         一時欠席   /join <参加者>  復帰
  /ask <参加者> <質問>   特定の参加者だけに質問
  /budget               予算の残りと呼び出し実績
  /stop                 実行中の発言を中断
  /retry /continue /abort  一時停止中の対応（再試行／飛ばして続行／中断）
  /end                  残りの議題を打ち切って総括へ
  /save  /quit          保存／保存して終了（resume で再開できます）"""


@dataclass
class Result:
    message: str = ""
    ui_action: str | None = None  # "edit" | "quit" | "save"
    level: str = "info"


def handle(engine: Engine, line: str) -> Result:
    line = line.strip()
    if not line:
        return Result()
    if not line.startswith("/"):
        engine.interject(line)
        return Result()
    head, _, rest = line[1:].partition(" ")
    cmd, rest = head.lower(), rest.strip()
    names = {n.lower(): n for n in engine.cfg.participants}
    for n, dn in engine.names.items():
        names.setdefault(dn.lower(), n)

    def who(s: str) -> str | None:
        return names.get(s.lower())

    match cmd:
        case "help" | "h" | "?":
            return Result(HELP)
        case "topic" | "t":
            if not rest:
                return Result("使い方: /topic <議題>", level="warning")
            engine.add_topic(rest)
            return Result(f"議題を追加しました: {rest}")
        case "next" | "n":
            return Result(engine.next())
        case "approve" | "a":
            return Result(engine.approve())
        case "edit" | "e":
            if engine.current_proposal() is None:
                return Result("承認待ちのまとめはありません", level="warning")
            return Result(ui_action="edit")
        case "reject" | "r":
            if not rest:
                return Result("使い方: /reject <理由>", level="warning")
            return Result(engine.reject(rest))
        case "skip" | "join":
            p = who(rest)
            if not p:
                return Result(f"参加者が見つかりません: {rest}", level="warning")
            return Result(engine.skip(p) if cmd == "skip" else engine.join(p))
        case "ask":
            try:
                target, question = rest.split(None, 1)
            except ValueError:
                return Result("使い方: /ask <参加者> <質問>", level="warning")
            p = who(target)
            if not p:
                return Result(f"参加者が見つかりません: {target}", level="warning")
            return Result(engine.ask(p, question))
        case "budget" | "b":
            return Result(engine.budget_report())
        case "stop":
            return Result(engine.stop())
        case "retry":
            return Result(engine.resolve_pause("retry"))
        case "continue" | "cont":
            return Result(engine.resolve_pause("skip"))
        case "abort":
            return Result(engine.resolve_pause("abort"))
        case "end":
            engine.end()
            return Result("今の議題が終わったら総括に進みます" if engine.cur else "総括に進みます")
        case "save":
            return Result("保存しました（発言は毎回ディスクに書いています）", ui_action="save")
        case "quit" | "q" | "exit":
            return Result(ui_action="quit")
        case _:
            return Result(f"不明なコマンド: /{cmd}（/help で一覧）", level="warning")

