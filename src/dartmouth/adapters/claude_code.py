"""Claude Code（claude -p）アダプタ。

確認したフラグ（claude --help, 2026-11）:
  -p / --output-format json / --tools / --allowedTools / --permission-mode dontAsk
  --json-schema（結果は structured_output に入る） / --session-id / --resume / --no-session-persistence
プロンプトは標準入力で渡す（引数長の上限を避けるため）。

options:
  model, effort, tools（既定: 読み取り系のみ）, cwd, command, continue_session（既定 False）
continue_session=False が既定なのは、毎ターン「目的＋議事録＋今の議題の発言」を渡しているため、
セッションを継続すると同じ文脈を二重に送ることになるから。
"""

from __future__ import annotations

import json
import uuid

from .base import AdapterError, Reply, TurnContext
from .cli_base import CLIAdapter, parse_json_output, tail

READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]
WEB_TOOLS = ["WebSearch", "WebFetch"]


SYSTEM_PROMPT = "あなたは複数のAIが参加する会議の参加者です。ユーザーの指示に書かれた役割と形式に従って回答してください。"


class ClaudeCodeAdapter(CLIAdapter):
    default_command = "claude"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sessions: set[str] = set()

    def build(self, ctx: TurnContext) -> tuple[list[str], str | None]:
        if ctx.round == "research":
            tools = WEB_TOOLS  # 事前調査は Web だけ（ファイルは読ませない）
        else:
            tools = self.options.get("tools", READ_ONLY_TOOLS) if self.tools_enabled(ctx) else []
        tools_arg = ",".join(tools) if isinstance(tools, list) else str(tools)
        argv = [self.command, "-p", "--output-format", "json", "--tools", tools_arg, "--permission-mode", "dontAsk"]
        if tools_arg:
            argv += ["--allowedTools", tools_arg]
        if self.options.get("model"):
            argv += ["--model", str(self.options["model"])]
        minimal = self.options.get("minimal_prompt", "auto")
        if minimal is True or (minimal == "auto" and not tools_arg):
            # 道具を使わない呼び出しでは、Claude Code の既定のシステムプロンプト（約3千トークン）を短い1文に置き換え、
            # 設定・MCP・スキルの読み込みも止める。全呼び出しで同じ短いプロンプトになるので、キャッシュも効きやすい
            argv += ["--system-prompt", str(self.options.get("system_prompt") or SYSTEM_PROMPT),
                     "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", ""]
        if self.options.get("effort"):
            argv += ["--effort", str(self.options["effort"])]
        if ctx.response_schema is not None:
            argv += ["--json-schema", json.dumps(ctx.response_schema, ensure_ascii=False)]
        if self.options.get("continue_session"):
            sid = self._sid(ctx)
            argv += ["--resume", sid] if sid in self._sessions else ["--session-id", sid]
        else:
            argv += ["--no-session-persistence"]
        return argv, ctx.prompt

    def _sid(self, ctx: TurnContext) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"dartmouth:{ctx.meeting_id}:{self.name}:{ctx.topic}"))

    def parse(self, rc: int, out: str, err: str, ctx: TurnContext) -> Reply:
        reply = self._parse(rc, out, err, ctx)
        if self.options.get("continue_session"):
            # セッションが実際にできたのは成功したときだけ（失敗後の再試行で --resume しない）
            self._sessions.add(self._sid(ctx))
        return reply

    def _parse(self, rc: int, out: str, err: str, ctx: TurnContext) -> Reply:
        if rc != 0 and not out.strip():
            raise AdapterError(f"claude exited {rc}: {tail(err)}")
        d = parse_json_output(out)
        if d.get("is_error") or d.get("subtype") not in (None, "success"):
            raise AdapterError(f"claude error ({d.get('subtype')}): {tail(str(d.get('result', '')))}")
        text = d.get("result") or ""
        structured = d.get("structured_output")
        if not text and structured is not None:
            text = json.dumps(structured, ensure_ascii=False)
        if not text.strip():
            raise AdapterError("claude returned an empty result")
        u = d.get("usage") or {}
        tokens = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")) or None
        usage = {k: int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}
        model_usage = d.get("modelUsage")
        model = d.get("model")
        if not isinstance(model, str) and isinstance(model_usage, dict) and len(model_usage) == 1:
            model = next(iter(model_usage))
        return Reply(text, tokens=tokens, structured=structured,
                     meta={"session_id": d.get("session_id"), "cost_usd": d.get("total_cost_usd"),
                           "turns": d.get("num_turns"), "usage": usage, "model": model})
