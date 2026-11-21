from __future__ import annotations

import asyncio
import json

import pytest

from dartmouth.engine import events as E
from dartmouth.session import resume_session
from dartmouth.storage.transcript import BASE_KEYS, KINDS, ROUNDS, STATUSES

from .conftest import Driver, make_config, run_auto


async def test_full_meeting_with_three_dummies(cfg, new_session):
    s = new_session(cfg)
    d = await run_auto(s)
    recs = d.records()
    # transcript の形式
    for r in recs:
        assert list(r)[: len(BASE_KEYS)] == list(BASE_KEYS)
        assert r["round"] in ROUNDS
        assert r["kind"] in KINDS
        assert r["status"] in STATUSES
        assert r["meeting"] == s.dir.name
    msgs = [r for r in recs if r["kind"] == "message"]
    rounds = {(r["topic"], r["round"]) for r in msgs}
    assert {(None, "intro"), (1, "independent"), (1, "critique"), (1, "summary"), (2, "independent"), (2, "critique"), (2, "summary")} <= rounds
    assert sum(1 for r in msgs if r["round"] == "intro") == 3
    # 議事録
    md = (s.dir / "minutes.md").read_text()
    assert "# 議事録: test" in md
    assert "[D-001]" in md and "[D-002]" in md and "（議題1、承認" in md
    assert "[Q-001]" in md and "（担当: gemini）" not in md
    assert "- [ ] [T-001]" in md
    assert "### 議題1: 議題A" in md and "### 議題2: 議題B" in md
    assert "## 総括" in md
    assert (s.dir / "requirements_draft.md").exists()
    assert (s.dir / "config.yaml").exists()
    assert (s.dir / "app.log").exists()


async def test_independent_round_hides_other_answers(cfg, new_session):
    s = new_session(cfg)
    await run_auto(s)
    gem = s.adapters["gemini"]
    indep = [c for c in gem.prompts if c.round == "independent"]
    assert indep and all("claude の" not in c.prompt.split("# この議題のこれまでの発言")[1] for c in indep)
    crit = [c for c in gem.prompts if c.round == "critique"][0]
    assert "claude のindependentでの発言" in crit.prompt


async def test_previous_topic_messages_not_passed(cfg, new_session):
    s = new_session(cfg)
    await run_auto(s)
    t2 = [c for c in s.adapters["gemini"].prompts if c.topic == 2]
    assert t2 and all("（議題1、" not in c.prompt.split("# この議題のこれまでの発言")[1] for c in t2)
    # 現在の議事録（議題1の決定事項）は入っている
    assert "[D-001]" in t2[0].prompt


async def test_optional_participant_timeout_continues(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair"},
        "gemini": {"adapter": "dummy", "role": "researcher"},
        "critic": {"adapter": "dummy", "role": "third_opinion", "optional": True, "timeout_s": 0.2, "fail_mode": "hang", "fail_rate": 1.0},
    }, topics=["議題A"], intro=False)
    s = new_session(cfg)
    d = await run_auto(s)
    recs = d.records()
    critic = [r for r in recs if r["speaker"] == "critic"]
    assert any(r["status"] == "timeout" for r in critic)
    assert sum(1 for r in critic if r["kind"] == "error") == 4  # 独立回答・相互批判 × (1回 + 再試行1回)
    assert any("欠席扱い" in r["text"] for r in recs if r["kind"] == "system")
    assert "[D-001]" in (s.dir / "minutes.md").read_text()


async def test_required_participant_failure_pauses(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair", "timeout_s": 0.2, "fail_mode": "hang", "fail_calls": [1, 2]},
        "gemini": {"adapter": "dummy", "role": "researcher"},
    }, topics=["議題A"], intro=False)
    s = new_session(cfg)
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    ev = await d.until(lambda e: e.type == E.PAUSED)
    assert ev.data["participant"] == "claude"
    assert s.engine.status()["waiting"] == "pause"
    # gemini の独立回答は進んでいる。再試行すると3回目の呼び出しで成功する
    assert s.engine.resolve_pause("retry") == "再試行します"
    ev = await d.until(lambda e: e.type == E.AWAITING_APPROVAL)
    s.engine.approve()
    await d.until(lambda e: e.type == E.MEETING_END)
    await task
    await s.close()
    assert any(r.get("event") == "paused" for r in d.records())


