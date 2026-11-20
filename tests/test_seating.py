from __future__ import annotations

import asyncio

from dartmouth.adapters.dummy import DummyAdapter
from dartmouth.config import Config
from dartmouth.engine import events as E
from dartmouth.session import resume_session

from .conftest import ROOT, Driver, run_auto

SECRET = "血圧は135/88"


def _cfg(tmp_path, seat_plan=None, **meeting):
    return Config.model_validate({
        "meeting": {"name": "seat", "purpose": "席決めの確認", "topics": [{"title": "議題A", "premise": f"前提 <private>{SECRET}</private>"}],
                    "intro": False, "chair_participates": False, **meeting},
        "participants": {"chair": {"adapter": "dummy", "role": "chair", "seat_plan": seat_plan or []}},
        "seating": {"personas": ["engineer", "researcher", "critic", "security"], "min_seats": 2, "max_seats": 3,
                    "backends": {"fast": {"adapter": "dummy", "note": "速い"},
                                 "free": {"adapter": "dummy", "share_scope": "public_only", "note": "無料枠"}}},
        "prompts_dir": str(ROOT / "prompts"),
        "meetings_dir": str(tmp_path / "meetings"),
    })


def _overrides():
    return {"backend:fast": DummyAdapter("fast", "member", 30, {}), "backend:free": DummyAdapter("free", "member", 30, {})}


async def test_chair_picks_personas_and_backends(tmp_path, new_session):
    plan = [{"persona": "engineer", "backend": "fast", "reason": "実装の見積もりが要る"},
            {"persona": "critic", "backend": "free", "reason": "前提を疑う役"}]
    ads = _overrides()
    s = new_session(_cfg(tmp_path, plan), adapters=ads)
    d = await run_auto(s)
    seat = next(r for r in d.records() if r.get("event") == "seating")
    assert [x["persona"] for x in seat["seats"]] == ["engineer", "critic"] and "実装の見積もりが要る" in seat["text"]
    assert seat["seats"][0]["name"] == "実装・運用担当"
    # 席ごとに、選ばれた経路で発言している
    msgs = [r for r in d.records() if r.get("call") and r["round"] == "independent"]
    assert {(r["speaker"], r["backend"]) for r in msgs} == {("engineer", "fast"), ("critic", "free")}
    # ペルソナの本文が渡り、機密は public_only の経路の席にだけ伏せ字で届く
    eng = [c.prompt for c in ads["backend:fast"].prompts if c.participant == "engineer"][0]
    crit = [c.prompt for c in ads["backend:free"].prompts if c.participant == "critic"][0]
    assert "最小で動くもの" in eng and SECRET in eng
    assert "前提を疑い" in crit and SECRET not in crit
    # 席決めの判断材料に、経路のメモと機密の扱いが入る
    plan_prompt = [c for c in s.adapters["chair"].prompts if c.round == "plan"][0].prompt
    assert "fast: dummy" in plan_prompt and "機密は伏せ字で届く" in plan_prompt and "engineer: 実装・運用担当" in plan_prompt


async def test_invalid_seating_is_filled_automatically(tmp_path, new_session):
    bad = [{"persona": "nobody", "backend": "fast", "reason": "x"}, {"persona": "engineer", "backend": "ghost", "reason": "x"}]
    s = new_session(_cfg(tmp_path, bad), adapters=_overrides())
    d = await run_auto(s)
    seat = next(r for r in d.records() if r.get("event") == "seating")
    assert len(seat["seats"]) == 2 and all(x["persona"] in ("engineer", "researcher", "critic", "security") for x in seat["seats"])
    assert "自動で補った" in seat["text"]


async def test_auto_approval_runs_without_human(tmp_path, new_session):
    s = new_session(_cfg(tmp_path, approval="auto"), adapters=_overrides())
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    await d.until(lambda e: e.type == E.MEETING_END, timeout=15)
    await task
    await s.close()
    assert not any(e.type == E.AWAITING_APPROVAL for e in d.events)
    recs = d.records()
    ap = next(r for r in recs if r.get("event") == "approved")
    assert ap["auto"] and "自動で承認" in ap["text"]
    assert "[D-001]" in (s.dir / "minutes.md").read_text()


