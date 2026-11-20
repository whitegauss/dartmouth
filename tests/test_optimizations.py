from __future__ import annotations

from dartmouth.config import Config, KnowledgeConfig
from dartmouth.engine.context import ContextBuilder, Message, PromptLibrary
from dartmouth.session import build_adapters
from dartmouth.storage.archive import stat_rows

from .conftest import ROOT, make_config, run_auto
from .test_knowledge import _docs


def test_meeting_tools_off_overrides_agent_tools():
    cfg = Config.model_validate({"meeting": {"tools": "off"}, "participants": {
        "a": {"adapter": "claude_code", "role": "chair", "cwd": "/code"}}})
    assert cfg.meeting.tools_enabled is False
    a = build_adapters(cfg)["a"]
    assert a.options.get("tools_rounds") == [] and "cwd" not in a.options
    on = Config.model_validate({"meeting": {"tools": "read_only", "agent_tools": False}, "participants": {
        "a": {"adapter": "claude_code", "role": "chair", "cwd": "/code"}}})
    assert on.meeting.tools_enabled is True and build_adapters(on)["a"].options.get("cwd") == "/code"


def _builder(cfg):
    return ContextBuilder(cfg, PromptLibrary(ROOT / "prompts"), {n: n for n in cfg.participants} | {"user": "ユーザー"})


def test_context_rounds_and_prompt_parts(tmp_path):
    cfg = make_config(tmp_path, topics=["議題A"], context=[
        {"text": "毎回渡す短い資料", "label": "短い"},
        {"text": "大きな議事録" * 200, "label": "大きい", "rounds": ["independent", "summary"]}])
    b = _builder(cfg)
    kw = dict(participant="gemini", round_template="x", topic_no=1, topic_title="t", topic_premise="", messages=[], minutes_md="", members=["claude", "gemini"])
    indep = b.build(round_name="independent", **kw)
    assert "大きな議事録" in indep and b.last_parts["background"] > 1000
    crit = b.build(round_name="critique", **kw)
    assert "大きな議事録" not in crit and "毎回渡す短い資料" in crit
    assert sum(b.last_parts.values()) == len(crit)


def test_critique_halves_materials_and_prefers_cited(tmp_path):
    cfg = make_config(tmp_path, topics=["議題A"])
    cfg.knowledge = KnowledgeConfig.model_validate({"sources": [], "max_chars": 400})
    b = _builder(cfg)
    mats = [{"id": f"m{i}", "path": f"docs/f{i}.md", "start": 1, "end": 2, "text": "あ" * 150} for i in range(3)]
    msgs = [Message("claude", "independent", "`docs/f2.md:1-2` によると…", 1)]
    kw = dict(participant="gemini", round_template="x", topic_no=1, topic_title="t", topic_premise="", members=["claude", "gemini"], minutes_md="")
    indep = b.build(round_name="independent", materials=mats, messages=msgs, **kw)
    assert indep.count("あ" * 150) == 2  # 上限400字
    crit = b.build(round_name="critique", materials=mats, messages=msgs, **kw)
    assert crit.count("あ" * 150) == 1  # 上限は半分の200字
    body = crit.split("# 関連資料")[1]
    assert body.index("docs/f2.md") < body.index("docs/f0.md")  # 引用された資料が先


async def test_duplicate_requests_and_stats(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "chair": {"adapter": "dummy", "role": "chair"},
        "claude": {"adapter": "dummy", "role": "engineer", "replies": ["案\n【資料請求】利用枠 5時間\n【資料請求】利用枠  5時間"]},
    }, topics=["ストレージの容量"], intro=False, chair_participates=False, approval="auto")
    cfg.knowledge = KnowledgeConfig.model_validate({"sources": _docs(tmp_path), "embedder": {"backend": "hash"},
                                                     "index_dir": str(tmp_path / "idx")})
    s = new_session(cfg)
    d = await run_auto(s)
    recs = d.records()
    assert sum(1 for r in recs if r.get("event") == "kb_lookup") == 1
    assert sum(1 for r in recs if r.get("event") == "kb_duplicate") >= 1
    rows, total = stat_rows(s.dir)
    assert total["auto_approved"] == 1 and total["kb_duplicates"] >= 1
    assert total["context_ratio"] is not None and 0 < total["context_ratio"] < 1
    assert all(r["context_ratio"] != "" for r in rows if r["round"] in ("independent", "critique"))


def test_tools_off_written_bare_in_yaml(tmp_path):
    from dartmouth.config import load_config

    f = tmp_path / "c.yaml"
    f.write_text("meeting:\n  tools: off\nparticipants:\n  a: {adapter: dummy, role: chair}\n")
    assert load_config(f).meeting.tools == "off" and load_config(f).meeting.tools_enabled is False


def test_digest_keeps_open_items_and_redacts_private():
    from dartmouth.storage.minutes import make_digest

    md = "# 議事録: x\n\n## 決定事項\n" + "".join(f"- [D-{i:03d}] 決定{i}の本文" + "あ" * 80 + "（議題1、承認 2026-11-18 10:00）\n" for i in range(1, 31)) \
        + "- [D-031] <private>口座番号 123</private> を使う\n" \
        + "\n## 未決事項\n- [Q-001] 未決A（担当: user）\n- [Q-002] 済んだ未決\n\n## ToDo\n- [ ] [T-001] やること\n\n## 議題ごとの要約\n長い要約" + "い" * 5000
    out = make_digest(md, limit=1500, done={"Q-002"})
    assert len(out) <= 1500 + 400  # 索引の分だけ超えてよい
    assert "[Q-001]" in out and "Q-002" not in out and "[T-001]" in out
    assert "[D-031]" in out and "123" not in out and "<private>" not in out  # 新しい決定から入れる。非公開は伏せる
    assert "承認 2026" not in out and "長い要約" not in out
    assert "索引" in out and "D-001" in out


def test_claude_minimal_system_prompt_only_without_tools():
    from dartmouth.adapters.base import TurnContext
    from dartmouth.adapters.claude_code import ClaudeCodeAdapter

    a = ClaudeCodeAdapter("c", "member", 30, {"model": "sonnet", "tools_rounds": ["independent"]})
    crit = a.build(TurnContext(participant="c", role="member", meeting_id="m", topic=1, round="critique", prompt="p"))[0]
    assert "--system-prompt" in crit and "--strict-mcp-config" in crit and crit[crit.index("--setting-sources") + 1] == ""
    ind = a.build(TurnContext(participant="c", role="member", meeting_id="m", topic=1, round="independent", prompt="p"))[0]
    assert "--system-prompt" not in ind  # 道具を使う回は Claude Code の既定のプロンプトのまま
    off = ClaudeCodeAdapter("c", "member", 30, {"minimal_prompt": False}).build(TurnContext(participant="c", role="member", meeting_id="m", topic=1, round="critique", prompt="p"))[0]
    assert "--system-prompt" not in off