async def test_interjection_during_critique_reaches_next_speaker(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair", "delay_s": 0.3},
        "gemini": {"adapter": "dummy", "role": "researcher", "delay_s": 0.3},
        "critic": {"adapter": "dummy", "role": "third_opinion", "delay_s": 0.3},
    }, topics=["議題A"], intro=False)
    s = new_session(cfg)
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    # 相互批判の1人目（claude）が話している最中に割り込む
    await d.until(lambda e: e.type == E.SPEECH_START and e.data["round"] == "critique")
    s.engine.interject("予算は月1000円以内で")
    await d.until(lambda e: e.type == E.AWAITING_APPROVAL)
    s.engine.approve()
    await d.until(lambda e: e.type == E.MEETING_END)
    await task
    await s.close()
    crit = lambda name: [c for c in s.adapters[name].prompts if c.round == "critique"][0]  # noqa: E731
    assert "予算は月1000円以内で" not in crit("claude").prompt  # 実行中の発言には入らない
    assert "予算は月1000円以内で" in crit("gemini").prompt  # 次の発言から入る
    assert "予算は月1000円以内で" in crit("critic").prompt
    summary = [c for c in s.adapters["claude"].prompts if c.round == "summary"][0]
    assert "予算は月1000円以内で" in summary.prompt


async def test_reject_restarts_critique_and_only_approved_enter_minutes(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=["議題A"], intro=False)
    s = new_session(cfg)
    seen = []

    def on_approval(engine, ev):
        seen.append(ev.data["proposal"]["decisions"][0])
        if len(seen) == 1:
            engine.reject("コストの根拠がない")
        else:
            engine.approve()

    d = await run_auto(s, on_approval)
    recs = d.records()
    assert any(r.get("event") == "rejected" and "コストの根拠がない" in r["text"] for r in recs)
    assert sum(1 for r in recs if r["kind"] == "message" and r["round"] == "critique") == 6  # 2巡
    crit2 = [c for c in s.adapters["gemini"].prompts if c.round == "critique"][1]
    assert "コストの根拠がない" in crit2.prompt
    md = (s.dir / "minutes.md").read_text()
    assert seen[0] != seen[1]
    assert seen[0] not in md  # 却下された案は入らない
    assert seen[1] in md and "[D-002]" not in md


async def test_edit_then_approve(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=["議題A"], intro=False)
    s = new_session(cfg)

    def on_approval(engine, ev):
        p = dict(ev.data["proposal"])
        p["decisions"] = ["ユーザーが修正した決定事項"]
        engine.approve(p)

    await run_auto(s, on_approval)
    md = (s.dir / "minutes.md").read_text()
    assert "[D-001] ユーザーが修正した決定事項" in md


async def test_budget_exceeded_goes_to_summary(tmp_path, new_session):
    # 自己紹介3 + 独立回答3 = 6 回で使い切り、相互批判を飛ばしてまとめへ
    cfg = make_config(tmp_path, topics=["議題A"], budget={"max_calls": 6}, critique_rounds=2)
    s = new_session(cfg)
    d = await run_auto(s)
    recs = d.records()
    assert not any(r["round"] == "critique" and r["kind"] == "message" for r in recs)
    assert any(r.get("event") == "budget_exceeded" for r in recs)
    assert any(r["round"] == "summary" and r["kind"] == "message" for r in recs)
    assert "[D-001]" in (s.dir / "minutes.md").read_text()


async def test_max_rounds_budget(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=["議題A"], budget={"max_rounds": 1}, critique_rounds=3, intro=False)
    s = new_session(cfg)
    d = await run_auto(s)
    assert not any(r["round"] == "critique" and r["kind"] == "message" for r in d.records())


async def test_message_truncated_to_max_chars(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair"},
        "gemini": {"adapter": "dummy", "role": "researcher", "replies": ["あ" * 500]},
    }, topics=["議題A"], intro=False, budget={"max_chars_per_message": 100})
    s = new_session(cfg)
    d = await run_auto(s)
    g = [r for r in d.records() if r["speaker"] == "gemini" and r["kind"] == "message"]
    assert all(r["text"].startswith("あ" * 100) and "文字数上限" in r["text"] and r["truncated"] for r in g)


async def test_bad_json_summary_is_re_requested(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair", "bad_json": True},
        "gemini": {"adapter": "dummy", "role": "researcher"},
    }, topics=["議題A"], intro=False)
    s = new_session(cfg)
    await run_auto(s)
    summaries = [c for c in s.adapters["claude"].prompts if c.round == "summary" and c.response_schema]
    assert len(summaries) == 2
    assert "JSONとして解釈できませんでした" in summaries[1].prompt
    assert "[D-001]" in (s.dir / "minutes.md").read_text()