async def test_seats_survive_resume(tmp_path, new_session):
    plan = [{"persona": "security", "backend": "fast", "reason": "機密の扱い"}, {"persona": "researcher", "backend": "free", "reason": "調べる"}]
    s = new_session(_cfg(tmp_path, plan), adapters=_overrides())
    d = Driver(s)
    task = asyncio.create_task(s.engine.run())
    await d.until(lambda e: e.type == E.AWAITING_APPROVAL)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await s.close()
    s2 = resume_session(s.dir, adapters=_overrides())
    assert s2.engine.cur.members == ["chair", "security", "researcher"]
    assert s2.engine.seat_backend == {"security": "fast", "researcher": "free"}
    assert s2.engine.names["security"] == "セキュリティ・プライバシー担当"
    await s2.close()


async def test_exhausted_claude_backends_are_avoided(tmp_path, new_session):
    from dartmouth.adapters.dummy import DummyAdapter

    cfg = Config.model_validate({
        "meeting": {"name": "q", "topics": ["議題A", "議題B"], "intro": False, "chair_participates": False, "approval": "auto"},
        "participants": {"chair": {"adapter": "dummy", "role": "chair",
                                   "seat_plan": [{"persona": "engineer", "backend": "sonnet", "reason": "x"},
                                                 {"persona": "critic", "backend": "free", "reason": "y"}]}},
        "seating": {"personas": ["engineer", "critic", "researcher"], "min_seats": 2, "max_seats": 2,
                    "backends": {"sonnet": {"adapter": "claude_code", "model": "sonnet"},
                                 "free": {"adapter": "dummy", "share_scope": "public_only"}}},
        "prompts_dir": str(ROOT / "prompts"), "meetings_dir": str(tmp_path / "meetings"),
    })

    class Limited(DummyAdapter):
        async def respond(self, ctx):
            from dartmouth.adapters.base import AdapterError
            raise AdapterError("claude error (success): You've hit your 5-hour limit · resets 5:50pm")

    ads = {"backend:sonnet": Limited("sonnet", "member", 30, {}), "backend:free": DummyAdapter("free", "member", 30, {})}
    s = new_session(cfg, adapters=ads)
    d = await run_auto(s)
    seats = [r["seats"] for r in d.records() if r.get("event") == "seating"]
    assert seats[0][0]["backend"] == "sonnet"  # 議題1は議長の指定どおり
    assert all(x["backend"] == "free" for x in seats[1])  # 議題2は利用枠に当たった Claude を避ける
    assert "sonnet" in s.engine.exhausted


