import re
from datetime import datetime, timezone

from dartmouth.export_md import export_markdown
from dartmouth.ledger import Ledger, sync
from dartmouth.storage.archive import save_item_status, set_archived, summarize

from .conftest import make_config, run_auto


async def test_export_md_format_and_determinism(tmp_path, new_session):
    s = new_session(make_config(tmp_path, topics=[{"title": "議題A", "premise": "前提X"}, "議題B"]))
    await run_auto(s)
    mdir = s.dir
    sm = summarize(mdir)
    todo = sm["todos"][0]["id"]
    save_item_status(mdir, todo, True, "済ませた")
    set_archived(mdir, True, "テスト")
    db = tmp_path / "ledger.sqlite"
    lg = Ledger(db)
    sync(lg, [mdir.parent])
    lg.update_item(f"{mdir.name}:Q-001", {"rationale": "根拠R", "ball": "external"})
    lg.close()
    before = db.stat().st_mtime_ns

    now = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
    md = export_markdown([mdir.parent], db, now=now)
    n_items = sum(len(summarize(mdir)[k]) for k in ("decisions", "open_questions", "todos"))

    # 先頭サマリ
    assert md.startswith("# Dartmouth 全アイテム（エクスポート）\n")
    assert "- 生成日時: 2026-11-20T09:00:00+00:00" in md
    assert "- 会議数: 1（アーカイブ済み 1・アイテムのある会議 1）" in md
    assert f"- アイテム総数: {n_items}（" in md and "ToDo・完了 1" in md
    # 会議のセクション: ID・名前・日付、アーカイブ、件数
    assert re.search(rf"\n## {re.escape(mdir.name)}「.+」（\d{{4}}-\d{{2}}-\d{{2}}）\n", md)
    assert "- アーカイブ: 済み（" in md and f"- アイテム: {n_items} 件（開いている " in md
    # アイテム: ID 順、種別・状態・ボール・根拠・全文
    ids = re.findall(r"\n### \[([A-Z]+-\d+)\]", md)
    assert ids == sorted(ids, key=lambda x: (x.split("-")[0], int(x.split("-")[1]))) and len(ids) == n_items
    q = md[md.index("### [Q-001]"):]
    q = q[: q.index("\n### ", 1)] if "\n### " in q[1:] else q
    assert "- 種別: 未決事項（question）" in q and "- ボール: 外部待ち（手で設定）" in q and "- 根拠（台帳）: 根拠R" in q
    assert f"> {summarize(mdir)['open_questions'][0]['text'].splitlines()[0]}" in q
    t = md[md.index(f"### [{todo}]"):]
    assert "- 状態: 完了（done=true）" in t and "- メモ: 済ませた" in t
    # 決定的（生成日時が同じならバイト単位で同じ）・LF・台帳は書き換えない
    assert export_markdown([mdir.parent], db, now=now) == md
    assert "\r" not in md and db.stat().st_mtime_ns == before
    # 台帳がなくても出せる
    assert f"- アイテム総数: {n_items}（" in export_markdown([mdir.parent], tmp_path / "none.sqlite", now=now)


async def test_export_md_web_endpoint(tmp_path, new_session, monkeypatch):
    from fastapi.testclient import TestClient

    from dartmouth.ui.web import COOKIE, Hub, create_app

    s = new_session(make_config(tmp_path))
    await run_auto(s)
    monkeypatch.setenv("DARTMOUTH_LEDGER", str(tmp_path / "none.sqlite"))
    hub = Hub([], [s.dir.parent])
    with TestClient(create_app("t", hub=hub)) as c:
        assert c.get("/api/export.md").status_code == 401  # トークンが要る
        c.cookies.set(COOKIE, "t")
        r = c.get("/api/export.md")
        assert r.headers["content-type"].startswith("text/markdown") and "attachment" not in r.headers.get("content-disposition", "")
        assert r.text.startswith("# Dartmouth 全アイテム") and f"\n## {s.dir.name}「" in r.text
        d = c.get("/api/export.md?download=1")
        assert d.headers["content-disposition"].startswith('attachment; filename="dartmouth-items-')