async def test_skip_and_join(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=["議題A", "議題B"], intro=False)
    s = new_session(cfg)
    first = True

    def on_approval(engine, ev):
        nonlocal first
        if first:
            engine.skip("critic")
            first = False
        engine.approve()

    d = await run_auto(s, on_approval)
    critic_t2 = [r for r in d.records() if r["speaker"] == "critic" and r["topic"] == 2 and r["kind"] == "message"]
    assert critic_t2 == []


async def test_ask_only_one_participant(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=["議題A"], intro=False)
    s = new_session(cfg)
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    await d.until(lambda e: e.type == E.AWAITING_APPROVAL)
    s.engine.ask("gemini", "ストレージ容量の目安は？")
    await d.until(lambda e: e.type == E.SPEECH_END and e.data["round"] == "interjection")
    s.engine.approve()
    await d.until(lambda e: e.type == E.MEETING_END)
    await task
    await s.close()
    asks = [c for c in s.adapters["gemini"].prompts if c.round == "interjection"]
    assert len(asks) == 1 and "ストレージ容量の目安は？" in asks[0].prompt
    assert not any(c.round == "interjection" for c in s.adapters["critic"].prompts)


async def test_stop_cancels_running_speech(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair"},
        "gemini": {"adapter": "dummy", "role": "researcher", "delay_s": 30},
    }, topics=["議題A"], intro=False, critique_rounds=0)
    s = new_session(cfg)
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    await d.until(lambda e: e.type == E.SPEECH_START and e.data["participant"] == "gemini")
    assert s.engine.stop() == "実行中の発言を中断しました"
    await d.until(lambda e: e.type == E.AWAITING_APPROVAL)
    s.engine.approve()
    await d.until(lambda e: e.type == E.MEETING_END)
    await task
    await s.close()
    g = [r for r in d.records() if r["speaker"] == "gemini" and r["round"] == "independent"]
    assert g[-1]["status"] == "skipped"


