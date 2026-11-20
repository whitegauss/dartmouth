import json
import os

import pytest
from fastapi.testclient import TestClient

from dartmouth.adapters.base import Reply
from dartmouth.ledger import Ledger, sync
from dartmouth.ledger.classify import classify
from dartmouth.storage.archive import load_item_status

from .conftest import ROOT, make_config, run_auto


async def _meeting(tmp_path, new_session, topics=None):
    cfg = make_config(tmp_path, topics=topics or [{"title": "議題A", "premise": "予算は月1万円"}, "議題B"])
    s = new_session(cfg)
    await run_auto(s)
    return s.dir


class FakeAI:
    name = "fake"
    options = {"model": "fake-1"}

    def __init__(self, fn):
        self.fn = fn
        self.prompts = []

    async def respond(self, ctx):
        self.prompts.append(ctx.prompt)
        return Reply(json.dumps(self.fn(ctx.prompt), ensure_ascii=False))

    async def close(self):
        pass


async def test_sync_imports_items_and_meeting_end_updates_ledger(tmp_path, new_session):
    d = await _meeting(tmp_path, new_session)
    # 会議の終わりに自動で取り込まれている（テストでは DARTMOUTH_LEDGER は一時ディレクトリ）
    auto = Ledger(os.environ["DARTMOUTH_LEDGER"])
    assert auto.counts()["items"] > 0
    auto.close()
    lg = Ledger(tmp_path / "l.sqlite")
    r = sync(lg, [tmp_path / "meetings"])
    assert r == {"meetings": 1, "new_items": r["new_items"]} and r["new_items"] >= 7
    kinds = {it["src_kind"] for it in lg.items(status=None)}
    assert kinds == {"decision", "question", "todo", "premise"}
    prem = lg.items(kind="premise")[0]
    assert prem["text"] == "予算は月1万円" and prem["uid"] == f"{d.name}:P-001"
    # 2回目は増えない。台帳だけで付けた状態は取り込み直しても残る
    q = lg.items(kind="question")[0]["uid"]
    lg.set_status(q, "dropped", "不要")
    assert sync(lg, [tmp_path / "meetings"])["new_items"] == 0
    assert lg.item(q)["status"] == "dropped"
    assert lg.items(text="残課題")  # 全文検索（trigram）


async def test_classify_splits_questions_links_deps_and_proposes_milestones(tmp_path, new_session):
    d = await _meeting(tmp_path, new_session)
    lg = Ledger(tmp_path / "l.sqlite")
    sync(lg, [tmp_path / "meetings"])
    pid = lg.projects()[0]["id"]
    lg.add_milestone(pid, "M1", "要件確定")
    D1, D2, Q1, T1 = (f"{d.name}:{x}" for x in ("D-001", "D-002", "Q-001", "T-001"))

    def answer(prompt):
        assert "要件確定" in prompt and "議題1の要約" in prompt  # マイルストーンと議題の記録が渡る
        out = []
        for uid in [u for u in (D1, D2, Q1, f"{d.name}:Q-002", T1, f"{d.name}:T-002", f"{d.name}:P-001") if u in prompt]:
            c = {"uid": uid, "kind": "decision", "rationale": "合意したから", "needs": "", "executable": "auto",
                 "domain": "software", "milestone": "M1", "requires": [], "supersedes": [], "defines_milestone": None}
            if uid == Q1:
                c.update(kind="unknown", needs="月額の見積もりが分かればよい", executable="blocked", milestone=None)
            if uid.endswith(":T-001"):
                c.update(kind="todo", requires=[Q1])
            if uid.endswith(":T-002"):
                c.update(kind="todo")
            if uid.endswith(":Q-002"):
                c.update(kind="discussion")
            if uid.endswith(":P-001"):
                c.update(kind="premise", rationale="予算は月1万円まで")
            if uid == D2:
                c.update(defines_milestone={"key": "M2", "title": "設計確定", "criteria": "構成図がある"}, supersedes=[D1])
            out.append(c)
        return {"items": out}

    ai = FakeAI(answer)
    r = await classify(lg, ai, (ROOT / "prompts" / "ledger_classify.md").read_text(), {d.name: d})
    assert r["classified"] == lg.counts()["items"] and r["failed"] == 0 and r["proposed_milestones"] == 1
    q = lg.item(Q1)
    assert q["kind"] == "unknown" and q["needs"].startswith("月額") and q["executable"] == "blocked"
    assert lg.item(D1)["status"] == "superseded"
    assert [m["key"] for m in lg.milestones(pid)] == ["M1", "M2"] and lg.milestones(pid)[1]["status"] == "proposed"
    # 「これがないからこれができない」と「これがあるからこれができる」
    assert [b["dst"] for b in lg.blockers(T1)] == [Q1]
    assert T1 in {x["uid"] for x in lg.blocked()}
    assert [u["uid"] for u in lg.unlocks(Q1)] == [T1]
    assert T1 not in {x["uid"] for x in lg.ready()} and D2 in {x["uid"] for x in lg.ready()}
    lg.set_status(Q1, "done")
    assert not lg.blockers(T1) and T1 in {x["uid"] for x in lg.ready()}
    # 会議の決定は決定のまま、未決は不明点・協議事項にしかならない
    assert lg.item(f"{d.name}:Q-002")["kind"] == "discussion"
    assert lg.item(f"{d.name}:P-001")["status"] == "open"  # 前提が読み取れたものは残る
    brief = lg.brief(pid)
    assert "## マイルストーン" in brief and "M1 要件確定" in brief and "## 協議事項" in brief and "根拠: 合意したから" in brief


