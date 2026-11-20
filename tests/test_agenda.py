import yaml
from fastapi.testclient import TestClient

from dartmouth.agenda import due_key, load
from dartmouth.config import load_config
from dartmouth.ledger import Ledger, sync_agenda
from dartmouth.topics_file import edit_topic, mark_topics_done

from .conftest import ROOT

AGENDA = """# 議題の一覧
共通設定:
  critique_rounds: 1
  intro: false
テーマ:
  - 名前: 暮らし
    マイルストーン:
      - キー: M1
        名前: 現状把握
        達成条件: 数字が揃う
        期限: 2020-03
      - キー: M2
        名前: 方針決定
        期限: 2030年
    案件:
      - id: life
        名前: 旅行計画
        目的: 旅行の行き先を決める
        マイルストーン期限:
          M2: 2028
        議題:
          - 題名: 予算配分
            前提: 月3万円まで
            グループ: お金
          - 住まい
        議論済み: []
  - 名前: 道具
    案件:
      - id: tool
        名前: 道具づくり
        議題: []
"""


def _agenda(tmp_path):
    p = tmp_path / "議題.yaml"
    p.write_text(AGENDA + f"meetings_dir: {tmp_path / 'meetings'}\nprompts_dir: {ROOT / 'prompts'}\n")
    (tmp_path / "agents.yaml").write_text(yaml.safe_dump({"participants": {
        "claude": {"adapter": "dummy", "role": "chair"}, "gemini": {"adapter": "dummy", "role": "researcher"}}}))
    return p


def test_due_key_parses_japanese_and_iso_forms():
    assert due_key("2026") == "2026-12-31" and due_key("2027年") == "2027-12-31"
    assert due_key("2026-02") == "2026-02-28" and due_key("2027年3月") == "2027-03-31"
    assert due_key("2026-12-05") == "2026-12-05" and due_key("2026Q2") == "2026-06-30"
    assert due_key("そのうち") is None and due_key("") is None and due_key("2026-13") is None


def test_agenda_loads_as_config_and_topics_move_to_done(tmp_path):
    p = _agenda(tmp_path)
    ag = load(p)
    assert [th["name"] for th in ag["themes"]] == ["暮らし", "道具"]
    cfg = load_config(f"{p}#life", agents_path=tmp_path / "agents.yaml")
    assert cfg.meeting.name == "旅行計画" and cfg.meeting.slug == "life" and cfg.meeting.project == "life"
    assert cfg.meeting.theme == "暮らし" and cfg.meeting.critique_rounds == 1 and cfg.meeting.purpose == "旅行の行き先を決める"
    assert [(t.title, t.premise, t.group) for t in cfg.meeting.topics] == [("予算配分", "月3万円まで", "お金"), ("住まい", "", None)]
    # 議題の追加・承認後の移動は、日本語のキーのまま書き戻す（コメントも残る）
    edit_topic(f"{p}#tool", "add", "新しい道具", premise="前提", group="g")
    assert mark_topics_done(f"{p}#life", ["予算配分"], "2026-11-20-life") == ["予算配分"]
    text = p.read_text()
    assert "# 議題の一覧" in text and "題名: 新しい道具" in text and "会議: 2026-11-20-life" in text
    life = load(p)["themes"][0]["projects"][0]
    assert [t if isinstance(t, str) else t["title"] for t in life["topics"]] == ["住まい"]
    assert life["done_topics"][0]["title"] == "予算配分" and life["done_topics"][0]["premise"] == "月3万円まで"


def test_ledger_syncs_agenda_milestones_with_due_and_timeline(tmp_path):
    p = _agenda(tmp_path)
    lg = Ledger(tmp_path / "l.sqlite")
    assert sync_agenda(lg, p) == {"projects": 2, "milestones": 2}
    assert {r["id"]: r["theme"] for r in lg.projects()} == {"life": "暮らし", "tool": "道具"}
    mx = lg.matrix("暮らし")
    assert [(m["key"], m["due"]) for m in mx["milestones"]] == [("M1", "2020-03"), ("M2", "2030年")]
    cells = mx["rows"][0]["cells"]
    assert cells[1]["due"] == "2028"  # 案件ごとの期限が、テーマ共通の期限より優先
    tl = lg.timeline()
    assert [(r["key"], r["due_key"]) for r in tl] == [("M1", "2020-03-31"), ("M2", "2028-12-31")]
    # 議題ファイルからマイルストーンを消すと、台帳からも消える
    p.write_text(p.read_text().replace("      - キー: M2\n        名前: 方針決定\n        期限: 2030年\n", ""))
    sync_agenda(lg, p)
    assert [m["key"] for m in lg.milestones("暮らし")] == ["M1"]


