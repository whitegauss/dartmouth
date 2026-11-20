from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from dartmouth.config import Config, load_config
from dartmouth.engine.commands import handle
from dartmouth.storage.minutes import Minutes, Proposal

ROOT = Path(__file__).resolve().parents[1]


def test_init_writes_loadable_config(tmp_path):
    from dartmouth.session import copy_example_config

    dest = tmp_path / "config.yaml"
    copy_example_config(dest)
    text = dest.read_text(encoding="utf-8")
    assert "meetings_dir:" not in text and "prompts_dir:" not in text
    cfg = load_config(dest)
    assert cfg.participants and cfg.meeting.topics


def test_adapter_from_file_path(tmp_path):
    """adapter: "path/to/file.py:Class" (relative to the config file) loads an adapter outside the package."""
    (tmp_path / "ext").mkdir()
    (tmp_path / "ext" / "echo.py").write_text(
        "from dartmouth.adapters.base import Adapter, Reply\n"
        "class Echo(Adapter):\n"
        "    async def respond(self, ctx):\n"
        "        return Reply('echo')\n"
    )
    (tmp_path / "c.yaml").write_text(
        "participants:\n  a: {adapter: 'ext/echo.py:Echo', role: chair}\n"
    )
    cfg = load_config(tmp_path / "c.yaml")
    assert cfg.participants["a"].adapter == f"{tmp_path / 'ext' / 'echo.py'}:Echo"
    from dartmouth.adapters import create_adapter

    a = create_adapter("a", cfg.participants["a"].adapter, "chair", 5, {})
    assert type(a).__name__ == "Echo"


def test_dummy_example_config_loads():
    cfg = load_config(ROOT / "examples" / "dummy.yaml")
    assert all(p.adapter == "dummy" for p in cfg.participants.values())


def test_config_extends_merges_and_resolves_paths(tmp_path):
    sub = tmp_path / "base"
    sub.mkdir()
    (sub / "chair.md").write_text("chair")
    (sub / "agents.yaml").write_text(
        "budget:\n  max_calls: 50\n"
        "participants:\n  chair: {adapter: dummy, role: chair, persona_file: chair.md}\n"
    )
    (tmp_path / "topic.yaml").write_text(
        "extends: base/agents.yaml\n"
        "meeting:\n  name: t\n  topics: [議題1]\n"
        "budget:\n  max_chars_per_message: 1500\n"
    )
    cfg = load_config(tmp_path / "topic.yaml")
    assert cfg.chair == "chair"
    assert cfg.participants["chair"].persona_file == str((sub / "chair.md").resolve())
    assert cfg.budget.max_calls == 50 and cfg.budget.max_chars_per_message == 1500
    assert [t.title for t in cfg.meeting.topics] == ["議題1"]


def test_load_config_with_separate_agents_path(tmp_path):
    (tmp_path / "agents.yaml").write_text(
        "participants:\n  chair: {adapter: dummy, role: chair, display_name: 議長A}\n"
    )
    (tmp_path / "topics.yaml").write_text(
        "meeting:\n  name: sep\n  topics: [議題X]\n"
    )
    cfg = load_config(tmp_path / "topics.yaml", agents_path=tmp_path / "agents.yaml")
    assert cfg.participants["chair"].display_name == "議長A"
    assert [t.title for t in cfg.meeting.topics] == ["議題X"]


def test_apply_chair_override():
    from dartmouth.config import apply_chair_override

    cfg = Config.model_validate({"participants": {"chair": {"adapter": "dummy", "role": "chair"}}})
    apply_chair_override(cfg, chair="argon", effort="high")
    ch = cfg.participants["chair"]
    assert ch.adapter == "antigravity_cli" and ch.options().get("model") == "gemini-4-argon-high" and ch.display_name == "議長(Argon)"

    apply_chair_override(cfg, chair="opus", effort="low")
    ch = cfg.participants["chair"]
    assert ch.adapter == "claude_code" and ch.options().get("model") == "opus" and ch.options().get("effort") == "low" and ch.display_name == "議長(Opus)"


def test_config_requires_one_chair():
    with pytest.raises(ValidationError):
        Config.model_validate({"participants": {"a": {"adapter": "dummy"}}})
    with pytest.raises(ValidationError):
        Config.model_validate({"participants": {"a": {"adapter": "dummy", "role": "chair"}, "b": {"adapter": "dummy", "role": "chair"}}})


def test_config_rejects_unknown_topic_participant():
    with pytest.raises(ValidationError):
        Config.model_validate({
            "meeting": {"topics": [{"title": "x", "participants": ["ghost"]}]},
            "participants": {"a": {"adapter": "dummy", "role": "chair"}},
        })


