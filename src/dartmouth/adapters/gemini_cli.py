"""Gemini CLI（gemini -p）アダプタ。

注意: Gemini CLI は 2026年6月に個人向けの提供を終了し、Antigravity CLI（agy）に統合された。
通常は antigravity_cli アダプタで model に Gemini を指定する。API キーで旧 CLI を使い続ける場合のために残している。

確認したフラグ（gemini --help, v0.68.0）:
  -p（標準入力の後ろに追記される） / -o json / --approval-mode plan（読み取り専用） / --skip-trust / -m
自動承認（--yolo）は使わない。プロンプトは標準入力で渡す。

options: model, cwd, command
"""

from __future__ import annotations

from .base import AdapterError, Reply, TurnContext
from .cli_base import CLIAdapter, parse_json_output, tail

INSTRUCTION = "上の内容（会議の文脈と今回の指示）に従って、あなたの発言だけを書いてください。"


class GeminiCLIAdapter(CLIAdapter):
    default_command = "gemini"

    def env(self) -> dict[str, str]:
        # 認証が切れているときにブラウザを開こうとして止まらないようにする
        return {"NO_BROWSER": "true"}

    def build(self, ctx: TurnContext) -> tuple[list[str], str | None]:
        argv = [self.command, "-p", INSTRUCTION, "-o", "json", "--approval-mode", "plan", "--skip-trust"]
        if self.options.get("model"):
            argv += ["-m", str(self.options["model"])]
        return argv, ctx.prompt

    def parse(self, rc: int, out: str, err: str, ctx: TurnContext) -> Reply:
        if not out.strip():
            raise AdapterError(f"gemini exited {rc} with no output: {tail(err)}")
        d = parse_json_output(out)
        if d.get("error"):
            e = d["error"]
            msg = e.get("message") if isinstance(e, dict) else str(e)
            raise AdapterError(f"gemini error: {tail(str(msg))}")
        text = d.get("response") or d.get("result") or d.get("text") or ""
        if not str(text).strip():
            raise AdapterError(f"gemini returned an empty response (exit {rc}): {tail(err)}")
        tokens = None
        stats = d.get("stats") or {}
        try:
            tokens = sum(int(m.get("tokens", {}).get("total") or 0) for m in (stats.get("models") or {}).values()) or None
        except (AttributeError, TypeError, ValueError):
            tokens = None
        return Reply(str(text), tokens=tokens, meta={"session_id": d.get("session_id")})