def test_web_lists_agenda_projects_and_writes_milestones_back(tmp_path):
    from dartmouth.ui.web import COOKIE, Hub, create_app

    from .test_web import _wait

    p = _agenda(tmp_path)
    hub = Hub([], [tmp_path / "meetings"], [tmp_path])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        cfgs = [x for x in c.get("/api/state").json()["configs"] if x.get("theme")]
        assert [(x["theme"], x["label"], x["path"].endswith("#life") or x["path"].endswith("#tool")) for x in cfgs] == \
            [("暮らし", "旅行計画", True), ("道具", "道具づくり", True)]
        life = cfgs[0]
        assert [t["title"] for t in life["topics"]] == ["予算配分", "住まい"] and life["topics"][0]["group"] == "お金"
        # 案件を選んで会議を始め、承認すると議題ファイルの「議論済み」へ移る
        r = c.post("/api/start", json={"config_path": life["path"], "topics": [{"title": "住まい"}]})
        assert r.status_code == 200, r.text
        _wait(c, lambda s: s.get("waiting") == "approval")
        c.post("/api/command", json={"line": "/approve"})
        _wait(c, lambda s: not s["meeting"]["running"])
        c.post("/api/close")
        assert "題名: 住まい" in p.read_text().split("議論済み")[1]
        # 台帳: 期限つきのマイルストーンを画面から足すと、議題ファイルに書かれる
        assert c.get("/api/ledger").status_code == 200
        assert c.post("/api/ledger/milestone", json={"theme": "暮らし", "key": "M3", "title": "実行", "due": "2027-04"}).status_code == 200
        assert "キー: M3" in p.read_text() and "期限: 2027-04" in p.read_text()
        assert c.post("/api/ledger/milestone", json={"theme": "暮らし", "key": "M4", "title": "x", "due": "いつか"}).status_code == 400
        mx = c.get("/api/ledger/matrix/暮らし").json()
        m3 = next(m for m in mx["milestones"] if m["key"] == "M3")
        assert c.post("/api/ledger/due", json={"project": "life", "milestone": m3["id"], "due": "2027-06"}).status_code == 200
        assert "M3: 2027-06" in p.read_text()
        assert c.post(f"/api/ledger/milestone/{m3['id']}", json={"fields": {"due": "2027-05"}}).status_code == 200
        assert "期限: 2027-05" in p.read_text()
        tl = c.get("/api/ledger/timeline?theme=暮らし").json()["rows"]
        assert any(r["key"] == "M1" and r["overdue"] for r in tl)  # 2020-03 は過ぎていて未達


def test_chair_bundles_open_items_into_topics_and_caches(tmp_path, monkeypatch):
    import json as _json

    from dartmouth.adapters.base import Reply
    from dartmouth.ledger import classify as cl
    from dartmouth.ui.web import COOKIE, Hub, create_app

    from .test_web import _wait

    p = _agenda(tmp_path)
    calls = []

    class FakeChair:
        name = "chair"
        options = {"model": "fake"}

        async def respond(self, ctx):
            calls.append(ctx.prompt)
            ids = [line.split(" ／ ")[0][2:] for line in ctx.prompt.splitlines() if line.startswith("- ") and " ／ " in line]
            return Reply(_json.dumps({"topics": [{"title": "まとめた議題", "goal": "方針を決める", "why": "関係が深い",
                                                  "item_ids": ids + ["unknown:Q-999"]}],
                                      "skipped": []}, ensure_ascii=False))

        async def close(self):
            pass

    monkeypatch.setattr(cl, "chair_adapter", lambda *a, **k: FakeChair())
    hub = Hub([], [tmp_path / "meetings"], [tmp_path])
    with TestClient(create_app("t", hub=hub)) as c:
        c.cookies.set(COOKIE, "t")
        life = next(x for x in c.get("/api/state").json()["configs"] if x.get("project") == "life")
        # 会議を1回して、未解決事項（Q-001）を台帳に作る
        c.post("/api/start", json={"config_path": life["path"], "topics": [{"title": "住まい"}]})
        _wait(c, lambda s: s.get("waiting") == "approval")
        c.post("/api/command", json={"line": "/approve"})
        _wait(c, lambda s: not s["meeting"]["running"])
        c.post("/api/close")
        r = c.get("/api/agenda/proposal?project=life").json()
        assert r["proposal"] is None and r["open_items"] == 1
        prop = c.post("/api/agenda/propose", json={"config_path": life["path"]}).json()["proposal"]
        assert [t["title"] for t in prop["topics"]] == ["まとめた議題"] and len(calls) == 1
        t = prop["topics"][0]
        assert len(t["item_ids"]) == 1 and "unknown:Q-999" not in t["item_ids"]  # 一覧にない uid は捨てる
        assert "この議題で決めること: 方針を決める" in t["premise"] and t["item_ids"][0] in t["premise"]
        # 未解決事項が変わらなければ、前の提案を AI を呼ばずに返す
        again = c.get("/api/agenda/proposal?project=life").json()["proposal"]
        assert again["cached"] and again["topics"][0]["title"] == "まとめた議題" and len(calls) == 1
        # 未解決事項が片付くと、前の提案は使わない
        c.post(f"/api/ledger/item/{t['item_ids'][0]}/status", json={"status": "done"})
        assert c.get("/api/agenda/proposal?project=life").json()["proposal"] is None


def test_cli_run_accepts_agenda_project_ref(tmp_path, monkeypatch):
    """dm run -c 議題.yaml#案件id: ファイルの存在確認で「#案件id」付きを弾かない（agents.yaml は同じフォルダから探す）。"""
    import pytest

    from dartmouth import cli

    p = tmp_path / "agenda.yaml"
    p.write_text(AGENDA, encoding="utf-8")
    (tmp_path / "agents.yaml").write_text(yaml.safe_dump({"participants": {
        "chair": {"adapter": "dummy", "role": "chair"}, "a": {"adapter": "dummy", "role": "member"}}}), encoding="utf-8")
    seen = {}
    monkeypatch.setattr("dartmouth.session.create_session", lambda cfg, *a, **k: seen.setdefault("cfg", cfg) and (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        cli.main(["run", "-c", f"{p}#life", "--headless"])
    assert seen["cfg"].meeting.project == "life"
