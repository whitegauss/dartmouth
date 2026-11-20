from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from dartmouth.adapters import create_adapter, load_adapter_class
from dartmouth.adapters.antigravity_cli import AntigravityCLIAdapter
from dartmouth.adapters.base import AdapterError, TurnContext, run_subprocess
from dartmouth.adapters.claude_code import ClaudeCodeAdapter
from dartmouth.adapters.gemini_cli import GeminiCLIAdapter
from dartmouth.engine.context import REDACTED, redact
from dartmouth.storage.minutes import PROPOSAL_SCHEMA

from .conftest import make_config, run_auto

SECRET = "血圧の記録は毎朝135/88"


def test_redact_patterns():
    t = f"公開情報\n<private>{SECRET}</private>\n[非公開] 口座番号 1234\n[private] key\n残り <PRIVATE>閉じ忘れ"
    r = redact(t)
    assert SECRET not in r and "1234" not in r and "key" not in r and "閉じ忘れ" not in r
    assert "公開情報" in r and r.count(REDACTED) == 4


async def test_public_only_participant_never_sees_private(tmp_path, new_session):
    cfg = make_config(
        tmp_path,
        {
            "claude": {"adapter": "dummy", "role": "chair", "replies": [f"引用: <private>{SECRET}</private> を踏まえると…"]},
            "gemini": {"adapter": "dummy", "role": "researcher"},
            "critic": {"adapter": "dummy", "role": "third_opinion", "optional": True, "share_scope": "public_only"},
        },
        topics=[{"title": "健康データの扱い", "premise": f"データの種類: 血圧・体重\n<private>{SECRET}</private>"}],
        context=[
            {"text": "公開資料: 中古ミニPC 8GB", "label": "機材"},
            {"text": f"非公開資料: {SECRET}", "private": True, "label": "健康記録"},
        ],
    )
    s = new_session(cfg)
    s.engine.interject(f"[非公開] {SECRET}")

    def on_approval(engine, ev):
        engine.approve()

    await run_auto(s, on_approval)
    critic_prompts = [c.prompt for c in s.adapters["critic"].prompts]
    assert critic_prompts
    for p in critic_prompts:
        assert SECRET not in p
        assert "健康記録" not in p
    assert any("中古ミニPC 8GB" in p for p in critic_prompts)
    assert any("データの種類: 血圧・体重" in p for p in critic_prompts)
    # 共有範囲 all の参加者には渡っている
    assert any(SECRET in c.prompt for c in s.adapters["gemini"].prompts)


def test_adapter_registry_and_plugin_path():
    assert load_adapter_class("dummy").__name__ == "DummyAdapter"
    assert load_adapter_class("dartmouth.adapters.dummy:DummyAdapter").__name__ == "DummyAdapter"
    with pytest.raises(ValueError):
        load_adapter_class("nope")


def ctx(**kw) -> TurnContext:
    base = dict(prompt="P" * 10, participant="x", role="chair", round="independent", topic=1, meeting_id="m")
    base.update(kw)
    return TurnContext(**base)


def test_claude_build_is_read_only_and_uses_stdin():
    a = ClaudeCodeAdapter("claude", "chair", 10, {})
    argv, stdin = a.build(ctx(response_schema=PROPOSAL_SCHEMA))
    assert stdin == "P" * 10 and "P" * 10 not in argv
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,WebSearch,WebFetch"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--json-schema" in argv and "--no-session-persistence" in argv
    assert "--dangerously-skip-permissions" not in argv


def test_claude_session_continuation():
    a = ClaudeCodeAdapter("claude", "chair", 10, {"continue_session": True})
    c = ctx()
    a1, _ = a.build(c)
    a_retry, _ = a.build(c)
    assert "--session-id" in a_retry  # 失敗した後の再試行では、まだ --resume しない
    ok = json.dumps({"subtype": "success", "is_error": False, "result": "ok"})
    a.parse(0, ok, "", c)
    a2, _ = a.build(ctx())
    assert "--session-id" in a1 and "--resume" in a2
    assert a1[a1.index("--session-id") + 1] == a2[a2.index("--resume") + 1]


