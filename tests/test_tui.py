from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Input, Markdown

from dartmouth.ui.tui import DartmouthApp, LogItem

from .conftest import make_config


PANES = ("#tabs", "#main", "#log", "#side", "#minutes-scroll", "#status", "#statusbar", "#input")


def _assert_fits(app, w: int, h: int) -> None:
    """レイアウトの枠（スクロールの中身ではなく）が画面内に収まり、重ならないこと。"""
    shown = []
    for sel in PANES:
        wid = app.query_one(sel)
        r = wid.region
        if not wid.display or r.area == 0:
            continue
        assert r.x >= 0 and r.y >= 0 and r.right <= w and r.bottom <= h, f"{sel} {r} が画面外"
        shown.append((sel, r))
    rows = [(s, r) for s, r in shown if s in ("#tabs", "#main", "#statusbar", "#input")]
    rows.sort(key=lambda x: x[1].y)
    for (a, ra), (b, rb) in zip(rows, rows[1:]):
        assert ra.bottom <= rb.y, f"{a} と {b} が重なっています"
    assert sum(r.height for _, r in rows) <= h


async def _wait(pilot, pred, timeout=10):
    async def loop():
        while not pred():
            await pilot.pause(0.05)

    await asyncio.wait_for(loop(), timeout)


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_tui_runs_meeting_and_fits(tmp_path, new_session, size):
    cfg = make_config(tmp_path, topics=["議題A"])
    s = new_session(cfg)
    app = DartmouthApp(s)
    async with app.run_test(size=size) as pilot:
        await _wait(pilot, lambda: s.engine.status()["waiting"] == "approval")
        narrow = app.screen.has_class("narrow")
        assert narrow == (size[0] < 100)
        inp = app.query_one("#input", Input)
        assert inp.region.bottom <= size[1] and inp.region.height == 3
        _assert_fits(app, *size)
        # 割り込み → 承認（Ctrl+A）
        await pilot.click("#input")
        for ch in "割り込みテスト":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.press("ctrl+a")
        await _wait(pilot, lambda: s.engine.phase == "done")
        await pilot.pause(0.2)
        texts = [i._markdown for i in app.query(LogItem)]
        assert any("割り込みテスト" in t for t in texts)
        assert "[D-001]" in app.query_one("#minutes", Markdown)._markdown
        if narrow:
            await pilot.press("ctrl+t")
            await pilot.pause(0.1)
            assert app.screen.has_class("show-minutes")
            assert app.query_one("#minutes-scroll").region.area > 0
            _assert_fits(app, *size)
        _assert_fits(app, *size)
        await app.run_command("/quit")
    assert (s.dir / "minutes.md").read_text().count("[D-001]") == 1


async def test_tui_ctrl_c_needs_confirmation(tmp_path, new_session):
    cfg = make_config(tmp_path, topics=["議題A"], intro=False)
    s = new_session(cfg)
    app = DartmouthApp(s)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert app.is_running
        await pilot.press("ctrl+c")
        await pilot.pause(0.5)
    recs = (s.dir / "transcript.jsonl").read_text()
    assert "interrupted" in recs or "会議を中断" in recs or "done" in recs


async def test_tui_edit_in_app_with_autosaved_draft(tmp_path, new_session):
    from textual.widgets import TextArea

    from dartmouth.ui.tui import EditScreen

    cfg = make_config(tmp_path, topics=["議題A"], intro=False)
    s = new_session(cfg)
    app = DartmouthApp(s)
    draft = s.dir / "edit_draft_topic1.yaml"
    async with app.run_test(size=(80, 24)) as pilot:
        await _wait(pilot, lambda: s.engine.status()["waiting"] == "approval")
        await app.run_command("/edit")
        await pilot.pause(0.2)
        assert isinstance(app.screen, EditScreen)
        ta = app.screen.query_one(TextArea)
        # 決定事項を書き換える → 自動保存される
        ta.text = ta.text.replace("議題1の決定事項案", "ユーザーが直した決定事項")
        await pilot.pause(0.2)
        assert "ユーザーが直した決定事項" in draft.read_text()
        # Esc で閉じても下書きは残り、承認はされない
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, EditScreen)
        assert s.engine.status()["waiting"] == "approval" and draft.exists()
        # もう一度 /edit すると続きから開く → Ctrl+S で承認
        await app.run_command("/edit")
        await pilot.pause(0.2)
        assert "ユーザーが直した決定事項" in app.screen.query_one(TextArea).text
        await pilot.press("ctrl+s")
        await _wait(pilot, lambda: s.engine.phase == "done")
        assert not draft.exists()
        await app.run_command("/quit")
    assert "ユーザーが直した決定事項" in (s.dir / "minutes.md").read_text()


async def test_edit_draft_survives_resume(tmp_path, new_session):
    from dartmouth.session import resume_session
    from dartmouth.ui.common import load_draft, save_draft

    cfg = make_config(tmp_path, topics=["議題A"], intro=False)
    s = new_session(cfg)
    app = DartmouthApp(s)
    async with app.run_test(size=(80, 24)) as pilot:
        await _wait(pilot, lambda: s.engine.status()["waiting"] == "approval")
        save_draft(s.dir, 1, "decisions:\n- 落ちる前の書きかけ\n")
        await app.run_command("/quit")
    s2 = resume_session(s.dir)
    assert s2.engine.phase == "approval"
    assert "落ちる前の書きかけ" in load_draft(s2.dir, 1, s2.engine.cur.proposal)
    await s2.close()


async def test_tui_approve_button(tmp_path, new_session):
    from textual.widgets import Button

    cfg = make_config(tmp_path, topics=["議題A"], intro=False)
    s = new_session(cfg)
    app = DartmouthApp(s)
    async with app.run_test(size=(100, 30)) as pilot:
        bar = app.query_one("#actions")
        assert not bar.has_class("show")
        await _wait(pilot, lambda: s.engine.status()["waiting"] == "approval")
        await pilot.pause(1.2)  # 状態の更新（1秒ごと）を待つ
        assert bar.has_class("show") and app.query_one("#btn-approve", Button).display
        await pilot.click("#btn-approve")
        await _wait(pilot, lambda: s.engine.phase == "done")
        await app.run_command("/quit")
    assert "[D-001]" in (s.dir / "minutes.md").read_text()
