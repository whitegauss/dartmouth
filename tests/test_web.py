from __future__ import annotations

import asyncio
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from dartmouth.ui import web
from dartmouth.ui.web import COOKIE, bind_hosts, create_app, tailscale_ipv4

from .conftest import make_config


@pytest.fixture
def client(tmp_path, new_session):
    s = new_session(make_config(tmp_path, topics=["議題A"], intro=False))
    return TestClient(create_app("tok-123", session=s)), s

# テスト用の架空の Tailscale アドレス
FAKE_TS = "100.101.102.103"  # leakscan:ignore
FAKE_TS2 = "100.88.1.2"  # leakscan:ignore


def test_requires_token(client):
    c, _ = client
    assert c.get("/api/status").status_code == 401
    assert c.get("/").status_code == 401
    assert c.get("/api/status", params={"token": "wrong"}).status_code == 401


def test_token_moves_to_cookie_and_status_works(client):
    c, s = client
    r = c.get("/", params={"token": "tok-123"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"  # URL からトークンを消す
    assert COOKIE in r.cookies and "httponly" in r.headers["set-cookie"].lower()
    c.cookies.set(COOKIE, r.cookies[COOKIE])
    page = c.get("/")
    assert page.status_code == 200 and "<title>Dartmouth</title>" in page.text
    st = c.get("/api/status").json()
    assert st["meeting"]["id"] == s.dir.name and st["phase"] in ("intro", "topic") and "calls_left" in st
    st = c.get("/api/state").json()
    assert st["mode"] == "meeting" and st["meeting"]["name"] == "test"


def test_meeting_name_is_not_inlined_into_html(tmp_path, new_session):
    # 会議名は JSON で渡して画面側が textContent で入れる（HTML に埋め込まない）
    s = new_session(make_config(tmp_path, topics=["議題A"], name="<script>alert(1)</script>"))
    c = TestClient(create_app("t", session=s))
    page = c.get("/", params={"token": "t"})
    assert "<script>alert(1)</script>" not in page.text
    assert c.get("/api/state", params={"token": "t"}).json()["meeting"]["name"] == "<script>alert(1)</script>"


def test_never_binds_all_interfaces(monkeypatch):
    monkeypatch.setattr(web, "tailscale_ipv4", lambda: None)
    assert bind_hosts() == ["127.0.0.1"]
    for bad in ("0.0.0.0", "::"):
        with pytest.raises(ValueError):
            bind_hosts([bad])
    monkeypatch.setattr(web, "tailscale_ipv4", lambda: FAKE_TS)
    assert bind_hosts() == ["127.0.0.1", FAKE_TS]


def test_tailscale_ipv4_parsing(monkeypatch):
    monkeypatch.setattr(web, "find_tailscale", lambda: ("/usr/bin/tailscale", "native"))
    class R:
        def __init__(self, out):
            self.stdout = out

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R(FAKE_TS2 + "\n"))
    assert tailscale_ipv4() == FAKE_TS2
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R("192.168.1.5\n"))
    assert tailscale_ipv4() is None  # Tailscale の範囲外は使わない

    def missing(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    assert tailscale_ipv4() is None


async def test_run_web_serves_and_stops_cleanly(tmp_path, new_session, monkeypatch, capsys):
    import httpx

    monkeypatch.setattr(web, "tailscale_ipv4", lambda: None)
    monkeypatch.setattr(web, "find_tailscale", lambda: None)  # テストで本物の Tailscale を触らない
    monkeypatch.setattr(web.secrets, "token_urlsafe", lambda n: "fixed-token")
    s = new_session(make_config(tmp_path, topics=["議題A"], intro=False))
    task = asyncio.create_task(web.run_web(s, port=18765))
    async with httpx.AsyncClient() as http:
        for _ in range(50):
            try:
                r = await http.get("http://127.0.0.1:18765/api/status", params={"token": "fixed-token"})
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.1)
        assert r.status_code == 200
        # 承認待ちまで進むのを待ってから Ctrl+C 相当で止める
        for _ in range(100):
            if (await http.get("http://127.0.0.1:18765/api/status", params={"token": "fixed-token"})).json()["waiting"] == "approval":
                break
            await asyncio.sleep(0.1)
    import os
    import signal as sig

    os.kill(os.getpid(), sig.SIGINT)
    assert await asyncio.wait_for(task, 10) == 0
    err = capsys.readouterr().err
    assert "http://127.0.0.1:18765/?token=fixed-token" in err
    recs = (s.dir / "transcript.jsonl").read_text()
    assert "interrupted" in recs  # resume できる状態で止まる