async def test_gemini_without_key_is_not_offered(tmp_path, new_session, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from dartmouth.session import build_backends

    cfg = Config.model_validate({
        "participants": {"chair": {"adapter": "dummy", "role": "chair"}},
        "seating": {"personas": ["engineer"], "min_seats": 1, "max_seats": 1,
                    "backends": {"fast": {"adapter": "dummy"}, "gem": {"adapter": "gemini_api", "model": "m"}}},
    })
    assert set(build_backends(cfg)) == {"fast"}
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert set(build_backends(cfg)) == {"fast", "gem"}


def test_window_usage_counts_recent_claude_tokens(tmp_path):
    import json
    from datetime import datetime, timedelta

    from dartmouth.storage.archive import window_usage

    d = tmp_path / "m1"; d.mkdir()
    (d / "config.yaml").write_text("participants:\n  c: {adapter: claude_code, role: chair}\n  g: {adapter: gemini_api, role: member}\n")
    now = datetime.now().astimezone()
    rows = [
        {"ts": (now - timedelta(hours=1)).isoformat(), "speaker": "c", "call": True, "kind": "message", "tokens": 1000,
         "usage": {"cache_read_input_tokens": 400}},
        {"ts": (now - timedelta(hours=6)).isoformat(), "speaker": "c", "call": True, "kind": "message", "tokens": 9999},
        {"ts": (now - timedelta(minutes=5)).isoformat(), "speaker": "g", "call": True, "kind": "message", "tokens": 50},
        {"ts": (now - timedelta(minutes=1)).isoformat(), "speaker": "c", "call": True, "kind": "error", "tokens": None,
         "text": "You've hit your 5-hour limit"},
    ]
    (d / "transcript.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    w = window_usage([tmp_path])
    assert w["claude_tokens"] == 600 and w["claude_calls"] == 2 and w["limit_hits"] == 1
    assert w["by_adapter"]["gemini_api"]["tokens_excl_cache_read"] == 50


def _rot_cfg(tmp_path, topics):
    return Config.model_validate({
        "meeting": {"name": "rot", "topics": topics, "intro": False, "chair_participates": False, "approval": "auto"},
        "participants": {"chair": {"adapter": "dummy", "role": "chair",
                                   "seat_plan": [{"persona": "critic", "backend": "fast", "reason": "x"}],
                                   "seat_score_map": {"manager": 4, "critic": 2}}},
        "seating": {"personas": ["manager", "critic", "engineer"], "min_seats": 1, "max_seats": 1,
                    "fixed": [{"persona": "manager", "backends": ["g1", "g2"]}],
                    "backends": {"fast": {"adapter": "dummy"}, "g1": {"adapter": "dummy", "model": "gem-a"},
                                 "g2": {"adapter": "dummy", "model": "gem-b"}}},
        "prompts_dir": str(ROOT / "prompts"), "meetings_dir": str(tmp_path / "meetings"),
    })


async def test_fixed_seat_rotates_and_scores_build_history(tmp_path, new_session):
    from dartmouth.storage.archive import seat_history

    ads = {f"backend:{b}": DummyAdapter(b, "member", 30, {}) for b in ("fast", "g1", "g2")}
    s = new_session(_rot_cfg(tmp_path, ["議題A", "議題B", "議題C"]), adapters=ads)
    d = await run_auto(s)
    seatings = [r["seats"] for r in d.records() if r.get("event") == "seating"]
    # 固定の席（マネージャー）はかならず座り、経路は回数の少ないほうから回る
    assert all(s_[0]["persona"] == "manager" and s_[0]["fixed"] for s_ in seatings)
    assert [s_[0]["backend"] for s_ in seatings] == ["g1", "g2", "g1"]
    # 議長は固定の席のペルソナを選ばない（席決めの説明に入る）
    plan = [c.prompt for c in s.adapters["chair"].prompts if c.round == "plan"][0]
    assert "固定の席" in plan and "manager" in plan
    # 議長の採点が記録され、ペルソナ×経路の成績になる
    sc = [r for r in d.records() if r.get("event") == "seat_scores"]
    assert len(sc) == 3 and {x["persona"] for x in sc[0]["scores"]} == {"manager", "critic"}
    hist = seat_history([tmp_path / "meetings"])
    assert hist["manager|g1"]["seated"] == 2 and hist["manager|g1"]["avg_score"] == 4.0
    assert hist["critic|fast"]["avg_score"] == 2.0 and hist["manager|g2"]["tokens_per_call"]
    # 次の会議では、成績が席決めの材料として議長に渡る
    s2 = new_session(_rot_cfg(tmp_path, ["議題D"]), adapters={f"backend:{b}": DummyAdapter(b, "member", 30, {}) for b in ("fast", "g1", "g2")})
    d2 = await run_auto(s2)
    plan2 = [c.prompt for c in s2.adapters["chair"].prompts if c.round == "plan"][0]
    assert "これまでの組み合わせの成績" in plan2 and "manager × g1: 座った 2 回、議長の採点 4.0" in plan2
    assert [r["seats"] for r in d2.records() if r.get("event") == "seating"][0][0]["backend"] == "g2"  # 回数の少ない g2


async def test_explore_seat_tries_least_used_backend(tmp_path, new_session):
    cfg = Config.model_validate({
        "meeting": {"name": "ex", "topics": ["議題A", "議題B"], "intro": False, "chair_participates": False, "approval": "auto"},
        "participants": {"chair": {"adapter": "dummy", "role": "chair",
                                   "seat_plan": [{"persona": "critic", "backend": "fast", "reason": "x"},
                                                 {"persona": "engineer", "backend": "fast", "reason": "y"}]}},
        "seating": {"personas": ["critic", "engineer"], "min_seats": 2, "max_seats": 2, "explore_seats": 1,
                    "backends": {"fast": {"adapter": "dummy"}, "slow": {"adapter": "dummy"}}},
        "prompts_dir": str(ROOT / "prompts"), "meetings_dir": str(tmp_path / "meetings"),
    })
    ads = {f"backend:{b}": DummyAdapter(b, "member", 30, {}) for b in ("fast", "slow")}
    d = await run_auto(new_session(cfg, adapters=ads))
    seatings = [r["seats"] for r in d.records() if r.get("event") == "seating"]
    assert all(x["backend"] == "fast" for x in seatings[0])  # 履歴がないうちは議長の指定どおり
    assert sum(1 for x in seatings[1] if x.get("explored")) == 1  # 2議題目は1席だけ、試した回数の少ない slow に替わる
    assert {x["backend"] for x in seatings[1]} == {"fast", "slow"}


async def test_non_claude_quota_hit_excludes_only_that_backend_and_persists_in_window(tmp_path, new_session):
    cfg = Config.model_validate({
        "meeting": {"name": "q2", "topics": ["議題A", "議題B"], "intro": False, "chair_participates": False, "approval": "auto"},
        "participants": {"chair": {"adapter": "dummy", "role": "chair",
                                   "seat_plan": [{"persona": "engineer", "backend": "agy", "reason": "x"},
                                                 {"persona": "critic", "backend": "agy-flash", "reason": "y"}]}},
        "seating": {"personas": ["engineer", "critic"], "min_seats": 2, "max_seats": 2,
                    "backends": {"agy": {"adapter": "antigravity_cli", "model": "gemini-4-hydrogen-medium"},
                                 "agy-flash": {"adapter": "antigravity_cli", "model": "gemini-3.8-flash-medium"}}},
        "prompts_dir": str(ROOT / "prompts"), "meetings_dir": str(tmp_path / "meetings"),
    })

    class QuotaErr(DummyAdapter):
        async def respond(self, ctx):
            from dartmouth.adapters.base import AdapterError
            raise AdapterError("agy quota exceeded (RESOURCE_EXHAUSTED)")

    ads1 = {"backend:agy": QuotaErr("agy", "member", 30, {}), "backend:agy-flash": DummyAdapter("agy-flash", "member", 30, {})}
    s1 = new_session(cfg, adapters=ads1)
    d1 = await run_auto(s1)
    seats1 = [r["seats"] for r in d1.records() if r.get("event") == "seating"]
    assert "agy" in s1.engine.exhausted and "agy-flash" not in s1.engine.exhausted
    assert all(x["backend"] == "agy-flash" for x in seats1[1])

    # 次の会議でも、直近5時間の履歴から agy を使わない
    cfg2 = Config.model_validate({
        **cfg.model_dump(),
        "meeting": {"name": "q3", "topics": ["議題C"], "intro": False, "chair_participates": False, "approval": "auto"},
    })
    ads2 = {f"backend:{b}": DummyAdapter(b, "member", 30, {}) for b in ("agy", "agy-flash")}
    s2 = new_session(cfg2, adapters=ads2)
    d2 = await run_auto(s2)
    seats2 = [r["seats"] for r in d2.records() if r.get("event") == "seating"]
    assert all(x["backend"] == "agy-flash" for x in seats2[0])


async def test_adapter_5h_tokens_excludes_over_budget_adapter(tmp_path, new_session):
    import json
    from datetime import datetime, timedelta

    m = tmp_path / "meetings" / "prev"
    m.mkdir(parents=True)
    (m / "config.yaml").write_text("participants:\n  a: {adapter: antigravity_cli, role: member}\n")
    now = datetime.now().astimezone()
    (m / "transcript.jsonl").write_text(json.dumps({
        "ts": (now - timedelta(minutes=10)).isoformat(),
        "speaker": "a", "call": True, "kind": "message", "tokens": 50000,
    }) + "\n")

    cfg = Config.model_validate({
        "meeting": {"name": "b5h", "topics": ["議題A"], "intro": False, "chair_participates": False, "approval": "auto"},
        "budget": {"adapter_5h_tokens": {"antigravity_cli": 40000}},
        "participants": {"chair": {"adapter": "dummy", "role": "chair",
                                   "seat_plan": [{"persona": "engineer", "backend": "agy", "reason": "x"}]}},
        "seating": {"personas": ["engineer"], "min_seats": 1, "max_seats": 1,
                    "backends": {"agy": {"adapter": "antigravity_cli", "model": "m"},
                                 "free": {"adapter": "dummy"}}},
        "prompts_dir": str(ROOT / "prompts"), "meetings_dir": str(tmp_path / "meetings"),
    })
    ads = {"backend:agy": DummyAdapter("agy", "member", 30, {}), "backend:free": DummyAdapter("free", "member", 30, {})}
    s = new_session(cfg, adapters=ads)
    d = await run_auto(s)
    seats = [r["seats"] for r in d.records() if r.get("event") == "seating"]
    assert seats[0][0]["backend"] == "free"


async def test_mid_call_limit_failover_switches_provider_immediately(tmp_path, new_session):
    cfg = Config.model_validate({
        "meeting": {"name": "fo", "topics": ["議題A"], "intro": False, "chair_participates": False, "approval": "auto", "critique_rounds": 0},
        "participants": {"chair": {"adapter": "dummy", "role": "chair",
                                   "seat_plan": [{"persona": "engineer", "backend": "agy", "reason": "x"}]}},
        "seating": {"personas": ["engineer"], "min_seats": 1, "max_seats": 1,
                    "backends": {"agy": {"adapter": "antigravity_cli", "model": "gemini-4-hydrogen-medium"},
                                 "sonnet": {"adapter": "claude_code", "model": "sonnet"}}},
        "prompts_dir": str(ROOT / "prompts"), "meetings_dir": str(tmp_path / "meetings"),
    })

    class QuotaErr(DummyAdapter):
        async def respond(self, ctx):
            from dartmouth.adapters.base import AdapterError
            raise AdapterError("agy quota exceeded (RESOURCE_EXHAUSTED)")

    ads = {"backend:agy": QuotaErr("agy", "member", 30, {}), "backend:sonnet": DummyAdapter("sonnet", "member", 30, {})}
    s = new_session(cfg, adapters=ads)
    d = await run_auto(s)
    recs = d.records()
    assert any(r.get("event") == "failover" and r.get("backend") == "sonnet" for r in recs)
    assert any(r.get("kind") == "message" and r.get("speaker") == "engineer" for r in recs)


def test_seat_history_aggregates_opus_and_argon_separately(tmp_path):
    import json
    from dartmouth.storage.archive import seat_history

    m1 = tmp_path / "m_opus"; m1.mkdir()
    (m1 / "config.yaml").write_text("participants:\n  chair: {adapter: claude_code, model: opus, role: chair}\n")
    (m1 / "transcript.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"event": "seating", "topic": 1, "seats": [{"persona": "engineer", "backend": "agy", "model": "gemini-4-hydrogen-medium"}]},
        {"event": "seat_scores", "topic": 1, "scores": [{"persona": "engineer", "backend": "agy", "score": 5}]},
    ]))
    m2 = tmp_path / "m_argon"; m2.mkdir()
    (m2 / "config.yaml").write_text("participants:\n  chair: {adapter: antigravity_cli, model: gemini-4-argon-medium, role: chair}\n")
    (m2 / "transcript.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"event": "seating", "topic": 1, "seats": [{"persona": "engineer", "backend": "agy", "model": "gemini-4-hydrogen-medium"}]},
        {"event": "seat_scores", "topic": 1, "chair_model": "gemini-4-argon-medium", "chair_adapter": "antigravity_cli",
         "scores": [{"persona": "engineer", "backend": "agy", "score": 3}]},
    ]))

    h = seat_history([tmp_path])["engineer|agy"]
    assert h["avg_score"] == 4.0 and h["rated"] == 2
    assert h["opus_avg_score"] == 5.0 and h["opus_rated"] == 1
    assert h["argon_avg_score"] == 3.0 and h["argon_rated"] == 1