async def test_resume_after_crash(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair", "delay_s": 0.05},
        "gemini": {"adapter": "dummy", "role": "researcher", "delay_s": 0.05},
    }, topics=["議題A", "議題B"])
    s = new_session(cfg)
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    await d.until(lambda e: e.type == E.AWAITING_APPROVAL)
    s.engine.approve()
    # 議題2の相互批判の途中で「プロセスが落ちる」
    await d.until(lambda e: e.type == E.SPEECH_START and e.data["round"] == "critique" and e.data["topic"] == 2)
    s.engine.interject("落ちる直前の割り込み")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await s.close()
    before = d.records()
    assert before[-1].get("event") == "interrupted"

    s2 = resume_session(s.dir)
    assert s2.engine.phase == "critique" and s2.engine.cur.no == 2
    d2 = await run_auto(s2)
    recs = d2.records()
    # 自己紹介や議題1はやり直さない
    after = recs[len(before):]
    assert not any(r["round"] == "intro" and r["kind"] == "message" for r in after)
    assert not any(r["topic"] == 1 and r["kind"] == "message" for r in after)
    # 議題2の独立回答と割り込みは文脈に戻っている
    crit = [c for c in s2.adapters["gemini"].prompts if c.round == "critique"][0]
    assert "gemini のindependentでの発言" in crit.prompt
    assert "落ちる直前の割り込み" in crit.prompt
    md = (s.dir / "minutes.md").read_text()
    assert md.count("[D-001]") == 1 and "[D-002]" in md and "[D-003]" not in md
    # seq は続き番号
    seqs = [r["seq"] for r in recs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


async def test_resume_while_awaiting_approval(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=["議題A"], intro=False)
    s = new_session(cfg)
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    ev = await d.until(lambda e: e.type == E.AWAITING_APPROVAL)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await s.close()
    s2 = resume_session(s.dir)
    d2 = Driver(s2)
    t2 = asyncio.create_task(s2.engine.run())
    ev2 = await d2.until(lambda e: e.type == E.AWAITING_APPROVAL)
    assert ev2.data["proposal"] == ev.data["proposal"]  # まとめを作り直さない
    s2.engine.approve()
    await d2.until(lambda e: e.type == E.MEETING_END)
    await t2
    await s2.close()
    assert "[D-001]" in (s.dir / "minutes.md").read_text()


async def test_headless_leaves_approvals_pending(tmp_path, new_session):
    cfg = make_config(tmp_path)
    s = new_session(cfg, headless=True)
    d = await run_auto(s, on_approval=lambda e, ev: (_ for _ in ()).throw(AssertionError("no approval in headless")))
    md = (s.dir / "minutes.md").read_text()
    assert "[D-001]" not in md
    assert "## 承認待ち" in md and "### 議題2: 議題B" in md
    assert sum(1 for r in d.records() if r.get("event") == "pending") == 2


async def test_topic_added_at_runtime_and_end(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=[], intro=False)
    s = new_session(cfg)
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    await d.until(lambda e: e.type == E.STATE and e.data["waiting"] == "topic")
    s.engine.add_topic("その場で出した議題")
    await d.until(lambda e: e.type == E.AWAITING_APPROVAL)
    s.engine.add_topic("承認待ちの間に足した議題")
    s.engine.approve()
    await d.until(lambda e: e.type == E.AWAITING_APPROVAL)
    s.engine.end()  # 今の議題で打ち切る
    s.engine.approve()
    await d.until(lambda e: e.type == E.MEETING_END)
    await task
    await s.close()
    md = (s.dir / "minutes.md").read_text()
    assert "### 議題1: その場で出した議題" in md and "### 議題2: 承認待ちの間に足した議題" in md


async def test_per_topic_participants(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=[{"title": "議題A", "participants": ["gemini"]}], intro=False)
    s = new_session(cfg)
    await run_auto(s)
    assert s.adapters["critic"].prompts == []
    assert s.adapters["claude"].prompts  # 議長は常に参加


async def test_manual_advance_waits_for_next(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=["議題A"], intro=False, auto_advance=False)
    s = new_session(cfg)
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    await d.until(lambda e: e.type == E.STATE and e.data["waiting"] == "next")
    assert s.engine.status()["phase"] == "independent"
    s.engine.next()
    await d.until(lambda e: e.type == E.STATE and e.data["waiting"] == "next")
    s.engine.next()
    await d.until(lambda e: e.type == E.AWAITING_APPROVAL)
    s.engine.approve()
    await d.until(lambda e: e.type == E.MEETING_END)
    await task
    await s.close()


async def test_stats_recorded(cfg, new_session):
    s = new_session(cfg)
    d = await run_auto(s)
    st = [r for r in d.records() if r.get("event") == "stats"][0]["stats"]
    assert st["claude"]["calls"] >= 4 and st["gemini"]["chars"] > 0
    assert "呼び出し実績" in (s.dir / "requirements_draft.md").read_text()


async def test_transcript_written_immediately(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair"},
        "gemini": {"adapter": "dummy", "role": "researcher", "delay_s": 30},
    }, topics=["議題A"], intro=False)
    s = new_session(cfg)
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    await d.until(lambda e: e.type == E.SPEECH_END and e.data["participant"] == "claude")
    lines = (s.dir / "transcript.jsonl").read_text().splitlines()
    assert any(json.loads(l)["speaker"] == "claude" and json.loads(l)["kind"] == "message" for l in lines)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await s.close()


async def _crash_at(s, pred):
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    await d.until(pred)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await s.close()


async def test_resume_twice_does_not_duplicate_discarded_messages(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair", "delay_s": 0.05},
        "gemini": {"adapter": "dummy", "role": "researcher", "delay_s": 0.05},
        "critic": {"adapter": "dummy", "role": "third_opinion", "delay_s": 0.05},
    }, topics=["議題A"], intro=False)
    s = new_session(cfg)
    # 相互批判で1人目（claude）が話し終わった後、2人目の途中で落ちる。これを2回繰り返す
    crit_by = lambda who: (lambda e: e.type == E.SPEECH_START and e.data["round"] == "critique" and e.data["participant"] == who)  # noqa: E731
    await _crash_at(s, crit_by("gemini"))
    s2 = resume_session(s.dir)
    await _crash_at(s2, crit_by("gemini"))
    s3 = resume_session(s.dir)
    crit_msgs = [m for m in s3.engine.cur.messages if m.round == "critique"]
    assert crit_msgs == []  # 中断されたラウンドの発言は戻さない
    assert sum(1 for m in s3.engine.cur.messages if m.round == "independent") == 3
    await run_auto(s3)
    summary = [c for c in s3.adapters["claude"].prompts if c.round == "summary"][0].prompt
    assert summary.count("（相互批判）") == 3


async def test_ask_before_topic_goes_into_first_topic(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=[], intro=False)
    s = new_session(cfg)
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    await d.until(lambda e: e.type == E.STATE and e.data["waiting"] == "topic")
    s.engine.ask("gemini", "事前の質問")
    await d.until(lambda e: e.type == E.SPEECH_END and e.data["round"] == "interjection")
    s.engine.add_topic("議題A")
    await d.until(lambda e: e.type == E.AWAITING_APPROVAL)
    s.engine.approve()
    await d.until(lambda e: e.type == E.MEETING_END)
    await task
    await s.close()
    indep = [c for c in s.adapters["claude"].prompts if c.round == "independent"][0].prompt
    assert "事前の質問" in indep


