"""Antigravity CLI（agy -p）アダプタ。

確認したフラグ（agy --help, v1.5.1）:
  -p <prompt> / --output-format json / --mode plan（読み取り専用） / --model / --sandbox / --print-timeout
JSON 出力: {"conversation_id", "status": "SUCCESS", "response", "usage": {"total_tokens", ...}}
agy は標準入力をプロンプトとして読まないので、引数で渡す。長すぎるときは一時ファイルに書いて読ませる。

options: model, effort（low|medium|high|xhigh。モデル名と矛盾すると agy がエラーにする）, cwd, command, sandbox（既定 True）

--json-schema は渡さない。v1.5.1 では出力が強制されず（違うキーの JSON や確認の質問が返る）、
出力の json_schema 欄に指定したスキーマが書き戻されるだけだったため。形式はプロンプトで指示し、
崩れたらエンジンが再要求する。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .base import AdapterError, Reply, TurnContext
from .cli_base import CLIAdapter, parse_json_output, tail

MAX_ARG = 100_000  # Linux の1引数あたりの上限（128KiB）より余裕をもたせる


class AntigravityCLIAdapter(CLIAdapter):
    default_command = "agy"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._tmpfiles: dict[int, str] = {}  # id(ctx) → 一時ファイル

    def cleanup(self, ctx: TurnContext) -> None:
        p = self._tmpfiles.pop(id(ctx), None)
        if p:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    def build(self, ctx: TurnContext) -> tuple[list[str], str | None]:
        prompt = ctx.prompt
        if len(prompt.encode()) > MAX_ARG:
            # 作業ディレクトリ（cwd でコードを読ませている場合など）を汚さないよう専用の場所に置き、呼び出し後に消す
            d = Path(self.options.get("prompt_dir") or Path(tempfile.gettempdir()) / f"dartmouth-{os.getuid()}" / "prompts")
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
            fd, path = tempfile.mkstemp(prefix="prompt-", suffix=".md", dir=d)
            self._tmpfiles[id(ctx)] = path
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(prompt)
            os.chmod(path, 0o600)
            prompt = f"ファイル {path} に会議の文脈と今回の指示があります。読んで、その指示に従ってあなたの発言だけを書いてください。"
        argv = [self.command, "-p", prompt, "--output-format", "json", "--mode", "plan"]
        if self.options.get("sandbox", True):
            argv.append("--sandbox")
        if self.options.get("model"):
            argv += ["--model", str(self.options["model"])]
        if self.options.get("effort"):
            argv += ["--effort", str(self.options["effort"])]
        return argv, None

    def parse(self, rc: int, out: str, err: str, ctx: TurnContext) -> Reply:
        if not out.strip():
            raise AdapterError(f"agy exited {rc} with no output: {tail(err)}")
        d = parse_json_output(out)
        if d.get("status") not in (None, "SUCCESS"):
            raise AdapterError(f"agy status {d.get('status')}: {tail(str(d.get('error') or d.get('response') or err))}")
        text = d.get("response") or ""
        if not text.strip():
            denied = d.get("denied_actions")
            raise AdapterError(f"agy returned an empty response{f' (denied: {denied})' if denied else ''}: {tail(err)}")
        u = d.get("usage") or {}
        tokens = u.get("total_tokens")
        return Reply(text, tokens=tokens, meta={"conversation_id": d.get("conversation_id"), "turns": d.get("num_turns"),
                                                "usage": {k: u.get(k) for k in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens")}})
