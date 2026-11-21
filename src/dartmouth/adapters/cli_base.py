"""CLI 系エージェント（claude / gemini / agy）の共通部分。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .base import Adapter, AdapterError, Reply, TurnContext, run_subprocess


def parse_json_output(out: str) -> dict[str, Any]:
    """標準出力の JSON を取り出す（前後に余計な行があっても、最後の JSON オブジェクトを拾う）。"""
    out = out.strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        pass
    dec = json.JSONDecoder()
    found = None
    i = out.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(out, i)
            if isinstance(obj, dict):
                found = obj
            i = out.find("{", end)
        except json.JSONDecodeError:
            i = out.find("{", i + 1)
    if found is None:
        raise AdapterError(f"JSON出力を解析できません: {out[:300]!r}")
    return found


class CLIAdapter(Adapter):
    default_command: str = ""

    @property
    def command(self) -> str:
        return str(self.options.get("command", self.default_command))

    def build(self, ctx: TurnContext) -> tuple[list[str], str | None]:
        """(argv, stdin) を返す。"""
        raise NotImplementedError

    def parse(self, rc: int, out: str, err: str, ctx: TurnContext) -> Reply:
        raise NotImplementedError

    def env(self) -> dict[str, str]:
        return {}

    def tools_enabled(self, ctx: TurnContext) -> bool:
        """このラウンドで道具（ファイル読み込み・Web検索）を使わせるか。

        道具を使うと、エージェントは発言を書く前に何度も読み込みを繰り返し、そのたびに全文脈を送り直すので、
        時間とトークンが数倍になる。既定では、調べる必要がある独立回答と /ask だけで使わせ、
        相互批判とまとめ（議論の中身だけで足りる）では使わせない。options.tools_rounds で変えられる。
        """
        if ctx.round == "research":
            return True  # 議長の事前調査は Web 検索のためのラウンド（会議の tools: off とは別に使わせる）
        rounds = self.options.get("tools_rounds", ["independent", "interjection"])
        return ctx.round in rounds

    def workdir(self, ctx: TurnContext | None = None) -> str:
        """エージェントを動かす作業ディレクトリ。

        既定は参加者ごとの空ディレクトリ。会議ディレクトリ（transcript.jsonl に他の参加者の
        回答や非公開情報の原文がある）や非公開資料を、エージェントが読みに行けないようにする。
        読ませたい資料があるときだけ options.cwd で明示する。
        """
        # 事前調査では作業ディレクトリ（コード）を渡さない。調べるのは Web だけ
        if self.options.get("cwd") and (ctx is None or (self.tools_enabled(ctx) and ctx.round != "research")):
            return str(Path(self.options["cwd"]).expanduser())
        d = Path(tempfile.gettempdir()) / f"dartmouth-{os.getuid()}" / self.name
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d.parent, 0o700)
        return str(d)

    async def respond(self, ctx: TurnContext) -> Reply:
        argv, stdin = self.build(ctx)
        try:
            rc, out, err = await run_subprocess(
                argv,
                stdin=stdin,
                # エンジン側の wait_for より少し長めにして、kill はエンジンのキャンセルに任せる
                timeout_s=self.timeout_s + 5,
                cwd=self.workdir(ctx),
                env=self.env(),
                name=self.name,
            )
        finally:
            self.cleanup(ctx)
        return self.parse(rc, out, err, ctx)

    def cleanup(self, ctx: TurnContext) -> None:
        """この呼び出し（ctx）で作った一時ファイルを消す。同じ参加者への並行呼び出しのものは消さない。"""


def tail(s: str, n: int = 500) -> str:
    s = s.strip()
    return s[-n:] if len(s) > n else s