async def test_matrix_positions_and_milestone_dependency(tmp_path, new_session):
    d = await _meeting(tmp_path, new_session)
    lg = Ledger(tmp_path / "l.sqlite")
    sync(lg, [tmp_path / "meetings"])
    pid = lg.projects()[0]["id"]
    lg.db.execute("INSERT INTO projects(id, name, theme) VALUES('other', 'other', ?)", (pid,))
    m1, m2 = lg.add_milestone(pid, "M1", "要件"), lg.add_milestone(pid, "M2", "設計")
    D1, T1 = f"{d.name}:D-001", f"{d.name}:T-001"
    lg.update_item(D1, {"milestone": m1})
    lg.update_item(T1, {"milestone": m2})
    mx = lg.matrix(pid)
    row = next(r for r in mx["rows"] if r["project"]["id"] == pid)
    assert [c["state"] for c in row["cells"]] == ["doing", "todo"] and row["current"] == m1
    other = next(r for r in mx["rows"] if r["project"]["id"] == "other")
    assert [c["state"] for c in other["cells"]] == ["empty", "empty"]  # 横に並べて比べる
    # T1 は段階 M1 に達していないとできない
    lg.add_dep(T1, f"m:{m1}")
    assert [b["dst"] for b in lg.blockers(T1)] == [f"m:{m1}"]
    lg.set_status(D1, "done")
    assert not lg.blockers(T1)
    rows = {r["project"]["id"]: r for r in lg.matrix(pid)["rows"]}
    assert rows[pid]["cells"][0]["state"] == "done" and rows[pid]["current"] == m2
    lg.set_project_milestone("other", m1, "na")
    assert {r["project"]["id"]: r for r in lg.matrix(pid)["rows"]}["other"]["cells"][0]["state"] == "na"
    # 循環する依存は断る
    lg.add_dep(D1, f"{d.name}:Q-001")
    with pytest.raises(ValueError):
        lg.add_dep(f"{d.name}:Q-001", D1)