def test_claude_parse():
    a = ClaudeCodeAdapter("claude", "chair", 10, {})
    out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": '{"msg":"hi"}',
                      "structured_output": {"msg": "hi"}, "session_id": "s", "usage": {"input_tokens": 2, "output_tokens": 4}})
    r = a.parse(0, out, "", ctx())
    assert r.text == '{"msg":"hi"}' and r.structured == {"msg": "hi"} and r.tokens == 6
    with pytest.raises(AdapterError):
        a.parse(0, json.dumps({"subtype": "error_max_turns", "is_error": True, "result": ""}), "", ctx())
    with pytest.raises(AdapterError):
        a.parse(1, "", "boom", ctx())


def test_gemini_build_and_parse():
    a = GeminiCLIAdapter("gemini", "researcher", 10, {"model": "gemini-2.5-pro"})
    argv, stdin = a.build(ctx())
    assert stdin == "P" * 10
    assert argv[argv.index("--approval-mode") + 1] == "plan" and "--yolo" not in argv and "-y" not in argv
    assert argv[argv.index("-o") + 1] == "json"
    out = "Loaded cached credentials.\n" + json.dumps({"session_id": "s", "response": "こんにちは",
                                                       "stats": {"models": {"g": {"tokens": {"total": 42}}}}})
    r = a.parse(0, out, "", ctx())
    assert r.text == "こんにちは" and r.tokens == 42
    with pytest.raises(AdapterError, match="Auth"):
        a.parse(41, json.dumps({"error": {"type": "Error", "message": "Please set an Auth method", "code": 41}}), "", ctx())


def test_antigravity_build_and_parse(tmp_path):
    a = AntigravityCLIAdapter("agy", "engineer", 10, {})
    argv, stdin = a.build(ctx())
    assert stdin is None and argv[argv.index("-p") + 1] == "P" * 10
    assert argv[argv.index("--mode") + 1] == "plan" and "--sandbox" in argv
    assert "--dangerously-skip-permissions" not in argv
    # 長いプロンプトはファイル経由
    a2 = AntigravityCLIAdapter("agy", "engineer", 10, {"cwd": str(tmp_path / "src"), "prompt_dir": str(tmp_path)})
    c1, c2 = ctx(prompt="あ" * 60_000), ctx(prompt="い" * 60_000)
    big, _ = a2.build(c1)
    a2.build(c2)  # 同じ参加者への並行呼び出し（/ask など）
    msg = big[big.index("-p") + 1]
    assert len(msg) < 500 and str(tmp_path) in msg
    files = sorted(tmp_path.iterdir(), key=lambda f: f.read_text())
    assert files[0].read_text() == "あ" * 60_000 and (files[0].stat().st_mode & 0o777) == 0o600
    a2.cleanup(c1)
    assert not files[0].exists() and files[1].exists()  # 自分の呼び出しの分だけ消す
    a2.cleanup(c2)
    assert not files[1].exists()
    out = json.dumps({"conversation_id": "c", "status": "SUCCESS", "response": "pong\n", "usage": {"total_tokens": 8012}})
    r = a.parse(0, out, "", ctx())
    assert r.text == "pong\n" and r.tokens == 8012
    with pytest.raises(AdapterError, match="denied"):
        a.parse(0, json.dumps({"status": "SUCCESS", "response": "", "denied_actions": [{"action": "command"}]}), "", ctx())


async def test_run_subprocess_kills_process_group_on_timeout(tmp_path):
    pidfile = tmp_path / "pid"
    script = f"sleep 60 & echo $! > {pidfile}; wait"
    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await run_subprocess(["bash", "-c", script], timeout_s=0.5, name="t")
    assert time.monotonic() - t0 < 5
    await asyncio.sleep(0.2)
    child = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)  # 孫プロセスまで消えている


async def test_run_subprocess_no_shell_injection(tmp_path):
    marker = tmp_path / "pwned"
    rc, out, _ = await run_subprocess(["echo", f"; touch {marker}"], timeout_s=5)
    assert rc == 0 and not marker.exists() and "touch" in out