def test_minutes_render_matches_spec_format():
    m = Minutes("requirements")
    m.apply(1, "データの棚卸し", Proposal(
        decisions=["写真は外付けHDDに置く"], open_questions=[{"text": "バックアップ先", "owner": "gemini"}],
        todos=[{"text": "容量を測る", "owner": "user"}], agreements=["ローカル優先"], summary="要約"), "2026-11-16T15:42:00+09:00")
    md = m.render()
    assert "- [D-001] 写真は外付けHDDに置く（議題1、承認 2026-11-16 15:42）" in md
    assert "- [Q-001] バックアップ先\n" in md  # AI がやるもの（参加者の id）には担当を出さない
    assert "- [ ] [T-001] 容量を測る（担当: 本人）" in md
    assert "### 議題1: データの棚卸し" in md


def test_command_parsing(tmp_path):
    from dartmouth.engine.context import PromptLibrary
    from dartmouth.engine.flow import Engine

    from .conftest import make_config

    cfg = make_config(tmp_path)
    e = Engine(cfg, "m", {}, prompts=PromptLibrary(ROOT / "prompts"))
    assert "コマンド" in handle(e, "/help").message
    assert handle(e, "/edit").level == "warning"
    assert handle(e, "/quit").ui_action == "quit"
    assert handle(e, "/skip Gemini").message.endswith("欠席にしました") and "gemini" in e.absent
    assert handle(e, "/join gemini").message.endswith("復帰しました") and not e.absent
    assert handle(e, "/skip nobody").level == "warning"
    assert handle(e, "/bogus").level == "warning"
    handle(e, "/topic 追加議題")
    assert e.topics[-1].title == "追加議題"


def test_embedded_headings_are_demoted():
    m = Minutes("x")
    m.final_summary = "## 会議総括\n### 1. 決まったこと\n本文 #タグ"
    md = m.render()
    assert "\n#### 会議総括\n##### 1. 決まったこと\n" in md and "本文 #タグ" in md


def test_arrival_buffer_all_at_once():
    from dartmouth.engine import events as E
    from dartmouth.ui.common import ArrivalBuffer

    b = ArrivalBuffer("all_at_once")
    ev = lambda t, **d: E.Event(t, d)  # noqa: E731
    assert b.feed(ev(E.ROUND_START, round="independent"))
    assert b.feed(ev(E.SPEECH_END, round="independent", status="ok")) == []
    assert len(b.feed(ev(E.SPEECH_END, round="independent", status="timeout"))) == 1  # 失敗はすぐ出す
    out = b.feed(ev(E.MEETING_END, reason="interrupted"))
    assert [e.type for e in out] == [E.SPEECH_END, E.MEETING_END]  # 溜めていた回答も出す


def test_compact_minutes_omits_summaries():
    m = Minutes("x")
    m.apply(1, "議題", Proposal(decisions=["決定A"], summary="長い要約" * 50), "2026-11-16T15:42:00+09:00")
    c = m.render_compact()
    assert "[D-001] 決定A" in c and "長い要約" not in c


def test_slugify_non_ascii_name_gets_time():
    from dartmouth.session import slugify

    assert slugify("Home Requirements!") == "home-requirements"
    s = slugify("画面テスト")
    assert s.startswith("meeting-") and len(s) == len("meeting-1530")


def test_classify_error():
    from dartmouth.storage.archive import classify_error

    assert classify_error("claude error (success): You've hit your 5-hour limit") == "利用上限"
    assert classify_error("agy returned an empty response (denied: [{'action': 'read_file'}])") == "権限の拒否で空の応答"
    assert classify_error("180秒以内に応答がありませんでした") == "タイムアウト"


def test_literal_backslash_n_in_summary_becomes_newline():
    p = Proposal(summary="合意した。\\n- 構成は5スタック\\n- 権限は最小", handover="案A\\n論点B")
    assert p.summary == "合意した。\n- 構成は5スタック\n- 権限は最小" and p.handover == "案A\n論点B"
    assert Proposal(summary="1行目\n2行目 C:\\\\new").summary == "1行目\n2行目 C:\\\\new"  # 本物の改行がある文はそのまま


def test_meeting_lock_blocks_second_process(tmp_path):
    from dartmouth.storage.lock import MeetingLock, MeetingLocked, is_locked

    a = MeetingLock(tmp_path).acquire()
    assert is_locked(tmp_path)
    import pytest as _p
    with _p.raises(MeetingLocked):
        MeetingLock(tmp_path).acquire()
    a.release()
    assert not is_locked(tmp_path)
    MeetingLock(tmp_path).acquire().release()