async def test_chair_skipped_still_summarises(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=["議題A"], intro=False)
    s = new_session(cfg)
    s.engine.skip("claude")
    d = await run_auto(s)
    assert any(r["round"] == "summary" and r["speaker"] == "claude" and r["kind"] == "message" for r in d.records())
    assert "[D-001]" in (s.dir / "minutes.md").read_text()


async def test_budget_exhausted_skips_remaining_topics(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=["議題A", "議題B", "議題C"], budget={"max_calls": 7}, intro=False)
    s = new_session(cfg)
    d = await run_auto(s)
    started = [r for r in d.records() if r.get("event") == "topic_start"]
    assert len(started) < 3
    assert any("残りの議題を打ち切って" in r["text"] for r in d.records())


async def test_intro_not_in_topic_context(cfg, new_session):
    s = new_session(cfg)
    await run_auto(s)
    crit = [c for c in s.adapters["gemini"].prompts if c.round == "critique"][0].prompt
    assert "introでの発言" not in crit


def _fake_kill9(s):
    """interrupted の印を残さずに落ちた状態を作る（最後の interrupted 行を消す）。"""
    path = s.dir / "transcript.jsonl"
    lines = path.read_text().splitlines()
    lines = [l for l in lines if '"event": "interrupted"' not in l]
    path.write_text("\n".join(lines) + "\n")


async def test_resume_after_kill9_twice(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair", "delay_s": 0.05},
        "gemini": {"adapter": "dummy", "role": "researcher", "delay_s": 0.05},
        "critic": {"adapter": "dummy", "role": "third_opinion", "delay_s": 0.05},
    }, topics=["議題A"], intro=False)
    s = new_session(cfg)
    crit_gem = lambda e: e.type == E.SPEECH_START and e.data["round"] == "critique" and e.data["participant"] == "gemini"  # noqa: E731
    await _crash_at(s, crit_gem)
    _fake_kill9(s)
    s2 = resume_session(s.dir)
    await _crash_at(s2, crit_gem)
    _fake_kill9(s)
    s3 = resume_session(s.dir)
    assert [m for m in s3.engine.cur.messages if m.round == "critique"] == []
    await s3.close()


async def test_skip_and_end_survive_resume(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=["議題A", "議題B"], intro=False)
    s = new_session(cfg)
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    await d.until(lambda e: e.type == E.AWAITING_APPROVAL)
    s.engine.skip("critic")
    s.engine.end()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await s.close()
    s2 = resume_session(s.dir)
    assert "critic" in s2.engine.absent and s2.engine._end_requested
    d2 = await run_auto(s2)
    assert not any(r.get("event") == "topic_start" and r["topic"] == 2 for r in d2.records())


async def test_pre_topic_interjection_survives_resume(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=[], intro=False)
    s = new_session(cfg)
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    await d.until(lambda e: e.type == E.STATE and e.data["waiting"] == "topic")
    s.engine.interject("議題前のメモ")
    s.engine.add_topic("議題A")
    await d.until(lambda e: e.type == E.SPEECH_START and e.data["round"] == "critique")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await s.close()
    s2 = resume_session(s.dir)
    assert any(m.text == "議題前のメモ" for m in s2.engine.cur.messages)
    await s2.close()


async def test_per_topic_chair_gets_chair_prompt_and_pause_policy(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair"},
        "gemini": {"adapter": "dummy", "role": "researcher"},
    }, topics=[{"title": "議題A", "chair": "gemini"}], intro=False)
    s = new_session(cfg)
    await run_auto(s)
    summ = [c for c in s.adapters["gemini"].prompts if c.round == "summary"]
    assert summ and "議長・書記" in summ[0].prompt
    assert not any(c.round == "summary" and c.topic == 1 for c in s.adapters["claude"].prompts)
    cur_like = type("T", (), {"chair": "gemini"})()
    assert s.engine._failure_policy("gemini", cur_like) == "pause"
    assert s.engine._failure_policy("claude", cur_like) == "skip"


async def test_chair_does_not_participate_when_disabled(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=["議題A"], intro=False, chair_participates=False)
    s = new_session(cfg)
    d = await run_auto(s)
    claude_rounds = {c.round for c in s.adapters["claude"].prompts}
    assert claude_rounds == {"summary"}  # まとめと総括だけ
    assert {c.round for c in s.adapters["gemini"].prompts} >= {"independent", "critique"}
    assert "[D-001]" in (s.dir / "minutes.md").read_text()