def test_ledger_web_api(tmp_path, monkeypatch):
    from dartmouth.ui.web import COOKIE, Hub, create_app

    from .test_web import _wait, _write_base_config

    hub = Hub([_write_base_config(tmp_path)], [tmp_path / "meetings"])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        c.post("/api/start", json={"config": 0, "topics": [{"title": "議題A", "premise": "前提X"}]})
        _wait(c, lambda s: s.get("waiting") == "approval")
        c.post("/api/command", json={"line": "/approve"})
        mid = _wait(c, lambda s: not s["meeting"]["running"])["meeting"]["id"]
        c.post("/api/close")
        assert c.post("/api/ledger/sync").status_code == 200
        d = c.get("/api/ledger").json()
        assert d["counts"]["items"] >= 4 and d["themes"]
        theme = d["themes"][0]
        r = c.post("/api/ledger/milestone", json={"theme": theme, "key": "M1", "title": "要件確定"})
        mid_ms = r.json()["id"]
        uid = f"{mid}:T-001"
        assert c.post(f"/api/ledger/item/{uid}", json={"fields": {"milestone": mid_ms, "executable": "auto"}}).status_code == 200
        assert c.post("/api/ledger/dep", json={"src": uid, "dst": f"{mid}:Q-001"}).status_code == 200
        it = c.get(f"/api/ledger/item/{uid}").json()
        assert it["milestone_key"] == "M1" and it["blockers"][0]["dst"] == f"{mid}:Q-001"
        assert c.get("/api/ledger?kind=todo").json()["items"][0]["blocked_by"] == [f"{mid}:Q-001"]
        mx = c.get(f"/api/ledger/matrix/{theme}").json()
        assert mx["milestones"][0]["key"] == "M1" and mx["rows"][0]["cells"][0]["total"] == 1
        # 状態の変更は会議の item_status.json にも反映する
        assert c.post(f"/api/ledger/item/{uid}/status", json={"status": "done", "note": "済"}).status_code == 200
        assert load_item_status(tmp_path / "meetings" / mid)["T-001"]["done"]
        assert "# 案件" in c.get(f"/api/ledger/brief/{d['projects'][0]['id']}").text
        pk = c.get(f"/api/ledger/pack?project={d['projects'][0]['id']}")
        assert pk.headers["content-type"].startswith("text/markdown") and f"[{uid}]" in pk.text
        assert "# 台帳コンテキストパック" in c.get("/api/ledger/pack").text
        assert all(i["ball_now"] == "self" for i in c.get("/api/ledger?ball=self").json()["items"])
        assert c.post("/api/ledger/dep", json={"src": uid, "dst": "nope"}).status_code == 404
        # ホームの議題候補: 残っている未決（協議事項・不明点・未分類）だけ。止めている数の多いものが先
        pid = d["projects"][0]["id"]
        cand = c.get(f"/api/ledger/candidates?project={pid}").json()["items"]
        assert [x["uid"] for x in cand] == [f"{mid}:Q-001"] and cand[0]["unlocks"] == 0
        c.post(f"/api/ledger/item/{mid}:Q-001/status", json={"status": "done"})
        assert c.get(f"/api/ledger/candidates?project={pid}").json()["items"] == []


def test_ball_of_rules():
    from dartmouth.ledger import ball_of

    base = {"status": "open", "kind": "todo", "owner": "", "executable": "", "ball": ""}
    assert ball_of({**base, "status": "done"}, False) == "none"
    assert ball_of({**base, "ball": "external"}, True) == "external"  # 手で付けた値が最優先
    assert ball_of({**base, "status": "held"}, False) == "held"
    assert ball_of(base, True) == "blocked"
    assert ball_of({**base, "owner": "user"}, False) == "self"
    assert ball_of({**base, "owner": "engineer"}, False) == "ai"  # 古い会議の参加者 id は AI 扱い
    assert ball_of({**base, "owner": "external"}, False) == "external"
    assert ball_of({**base, "kind": "discussion"}, False) == "self"
    assert ball_of({**base, "kind": "unknown"}, False) == "ai"
    assert ball_of({**base, "kind": "decision"}, False) == "none"  # 方針の決定は誰かの番ではない
    assert ball_of({**base, "kind": "decision", "executable": "auto"}, False) == "ai"
    assert ball_of({**base, "kind": "premise"}, False) == "none"


async def test_context_pack_groups_by_meeting_and_shows_ball(tmp_path, new_session):
    mdir = await _meeting(tmp_path, new_session)
    lg = Ledger(tmp_path / "ledger.sqlite")
    sync(lg, [mdir.parent])
    pid = lg.projects()[0]["id"]
    mid = mdir.name
    lg.update_item(f"{mid}:Q-001", {"ball": "external", "rationale": "外の回答待ち"})
    with pytest.raises(ValueError):
        lg.update_item(f"{mid}:Q-001", {"ball": "someone"})
    t = lg.context_pack(pid)
    assert t.startswith(f"# 案件「") and "> 読み方:" in t
    # 索引（ボール別）→ 会議 → 議題 → 種類 の順で、ID と固定フィールドを持つ
    assert t.index("## ボール別の未完了") < t.index(f"## 会議 ") < t.index("### 議題1: 議題A")
    assert "### 外部待ち（1）" in t and f"- [{mid}:Q-001]" in t
    assert "#### 決定事項" in t and "#### 未決事項" in t and "#### ToDo" in t
    assert "| ボール: 外部待ち" in t and "  - 理由: 外の回答待ち" in t
    lg.set_status(f"{mid}:T-001", "done")
    assert f"[{mid}:T-001]" not in lg.context_pack(pid, open_only=True)
    # 全案件: 見出しを1段下げ、読み方は先頭に1回だけ
    allp = lg.context_pack()
    assert allp.startswith("# 台帳コンテキストパック") and allp.count("> 読み方:") == 1 and "\n## 案件「" in allp