async def test_cli_adapter_end_to_end_with_fake_binary(tmp_path):
    """本物の CLI の代わりに、同じ JSON を返す偽コマンドで respond を通す。"""
    fake = tmp_path / "fake-claude"
    fake.write_text('#!/bin/sh\ncat > /dev/null\necho \'{"type":"result","subtype":"success","is_error":false,"result":"ok from fake"}\'\n')
    fake.chmod(0o755)
    a = create_adapter("claude", "claude_code", "chair", 10, {"command": str(fake)})
    r = await a.respond(ctx())
    assert r.text == "ok from fake"


def test_cli_workdir_is_isolated_by_default(tmp_path):
    a = ClaudeCodeAdapter("claude", "chair", 10, {})
    wd = a.workdir()
    assert wd != os.getcwd() and wd.endswith("/claude") and os.listdir(wd) == []
    assert ClaudeCodeAdapter("c", "chair", 10, {"cwd": str(tmp_path)}).workdir() == str(tmp_path)


def test_redact_marker_after_prefix():
    r = redact(f"- [D-001] [非公開] {SECRET}（議題1）\n却下: [private] {SECRET}")
    assert SECRET not in r


async def test_public_only_redacts_rejection_reason_and_ask(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair"},
        "critic": {"adapter": "dummy", "role": "third_opinion", "share_scope": "public_only"},
    }, topics=["議題A"], intro=False)
    s = new_session(cfg)
    n = 0

    def on_approval(engine, ev):
        nonlocal n
        n += 1
        if n == 1:
            engine.reject(f"<private>{SECRET}</private> を考慮していない")
        else:
            engine.ask("critic", f"<private>{SECRET}</private> についてどう思う？")
            engine.approve()

    await run_auto(s, on_approval)
    await asyncio.sleep(0.1)
    assert all(SECRET not in c.prompt for c in s.adapters["critic"].prompts)
    assert any("を考慮していない" in c.prompt for c in s.adapters["critic"].prompts)


def test_antigravity_effort_and_no_json_schema():
    a = AntigravityCLIAdapter("agy", "chair", 10, {"model": "gemini-4-argon-high", "effort": "high"})
    argv, _ = a.build(ctx(response_schema=PROPOSAL_SCHEMA))
    assert argv[argv.index("--effort") + 1] == "high"
    assert "--json-schema" not in argv


def test_redact_html_comment_markers():
    r = redact(f"公開\n<!-- private -->\n- {SECRET}\n<!-- /private -->\n続き\n<!--private-->閉じ忘れ {SECRET}")
    assert SECRET not in r and "公開" in r and "続き" in r


def test_tools_only_in_research_rounds(tmp_path):
    a = ClaudeCodeAdapter("claude", "engineer", 10, {"cwd": str(tmp_path)})
    argv, _ = a.build(ctx(round="independent"))
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,WebSearch,WebFetch"
    assert a.workdir(ctx(round="independent")) == str(tmp_path)
    argv, _ = a.build(ctx(round="critique"))
    assert argv[argv.index("--tools") + 1] == "" and "--allowedTools" not in argv
    assert a.workdir(ctx(round="critique")) != str(tmp_path)  # コードも読めない空の作業ディレクトリ
    a2 = ClaudeCodeAdapter("claude", "engineer", 10, {"tools_rounds": ["independent", "critique"]})
    argv, _ = a2.build(ctx(round="critique"))
    assert argv[argv.index("--tools") + 1] != ""


def test_usage_breakdown_recorded():
    a = ClaudeCodeAdapter("claude", "chair", 10, {})
    out = json.dumps({"subtype": "success", "is_error": False, "result": "ok", "num_turns": 7,
                      "usage": {"input_tokens": 5, "cache_read_input_tokens": 1000, "output_tokens": 50}})
    r = a.parse(0, out, "", ctx())
    assert r.meta["turns"] == 7 and r.meta["usage"]["cache_read_input_tokens"] == 1000