async def test_persona_used_as_intro_without_calling_model(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair", "persona": "議長です。読み取り専用で、まとめを書きます。"},
        "gemini": {"adapter": "dummy", "role": "researcher"},  # ペルソナなし → 従来どおり生成
    }, topics=["議題A"])
    s = new_session(cfg)
    d = await run_auto(s)
    intros = {r["speaker"]: r for r in d.records() if r["round"] == "intro" and r["kind"] == "message"}
    assert intros["claude"]["text"] == "議長です。読み取り専用で、まとめを書きます。"
    assert intros["claude"]["source"] == "persona" and not intros["claude"].get("call")
    assert not any(c.round == "intro" for c in s.adapters["claude"].prompts)  # 呼んでいない
    assert any(c.round == "intro" for c in s.adapters["gemini"].prompts)
    # ペルソナは毎ターンの「あなたの立場」に入る
    summ = [c for c in s.adapters["claude"].prompts if c.round == "summary"][0].prompt
    assert "あなたのペルソナ" in summ and "読み取り専用で、まとめを書きます" in summ


# ---------------------------------------------------------------- 延長

def _ext_cfg(tmp_path, chair_opts=None, **meeting):
    return make_config(tmp_path, {
        "chair": {"adapter": "dummy", "role": "chair", **(chair_opts or {})},
        "claude": {"adapter": "dummy", "role": "engineer"},
        "gemini": {"adapter": "dummy", "role": "researcher"},
    }, intro=False, chair_participates=False, **meeting)


async def test_extension_runs_after_all_topics_with_handover(tmp_path, new_session):
    cfg = _ext_cfg(tmp_path, {"extend_topics": [1]}, topics=["議題A", "議題B"])
    s = new_session(cfg)
    order = []

    def on_approval(engine, ev):
        order.append(ev.data["title"])
        if ev.data["topic"] == 1:
            assert ev.data["proposal"]["extension_requested"] and ev.data["proposal"]["handover"]
        engine.approve()

    d = await run_auto(s, on_approval)
    # 延長は全議題（A, B）のあと
    assert order == ["議題A", "議題B", "【延長】議題A"]
    starts = [r for r in d.records() if r.get("event") == "topic_start"]
    assert starts[-1]["extension_of"] == 1 and starts[-1]["topic"] == 3
    # 延長の議題には申し送りだけが渡り、元の議題の発言は渡らない
    ext = [c for c in s.adapters["claude"].prompts if c.topic == 3 and c.round == "independent"][0].prompt
    assert "議題1「議題A」の延長です" in ext and "月いくらまでなら出せるか" in ext
    assert "（議題1、" not in ext.split("# この議題のこれまでの発言")[1]
    # 延長した議題からはさらに延長しない（議長には延長禁止のルールが渡る）
    summ3 = [c for c in s.adapters["chair"].prompts if c.topic == 3 and c.round == "summary"][0].prompt
    assert "extension_requested は必ず false" in summ3
    md = (s.dir / "minutes.md").read_text()
    assert "## 申し送り" in md and "延長（議題3）で議論済み" in md and "月いくらまでなら出せるか" in md
    # 最後の相互批判にだけ延長希望の案内が入る
    crit = [c.prompt for c in s.adapters["claude"].prompts if c.topic == 1 and c.round == "critique"]
    assert "【延長希望】" in crit[-1]


async def test_extension_skipped_when_budget_is_short(tmp_path, new_session):
    # 議題1つ: 独立2 + 批判2 + まとめ1 = 5。延長の見込みは 2*(1+1)+2 = 6 なので max_calls 10 では足りない
    cfg = _ext_cfg(tmp_path, {"extend_topics": [1]}, topics=["議題A"], budget={"max_calls": 10})
    s = new_session(cfg)
    d = await run_auto(s)
    assert not any(r.get("event") == "extension_started" for r in d.records())
    md = (s.dir / "minutes.md").read_text()
    assert "予算が足りないため見送り" in md and "月いくらまでなら出せるか" in md  # 申し送りは残る
    assert "## 総括" in md


async def test_extension_skipped_when_token_budget_is_short(tmp_path, new_session):
    cfg = _ext_cfg(tmp_path, {"extend_topics": [1]}, topics=["議題A"], budget={"max_tokens": 120})
    s = new_session(cfg)
    await run_auto(s)
    assert "トークンの残り" in (s.dir / "minutes.md").read_text()