def test_windows_tailscale_is_not_bound_directly(monkeypatch):
    monkeypatch.setattr(web, "find_tailscale", lambda: (web.WINDOWS_TAILSCALE, "windows"))
    assert tailscale_ipv4() is None  # WSL からは Windows の Tailscale アドレスで待ち受けられない


def test_windows_serve_relay_start_and_stop(monkeypatch):
    calls = []

    class R:
        def __init__(self, out="", rc=0):
            self.stdout, self.stderr, self.returncode = out, "", rc

    def fake(args, exe, timeout=15):
        calls.append(args)
        if args[:2] == ["status", "--json"]:
            return R(json.dumps({"Self": {"DNSName": "myhost.example.ts.net."}}))
        if args[:3] == ["serve", "status", "--json"]:
            return R("{}")
        return R()

    monkeypatch.setattr(web, "_ts", fake)
    relay = web.WindowsServeRelay("tailscale.exe", 8765)
    assert relay.start() == "http://myhost.example.ts.net:8765"
    assert ["serve", "--bg", "--yes", "--http", "8765", "http://127.0.0.1:8765"] in calls
    relay.stop()
    assert calls[-1] == ["serve", "--http", "8765", "off"]
    relay.stop()  # 2回目は何もしない
    assert calls.count(["serve", "--http", "8765", "off"]) == 1


def test_windows_serve_relay_does_not_clobber_existing(monkeypatch):
    class R:
        def __init__(self, out=""):
            self.stdout, self.stderr, self.returncode = out, "", 0

    calls = []

    def fake(args, exe, timeout=15):
        calls.append(args)
        if args[:2] == ["status", "--json"]:
            return R(json.dumps({"Self": {"DNSName": "x.ts.net."}}))
        if args[:3] == ["serve", "status", "--json"]:
            return R(json.dumps({"TCP": {"8765": {"HTTP": True}}}))
        return R()

    monkeypatch.setattr(web, "_ts", fake)
    relay = web.WindowsServeRelay("tailscale.exe", 8765)
    assert relay.start() is None and not relay.active
    relay.stop()
    assert not any(a[:2] == ["serve", "--bg"] or a[-1] == "off" for a in calls)


# ---------------------------------------------------------------- 画面から会議を始める

import time

import yaml

from dartmouth.ui.web import Hub, _event_stream, record_to_client, render_md

from .conftest import ROOT


def _write_base_config(tmp_path):
    cfg = {
        "meeting": {"name": "base", "purpose": "元の目的", "intro": False},
        "meetings_dir": str(tmp_path / "meetings"),
        "prompts_dir": str(ROOT / "prompts"),
        "participants": {
            "claude": {"adapter": "dummy", "role": "chair"},
            "gemini": {"adapter": "dummy", "role": "researcher"},
        },
    }
    p = tmp_path / "base.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True))
    return p