async def test_handover_is_mandatory(tmp_path, new_session):
    cfg = _ext_cfg(tmp_path, {"extend_topics": [1], "handover_missing": True}, topics=["議題A"])
    s = new_session(cfg)
    await run_auto(s)
    summaries = [c for c in s.adapters["chair"].prompts if c.topic == 1 and c.round == "summary"]
    assert len(summaries) == 2 and "申し送り）が空でした" in summaries[1].prompt
    assert "【延長】議題A" in (s.dir / "minutes.md").read_text()


async def test_user_can_cancel_extension_by_editing(tmp_path, new_session):
    cfg = _ext_cfg(tmp_path, {"extend_topics": [1]}, topics=["議題A"])
    s = new_session(cfg)

    def on_approval(engine, ev):
        p = dict(ev.data["proposal"]); p["extension_requested"] = False
        engine.approve(p)

    d = await run_auto(s, on_approval)
    assert not any(r.get("event") in ("extension_started", "handover") for r in d.records())


async def test_extensions_disabled_and_max(tmp_path, new_session):
    cfg = _ext_cfg(tmp_path, {"extend_topics": [1, 2]}, topics=["議題A", "議題B"], max_extensions=1)
    s = new_session(cfg)
    d = await run_auto(s)
    assert sum(1 for r in d.records() if r.get("event") == "extension_started") == 1
    assert "延長の上限（1回）に達したため見送り" in (s.dir / "minutes.md").read_text()
    cfg2 = _ext_cfg(tmp_path / "b", {"extend_topics": [1]}, topics=["議題A"], extensions=False)
    s2 = new_session(cfg2)
    d2 = await run_auto(s2)
    assert not any(r.get("event") == "extension_started" for r in d2.records())
    summ = [c for c in s2.adapters["chair"].prompts if c.round == "summary" and c.topic == 1][0].prompt
    assert "extension_requested は必ず false" in summ


async def test_extension_queue_survives_resume(tmp_path, new_session):
    cfg = _ext_cfg(tmp_path, {"extend_topics": [1]}, topics=["議題A", "議題B"])
    s = new_session(cfg)
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    ev = await d.until(lambda e: e.type == E.AWAITING_APPROVAL)
    s.engine.approve()  # 議題1を承認 → 延長が待ち行列に入る
    await d.until(lambda e: e.type == E.AWAITING_APPROVAL)  # 議題2の承認待ちで落ちる
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await s.close()
    s2 = resume_session(s.dir)
    assert len(s2.engine.extensions) == 1 and s2.engine.extensions[0]["topic"] == 1
    assert 1 in s2.engine.minutes.handovers
    d2 = await run_auto(s2)
    assert any(r.get("event") == "extension_started" for r in d2.records())


async def test_agents_are_told_when_they_can_read_code(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "chair": {"adapter": "dummy", "role": "chair"},
        "claude": {"adapter": "dummy", "role": "engineer"},
    }, topics=["議題A"], intro=False, chair_participates=False)
    # dummy では道具の案内を出さない（CLI 系だけ）。設定を CLI 系に見せかけて ContextBuilder を直接確かめる
    from dartmouth.config import ParticipantConfig
    cfg.participants["claude"] = ParticipantConfig.model_validate({"adapter": "claude_code", "role": "engineer", "cwd": "/code"})
    from dartmouth.engine.context import ContextBuilder, PromptLibrary
    from .conftest import ROOT
    b = ContextBuilder(cfg, PromptLibrary(ROOT / "prompts"), {"chair": "議長", "claude": "Claude", "user": "ユーザー"})
    kw = dict(participant="claude", round_template="x", topic_no=1, topic_title="t", topic_premise="", messages=[], minutes_md="", members=["chair", "claude"])
    indep = b.build(round_name="independent", **kw)
    crit = b.build(round_name="critique", **kw)
    assert "作業ディレクトリ /code のファイルを読める" in indep and "ファイル名:行番号" in indep
    assert "このラウンドではファイルを読めない" in crit
    assert "ファイルを読める" not in b.build(round_name="independent", **{**kw, "participant": "chair"})


# ---------------------------------------------------------------- 会議のあとにユーザーが延長する