def _wait(c, pred, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = c.get("/api/state").json()
        if pred(st):
            return st
        time.sleep(0.05)
    raise AssertionError(f"timeout: {st}")


def test_start_meeting_from_topics_and_finish_in_browser(tmp_path):
    hub = Hub([_write_base_config(tmp_path)])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        st = c.get("/api/state").json()
        assert st["mode"] == "home" and st["configs"][0]["name"] == "base.yaml"
        r = c.post("/api/start", json={"config": 0, "name": "画面から", "topics": [
            {"title": "ストレージをどうするか", "premise": "予算は1万円"}]})
        assert r.status_code == 200, r.text
        assert c.post("/api/start", json={"config": 0, "topics": [{"title": "x"}]}).status_code == 409  # 1つずつ
        _wait(c, lambda s: s.get("waiting") == "approval")
        prop = c.get("/api/proposal").json()
        assert prop["topic"] == 1 and prop["title"] == "ストレージをどうするか" and prop["proposal"]["decisions"]
        # 割り込み → 承認
        assert c.post("/api/command", json={"line": "/approve"}).json()["message"] == "承認しました"
        st = _wait(c, lambda s: not s["meeting"]["running"])
        assert st["phase"] == "done"
        mid = st["meeting"]["id"]
        assert "画面から" in mid or mid.endswith("meeting") or mid
        assert "[D-001]" in c.get("/api/minutes").json()["html"]
        assert c.post("/api/close").status_code == 200
        assert c.get("/api/state").json()["mode"] == "home"
        ms = c.get("/api/meetings").json()["meetings"]
        assert ms[0]["state"] == "終了" and ms[0]["open_count"] == 1
        # 前提条件がエージェントに渡っている
        transcript = (tmp_path / "meetings" / mid / "config.yaml").read_text()
        assert "予算は1万円" in transcript


def test_start_from_previous_meeting_open_questions(tmp_path):
    hub = Hub([_write_base_config(tmp_path)])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        c.post("/api/start", json={"config": 0, "name": "first", "topics": [{"title": "議題A"}]})
        _wait(c, lambda s: s.get("waiting") == "approval")
        c.post("/api/command", json={"line": "/approve"})
        first = _wait(c, lambda s: not s["meeting"]["running"])["meeting"]["id"]
        c.post("/api/close")
        detail = c.get(f"/api/meetings/{first}").json()
        q = detail["open_questions"][0]
        assert q["id"] == "Q-001"
        r = c.post("/api/start", json={"config": 0, "name": "second", "from_meeting": first, "include_minutes": True,
                                       "topics": [{"title": q["text"], "premise": "前回の Q-001 から"}]})
        assert r.status_code == 200
        _wait(c, lambda s: s.get("waiting") == "approval")
        second = c.get("/api/state").json()["meeting"]["id"]
        saved = yaml.safe_load((tmp_path / "meetings" / second / "config.yaml").read_text())
        ctx = saved["meeting"]["context"][-1]
        assert ctx["label"].startswith("前回の会議") and "[D-001]" in ctx["text"] and "[Q-001]" in ctx["text"]
        assert saved["meeting"]["topics"][0]["title"] == q["text"]
        c.post("/api/close")


def test_resume_from_browser_and_reject_unknown_ids(tmp_path):
    hub = Hub([_write_base_config(tmp_path)])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        c.post("/api/start", json={"config": 0, "topics": [{"title": "議題A"}]})
        _wait(c, lambda s: s.get("waiting") == "approval")
        mid = c.get("/api/state").json()["meeting"]["id"]
        c.post("/api/close")  # 承認待ちのまま閉じる＝中断
        ms = {m["id"]: m for m in c.get("/api/meetings").json()["meetings"]}
        assert ms[mid]["resumable"] and ms[mid]["state"] == "承認待ちで中断"
        assert c.post(f"/api/resume/{mid}").status_code == 200
        _wait(c, lambda s: s.get("waiting") == "approval")
        c.post("/api/command", json={"line": "/approve"})
        _wait(c, lambda s: not s["meeting"]["running"])
        c.post("/api/close")
        # 一覧にない名前・パスは開かせない
        for bad in ("../../etc", "nope", "..%2F..%2Fetc"):
            assert c.post(f"/api/resume/{bad}").status_code in (404, 405)
            assert c.get(f"/api/meetings/{bad}").status_code in (404, 405)


def test_extend_finished_meeting_from_browser(tmp_path):
    hub = Hub([_write_base_config(tmp_path)])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        c.post("/api/start", json={"config": 0, "topics": [{"title": "議題A"}]})
        _wait(c, lambda s: s.get("waiting") == "approval")
        c.post("/api/command", json={"line": "/approve"})
        mid = _wait(c, lambda s: not s["meeting"]["running"])["meeting"]["id"]
        # 終わったばかりで画面に開いたまま（閉じずに）延長できる
        assert c.post(f"/api/extend/{mid}", json={"topic": 5}).status_code == 400
        r = c.post(f"/api/extend/{mid}", json={"topic": 1, "note": "費用を詰めたい"})
        assert r.status_code == 200 and r.json()["topic"] == 2, r.text
        assert c.post(f"/api/extend/{mid}", json={"topic": 1}).status_code == 409  # 進行中は不可
        _wait(c, lambda s: s.get("waiting") == "approval")
        assert c.get("/api/proposal").json()["title"] == "【延長】議題A"
        c.post("/api/command", json={"line": "/approve"})
        _wait(c, lambda s: not s["meeting"]["running"])
        c.post("/api/close")
        m = c.get(f"/api/meetings/{mid}").json()
        assert m["extendable"] and m["handovers"]["1"]["status"] == "延長（議題2）で議論済み"
        assert [t["no"] for t in m["topics"]] == [1, 2]


def test_edit_config_topics_from_browser_and_done_topics_listed(tmp_path):
    base = _write_base_config(tmp_path)
    text = base.read_text()
    base.write_text("# 手で書いたコメント\n" + text.replace("meeting:\n", "meeting:\n  topics:\n  - title: 議題A\n    group: g1\n"
                                                               "  done_topics:\n  - title: 古い議題\n    premise: 前提P\n"
                                                               "    meeting: 2026-11-01-x\n    done_at: '2026-11-01'\n", 1))
    hub = Hub([base])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        cfg = c.get("/api/state").json()["configs"][0]
        assert [t["title"] for t in cfg["topics"]] == ["議題A"]
        assert cfg["done_topics"] == [{"title": "古い議題", "premise": "前提P", "meeting": "2026-11-01-x", "done_at": "2026-11-01"}]
        post = lambda **kw: c.post("/api/config/topics", json={"config_path": str(base), **kw})
        assert post(action="add", title="議題B", premise="前提B", group="g2").status_code == 200
        assert post(action="add", title="議題B").status_code == 400  # 同じタイトルは足せない
        assert post(action="update", title="議題A", new_title="議題A2", premise="").status_code == 200
        assert post(action="remove", title="ない議題").status_code == 404
        # 再読み込みしなくても、次の状態取得で新しい議題が見える
        topics = c.get("/api/state").json()["configs"][0]["topics"]
        assert [(t["title"], t["premise"], t.get("group")) for t in topics] == [("議題A2", "", "g1"), ("議題B", "前提B", "g2")]
        assert post(action="remove", title="議題B").status_code == 200
        assert "# 手で書いたコメント" in base.read_text() and (tmp_path / "base.yaml.bak").exists()
        # 一覧にない設定ファイルは書き換えさせない
        assert c.post("/api/config/topics", json={"config_path": str(tmp_path / "other.yaml"), "action": "add", "title": "x"}).status_code == 400


def test_edit_draft_and_approve_via_api(tmp_path):
    hub = Hub([_write_base_config(tmp_path)])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        c.post("/api/start", json={"config": 0, "topics": [{"title": "議題A"}]})
        _wait(c, lambda s: s.get("waiting") == "approval")
        text = c.get("/api/draft").json()["text"]
        edited = text.replace("議題1の決定事項案", "ブラウザで直した決定事項")
        assert c.put("/api/draft", json={"text": edited}).status_code == 200
        assert "ブラウザで直した決定事項" in c.get("/api/draft").json()["text"]  # 書きかけが残る
        assert c.post("/api/approve_edit", json={"text": "decisions: [壊れた"}).status_code == 422
        assert c.post("/api/approve_edit", json={"text": edited}).status_code == 200
        _wait(c, lambda s: not s["meeting"]["running"])
        assert "ブラウザで直した決定事項" in c.get("/api/minutes").json()["html"]
        c.post("/api/close")


def test_start_requires_auth_and_valid_config(tmp_path):
    hub = Hub([_write_base_config(tmp_path)])
    with TestClient(create_app("t", hub=hub)) as c:
        assert c.post("/api/start", json={"config": 0, "topics": [{"title": "x"}]}).status_code == 401
        c.cookies.set(COOKIE, "t")
        assert c.post("/api/start", json={"config": 5, "topics": [{"title": "x"}]}).status_code == 400
        assert c.post("/api/start", json={"config": 0, "topics": []}).status_code == 422


def test_markdown_is_sanitized():
    html = render_md("**太字** <script>alert(1)</script> [x](javascript:alert(1)) <img src=x onerror=alert(1)>")
    assert "<strong>太字</strong>" in html
    assert "<script>" not in html and "<img" not in html and 'href="javascript' not in html


def test_record_to_client_hides_checkpoints_and_chair_json():
    names = {"claude": "Claude", "user": "ユーザー"}
    assert record_to_client({"event": "checkpoint", "kind": "system", "text": "x"}, names) is None
    m = record_to_client({"seq": 3, "topic": 1, "round": "summary", "speaker": "claude", "kind": "message",
                          "text": '{"decisions": ["SSDは2TB"], "summary": "要約です"}'}, names)
    assert m["kind"] == "summary" and "SSDは2TB" in m["html"] and "決定事項案" in m["html"] and '"decisions"' not in m["html"]
    # ラウンドの区切り
    d = record_to_client({"seq": 5, "topic": 1, "event": "checkpoint", "kind": "system", "state": {"phase": "critique", "critique_done": 1}}, names)
    assert d["kind"] == "divider" and d["html"] == "相互批判 2回目"
    assert record_to_client({"seq": 6, "topic": None, "event": "checkpoint", "kind": "system", "state": {"phase": "topic"}}, names) is None
    m = record_to_client({"seq": 4, "topic": 1, "round": "critique", "speaker": "claude", "kind": "message", "text": "**弱点**", "elapsed_ms": 4200}, names)
    assert m["name"] == "Claude" and "<strong>弱点</strong>" in m["html"] and m["elapsed_s"] == 4


async def test_event_stream_replays_then_streams_without_duplicates(tmp_path, new_session):
    s = new_session(make_config(tmp_path, topics=["議題A"], intro=False))
    s.engine.interject("最初の割り込み")  # 既存分として transcript にある
    hub = Hub()
    hub.session = s

    class Req:
        async def is_disconnected(self):
            return False

    gen = _event_stream(Req(), hub, s)
    got = []
    async for chunk in gen:
        text = chunk.decode()
        got.append(text)
        if text.startswith("event: ready"):
            break
    assert any("最初の割り込み" in g for g in got)
    s.engine.interject("あとの割り込み")
    nxt = (await gen.__anext__()).decode()
    assert "あとの割り込み" in nxt and "最初の割り込み" not in nxt
    await gen.aclose()
    assert not s.engine.bus._queues  # 購読を解除している


# ---------------------------------------------------------------- 管理画面

def _finished_meeting(c, name="first"):
    c.post("/api/start", json={"config": 0, "name": name, "topics": [{"title": "議題A"}]})
    _wait(c, lambda s: s.get("waiting") == "approval")
    c.post("/api/command", json={"line": "/approve"})
    mid = _wait(c, lambda s: not s["meeting"]["running"])["meeting"]["id"]
    c.post("/api/close")
    return mid


def test_archive_view_search_and_download(tmp_path):
    hub = Hub([_write_base_config(tmp_path)])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        mid = _finished_meeting(c)
        v = c.get(f"/api/archive/{mid}").json()
        assert v["summary"]["state"] == "終了" and v["summary"]["topics"][0]["title"] == "議題A"
        assert any(m["kind"] == "message" for m in v["messages"]) and "[D-001]" in v["minutes_html"]
        assert v["draft_html"] and v["usage"]["totals"]["calls"] > 0 and "claude" in v["participants"]
        hits = c.get("/api/search", params={"q": "independentでの発言"}).json()["hits"]
        assert hits and hits[0]["meeting"] == mid and hits[0]["seq"]
        assert c.get("/api/search", params={"q": "x"}).json()["hits"] == []
        r = c.get(f"/api/archive/{mid}/download/minutes")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and "[D-001]" in r.text
        assert c.get(f"/api/archive/{mid}/download/transcript").status_code == 200
        assert c.get(f"/api/archive/{mid}/download/secret").status_code == 404
        assert c.get("/api/archive/..%2Fx").status_code == 404


def test_items_tracker_marks_done_and_persists(tmp_path):
    hub = Hub([_write_base_config(tmp_path)])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        mid = _finished_meeting(c)
        items = c.get("/api/items").json()["items"]
        kinds = {i["kind"] for i in items}
        assert kinds == {"decision", "question", "todo"}
        todo = next(i for i in items if i["kind"] == "todo")
        assert not todo["done"] and todo["meeting"] == mid
        r = c.post("/api/items/status", json={"meeting": mid, "id": todo["id"], "done": True, "note": "済んだ"})
        assert r.status_code == 200
        again = next(i for i in c.get("/api/items").json()["items"] if i["id"] == todo["id"] and i["kind"] == "todo")
        assert again["done"] and again["note"] == "済んだ"
        assert (tmp_path / "meetings" / mid / "item_status.json").exists()
        # transcript は書き換えない
        assert "済んだ" not in (tmp_path / "meetings" / mid / "transcript.jsonl").read_text()
        assert c.post("/api/items/status", json={"meeting": mid, "id": "T-999", "done": True}).status_code == 404
        assert c.post("/api/items/status", json={"meeting": mid, "id": "bad", "done": True}).status_code == 422


def test_start_from_multiple_meetings(tmp_path):
    hub = Hub([_write_base_config(tmp_path)])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        a = _finished_meeting(c, "aaa")
        b = _finished_meeting(c, "bbb")
        c.post("/api/start", json={"config": 0, "from_meetings": [a, b], "topics": [{"title": "まとめて"}]})
        _wait(c, lambda s: s.get("waiting") == "approval")
        mid = c.get("/api/state").json()["meeting"]["id"]
        saved = yaml.safe_load((tmp_path / "meetings" / mid / "config.yaml").read_text())
        labels = [x["label"] for x in saved["meeting"]["context"]]
        assert any(a in l for l in labels) and any(b in l for l in labels)
        c.post("/api/close")


def test_usage_aggregates_by_participant(tmp_path):
    hub = Hub([_write_base_config(tmp_path)])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        _finished_meeting(c, "aaa")
        _finished_meeting(c, "bbb")
        u = c.get("/api/usage").json()
        assert u["totals"]["meetings"] == 2 and u["totals"]["calls"] > 0
        claude = next(p for p in u["participants"] if p["display_name"] == "Claude")
        assert claude["meetings"] == 2 and claude["avg_s"] >= 0 and 0 <= claude["fail_rate"] <= 1


def test_settings_edit_validates_and_blocks_code_execution(tmp_path):
    base = _write_base_config(tmp_path)
    hub = Hub([base])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        files = c.get("/api/settings/files").json()["files"]
        paths = {f["path"]: f for f in files}
        assert str(base) in paths and any(f["group"] == "ペルソナ" for f in files)
        orig = c.get("/api/settings/file", params={"path": str(base)}).json()["text"]
        # 正しい変更は保存され、前の版が .bak に残る
        new = orig.replace("元の目的", "新しい目的")
        r = c.put("/api/settings/file", json={"path": str(base), "text": new})
        assert r.status_code == 200 and "新しい目的" in base.read_text() and (tmp_path / "base.yaml.bak").exists()
        # 設定として壊れていれば拒否（ファイルは変わらない）
        assert c.put("/api/settings/file", json={"path": str(base), "text": "participants: {}\n"}).status_code == 422
        assert "新しい目的" in base.read_text()
        # 任意のプログラムを動かせる変更は拒否
        evil = new.replace("adapter: dummy\n    role: researcher", "adapter: /tmp/evil.py:Evil\n    role: researcher")
        assert evil != new
        r = c.put("/api/settings/file", json={"path": str(base), "text": evil})
        assert r.status_code == 422 and "アダプタ" in r.json()["detail"]
        cmd = new.replace("adapter: dummy\n    role: researcher", "adapter: dummy\n    command: /bin/sh\n    role: researcher")
        r = c.put("/api/settings/file", json={"path": str(base), "text": cmd})
        assert r.status_code == 422 and "command" in r.json()["detail"]
        # 一覧にないファイルは読めない・書けない
        assert c.get("/api/settings/file", params={"path": "/etc/passwd"}).status_code == 404
        assert c.put("/api/settings/file", json={"path": str(tmp_path / "other.yaml"), "text": "x"}).status_code == 404
        # プロンプトも編集できる
        persona = next(f for f in files if f["group"] == "ペルソナ")
        assert c.get("/api/settings/file", params={"path": persona["path"]}).status_code == 200


def test_static_assets_require_auth_hashed_cached_and_gzipped(tmp_path):
    import re

    hub = Hub([_write_base_config(tmp_path)])
    with TestClient(create_app("t", hub=hub)) as c:
        assert c.get("/static/js/core.js").status_code == 401
        c.cookies.set(COOKIE, "t")
        html = c.get("/").text
        url = re.search(r'src="(/static/js/core\.js\?v=[0-9a-f]+)"', html).group(1)
        assert "window.__assets=" in html and "ledger.js?v=" in html  # あとから読む画面の JS
        r = c.get(url, headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200 and "javascript" in r.headers["content-type"]
        assert r.headers["content-encoding"] == "gzip" and "immutable" in r.headers["cache-control"]
        assert c.get(url, headers={"If-None-Match": r.headers["etag"]}).status_code == 304
        assert c.get("/static/../web.py").status_code == 404 and c.get("/static/index.html").status_code == 404


def test_config_topics_shown_and_used_when_form_is_empty(tmp_path):
    base = _write_base_config(tmp_path)
    data = yaml.safe_load(base.read_text())
    data["meeting"]["topics"] = [{"title": "設定の議題1", "premise": "設定の前提"}, "設定の議題2"]
    base.write_text(yaml.safe_dump(data, allow_unicode=True))
    hub = Hub([base])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        cfgs = c.get("/api/state").json()["configs"]
        assert cfgs[0]["topics"] == [{"title": "設定の議題1", "premise": "設定の前提"}, {"title": "設定の議題2", "premise": ""}]
        assert cfgs[0]["meeting_name"] == "base"
        assert c.post("/api/start", json={"config": 0, "topics": []}).status_code == 200
        _wait(c, lambda s: s.get("waiting") == "approval")
        st = c.get("/api/state").json()
        assert st["topic_title"] == "設定の議題1" and st["topics_total"] == 2
        c.post("/api/close")


def test_configs_discovered_from_dirs_and_selected_by_path(tmp_path):
    base = _write_base_config(tmp_path)                    # 会議の設定
    (tmp_path / "notes.yaml").write_text("title: 会議の設定ではない\n")   # 参加者がないので出さない
    (tmp_path / ".hidden.yaml").write_text(base.read_text())             # 隠しファイルは出さない
    sub = tmp_path / "sub"; sub.mkdir()
    (sub / "deep.yaml").write_text(base.read_text())                     # サブフォルダは見ない
    hub = Hub(config_dirs=[tmp_path])
    assert hub.configs == [base]
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        cfgs = c.get("/api/state").json()["configs"]
        assert [x["name"] for x in cfgs] == ["base.yaml"] and cfgs[0]["participants"][0]["id"] == "claude"
        # 一覧にないパスは選べない
        for bad in (str(sub / "deep.yaml"), str(tmp_path / "notes.yaml"), "/etc/passwd"):
            assert c.post("/api/start", json={"config_path": bad, "topics": [{"title": "x"}]}).status_code == 400
        r = c.post("/api/start", json={"config_path": str(base), "topics": [{"title": "パスで選んだ"}]})
        assert r.status_code == 200
        _wait(c, lambda s: s.get("waiting") == "approval")
        assert c.get("/api/state").json()["topic_title"] == "パスで選んだ"
        c.post("/api/close")
        # 新しく置いた設定ファイルも、再起動なしで一覧に出る
        (tmp_path / "second.yaml").write_text(base.read_text())
        assert [x["name"] for x in c.get("/api/state").json()["configs"]] == ["base.yaml", "second.yaml"]
        # 議題専用 YAML（participants なし）も一覧に出て、別ディレクトリの agents.yaml と組み合わせて開始できる
        (tmp_path / "topic_only.yaml").write_text("meeting:\n  name: only\n  topics: [議題Z]\n")
        names = [x["name"] for x in c.get("/api/state").json()["configs"]]
        assert "topic_only.yaml" in names
        r2 = c.post("/api/start", json={"config_path": str(tmp_path / "topic_only.yaml"), "agents_config_path": str(base)})
        assert r2.status_code == 200
        c.post("/api/close")


        # 進行中以外は削除できる
        mid = r2.json()["id"]
        r_del = c.delete(f"/api/archive/{mid}")
        assert r_del.status_code == 200 and not (tmp_path / "meetings" / mid).exists()


def test_archive_meeting_hides_from_lists_but_keeps_data(tmp_path):
    hub = Hub([_write_base_config(tmp_path)])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        mid = _finished_meeting(c)
        assert c.post(f"/api/archive/{mid}/archived", json={"archived": True, "reason": "やり直したので"}).status_code == 200
        m = next(x for x in c.get("/api/meetings").json()["meetings"] if x["id"] == mid)
        assert m["archived"] and "やり直したので" in m["archived"]
        assert all(i["archived"] for i in c.get("/api/items").json()["items"] if i["meeting"] == mid)
        assert c.get("/api/usage").json()["totals"]["meetings"] == 1  # 集計には残る
        assert c.get(f"/api/archive/{mid}").status_code == 200  # 読める
        c.post(f"/api/archive/{mid}/archived", json={"archived": False})
        assert not next(x for x in c.get("/api/meetings").json()["meetings"] if x["id"] == mid)["archived"]


async def test_web_shows_meeting_running_in_another_process(tmp_path):
    from dartmouth.session import create_session
    from dartmouth.ui.web import Hub

    cfg = make_config(tmp_path, topics=["議題A"], intro=False, approval="auto")
    s = create_session(cfg, headless=True)  # 「CLI 側」のセッション（ロックを持つ）
    await s.engine.run()
    assert (s.dir / "status.json").exists()
    hub = Hub(extra_dirs=[s.dir.parent])
    hub.explicit = []
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        st = c.get("/api/state").json()
        assert st["mode"] == "meeting" and st["readonly"] and st["meeting"]["id"] == s.dir.name and st["meeting"]["running"]
        assert c.get("/api/minutes").json()["html"]
        assert next(m for m in c.get("/api/meetings").json()["meetings"] if m["id"] == s.dir.name)["running"]
        await s.close()  # CLI 側が終わる → ロックが外れる
        body = c.get("/api/events").text
        assert "event: msg" in body
        assert "event: end" in body and "議題A" in body
        st = c.get("/api/state").json()
        assert st["readonly"] and not st["meeting"]["running"]  # 閉じるまで表示したまま
        c.post("/api/close")
        assert c.get("/api/state").json()["mode"] == "home"