async def test_user_extends_after_meeting_even_when_budget_was_short(tmp_path, new_session):
    from dartmouth.session import extend_session

    cfg = _ext_cfg(tmp_path, {"extend_topics": [1]}, topics=["議題A"], budget={"max_calls": 10})
    s = new_session(cfg)
    await run_auto(s)
    assert "予算が足りないため見送り" in (s.dir / "minutes.md").read_text()
    # 会議のあとに、ユーザーが議題1の延長を指示する（予算は延長1本分が足される）
    s2 = extend_session(s.dir, 1, "費用の上限を詰めたい")
    assert s2.engine.phase == "topic" and s2.engine.topics[-1].extension_of == 1
    d = await run_auto(s2)
    recs = d.records()
    assert any(r.get("event") == "user_extension" for r in recs)
    starts = [r for r in recs if r.get("event") == "topic_start"]
    assert starts[-1]["topic"] == 2 and starts[-1]["title"] == "【延長】議題A"
    ext = [c for c in s2.adapters["claude"].prompts if c.topic == 2 and c.round == "independent"][0].prompt
    assert "月いくらまでなら出せるか" in ext and "費用の上限を詰めたい" in ext
    assert sum(1 for r in recs if r.get("event") == "final") == 2  # 総括を書き直す
    md = (s.dir / "minutes.md").read_text()
    assert "延長（議題2）で議論済み" in md and "[D-002]" in md


async def test_user_extension_without_handover_and_invalid_requests(tmp_path, new_session):
    from dartmouth.session import extend_session, resume_session

    cfg = _ext_cfg(tmp_path, topics=["議題A", "議題B"])
    s = new_session(cfg)
    await run_auto(s)
    # 存在しない議題は断り、ロックも残さない（もう一度開ける）
    with pytest.raises(ValueError):
        extend_session(s.dir, 9)
    s2 = extend_session(s.dir, 2)
    prem = s2.engine.topics[-1].premise
    assert "議長の申し送りはありません" in prem and "議題2の要約" in prem
    # 延長の途中で落ちても、再開すると延長の予算と続きが戻る
    d = Driver(s2)
    task = asyncio.create_task(s2.engine.run())
    await d.until(lambda e: e.type == E.SPEECH_START and e.data["round"] == "independent")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await s2.close()
    s3 = resume_session(s.dir)
    assert not s3.engine._end_requested and s3.engine.cur.no == 3
    d3 = await run_auto(s3)
    assert any(r.get("event") == "approved" and r["topic"] == 3 for r in d3.records())
    # 終わっていない（開始前の）会議は延長できない（再開を使う）
    s4 = new_session(make_config(tmp_path, topics=["議題X"]))
    with pytest.raises(ValueError):
        s4.engine.extend_after_done(1)


async def test_chair_research_memo_reaches_independent_round(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair"},
        "gemini": {"adapter": "dummy", "role": "researcher"},
    }, topics=["議題A"], intro=False, research="chair", tools="off", chair_participates=False)
    s = new_session(cfg)
    d = await run_auto(s)
    recs = d.records()
    memo = [r for r in recs if r["kind"] == "message" and r["round"] == "research"]
    assert [r["speaker"] for r in memo] == ["claude"] and memo[0]["topic"] == 1
    # 調査メモは独立回答より前で、独立回答のプロンプトに入る（他の席の回答は入らない）
    first_indep = next(i for i, r in enumerate(recs) if r["round"] == "independent" and r["kind"] == "message")
    assert recs.index(memo[0]) < first_indep
    research_call = [c for c in s.adapters["claude"].prompts if c.round == "research"][0]
    assert "検索語に個人の情報を入れない" in research_call.prompt
    indep = [c for c in s.adapters["gemini"].prompts if c.round == "independent"][0]
    assert "（議長の事前調査（Web））" in indep.prompt and "claude のresearchでの発言" in indep.prompt


async def test_research_off_by_default(cfg, new_session):
    s = new_session(cfg)
    d = await run_auto(s)
    assert not [r for r in d.records() if r["round"] == "research"]


async def test_research_failure_does_not_stop_topic(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "claude": {"adapter": "dummy", "role": "chair", "fail_rounds": ["research"]},
        "gemini": {"adapter": "dummy", "role": "researcher"},
    }, topics=["議題A"], intro=False, research="chair")
    s = new_session(cfg)
    d = await run_auto(s)
    recs = d.records()
    assert any(r.get("event") == "research_skipped" for r in recs)
    assert "[D-001]" in (s.dir / "minutes.md").read_text()


def test_shorten_redirect_links():
    from dartmouth.engine.flow import shorten_redirect_links

    t = "出典: [sbisec.co.jp](https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_x-1) / [fsa.go.jp](https://www.fsa.go.jp/a)"
    assert shorten_redirect_links(t) == "出典: sbisec.co.jp / [fsa.go.jp](https://www.fsa.go.jp/a)"
