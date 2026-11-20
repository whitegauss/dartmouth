"""Textual の TUI。エンジンのイベントを購読して表示し、入力をコマンドとしてエンジンに渡すだけ。

幅が広いとき: 左に会議ログ、右に議事録と状態の3ペイン
幅が狭いとき（< 100桁、80×24 を含む）: 会議ログ／議事録をタブで切り替え、状態は1行で下に出す
"""

from __future__ import annotations

import asyncio
import time

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Markdown, Static, Tab, Tabs, TextArea

from ..engine import commands
from ..engine import events as E
from ..session import Session
from .common import (
    ROUND_LABEL,
    ArrivalBuffer,
    clear_draft,
    colors_for,
    format_proposal_md,
    load_draft,
    save_draft,
    status_lines,
    yaml_to_proposal,
)

NARROW = 100
MAX_LOG_ITEMS = 300


class EditScreen(ModalScreen):
    """議長のまとめを画面の中で直す。外部エディタ（vim など）の操作を覚えなくてよい。

    Ctrl+S: 保存して承認 / Esc: 閉じる。書きかけは変更のたびに会議ディレクトリへ自動保存する。
    """

    DEFAULT_CSS = """
    EditScreen { align: center middle; }
    #edit-box { width: 100%; height: 100%; border: round $accent; padding: 0 1; }
    #edit-help { height: 1; color: $text-muted; }
    #edit-area { height: 1fr; }
    """
    BINDINGS = [
        Binding("ctrl+s", "save", "保存して承認", priority=True),
        Binding("escape", "close", "閉じる（下書きは残る）", priority=True),
    ]

    def __init__(self, text: str, on_change):
        super().__init__()
        self._text = text
        self._on_change = on_change

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-box"):
            yield Static("Ctrl+S 保存して承認 ｜ Esc 閉じる（書きかけは自動保存）｜ Ctrl+Z 元に戻す", id="edit-help")
            yield TextArea(self._text, id="edit-area", soft_wrap=True, show_line_numbers=False)

    def on_mount(self) -> None:
        self.query_one("#edit-box").border_title = "議長のまとめを修正"
        self.query_one(TextArea).focus()

    @on(TextArea.Changed)
    def _changed(self, event: TextArea.Changed) -> None:
        self._on_change(event.text_area.text)

    def action_save(self) -> None:
        self.dismiss(self.query_one(TextArea).text)

    def action_close(self) -> None:
        self.dismiss(None)


class LogItem(Markdown):
    DEFAULT_CSS = """
    LogItem { margin: 0 0 1 0; padding: 0 1; border: round $panel; height: auto; }
    LogItem.system { border: none; color: $text-muted; margin: 0; padding: 0 1; }
    """


class DartmouthApp(App):
    TITLE = "Dartmouth"
    CSS = """
    Screen { layout: vertical; }
    #tabs { display: none; height: 2; }
    #main { height: 1fr; }
    #log { width: 2fr; height: 1fr; border: round $primary-background; scrollbar-size-vertical: 1; }
    #side { width: 1fr; min-width: 30; height: 1fr; }
    #minutes-scroll { height: 1fr; border: round $primary-background; scrollbar-size-vertical: 1; }
    #status { height: auto; max-height: 10; border: round $primary-background; padding: 0 1; }
    #statusbar { display: none; height: 1; padding: 0 1; background: $boost; }
    #input { height: 3; }
    #actions { display: none; height: 3; padding: 0 1; }
    #actions.show { display: block; }
    #actions Button { min-width: 10; margin-right: 1; height: 3; }
    .pause-btn { display: none; }
    #actions.paused .pause-btn { display: block; }
    #actions.paused .approve-btn { display: none; }
    #log:focus, #minutes-scroll:focus { border: round $accent; }

    Screen.narrow #tabs { display: block; }
    Screen.narrow #statusbar { display: block; }
    Screen.narrow #status { display: none; }
    Screen.narrow #side { display: none; width: 1fr; }
    Screen.narrow #log { width: 1fr; }
    Screen.narrow.show-minutes #log { display: none; }
    Screen.narrow.show-minutes #side { display: block; }
    """

    BINDINGS = [
        Binding("ctrl+n", "next", "次へ", priority=True),
        Binding("ctrl+a", "approve", "承認", priority=True),
        Binding("ctrl+c", "confirm_quit", "終了", priority=True),
        Binding("ctrl+t", "toggle_tab", "ログ/議事録", priority=True, show=False),
    ]

    def __init__(self, session: Session):
        super().__init__()
        self.s = session
        self.e = session.engine
        self.pcolors = colors_for(self.e)
        self.buf = ArrivalBuffer(session.cfg.meeting.display)
        self._quit_armed = 0.0
        self._engine_task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self.exit_code = 0

    def compose(self) -> ComposeResult:
        yield Tabs(Tab("会議ログ", id="tab-log"), Tab("議事録", id="tab-minutes"), id="tabs")
        with Horizontal(id="main"):
            yield VerticalScroll(id="log")
            with Vertical(id="side"):
                with VerticalScroll(id="minutes-scroll"):
                    yield Markdown("", id="minutes")
                yield Static("", id="status")
        yield Static("", id="statusbar")
        # 承認待ち・一時停止のときだけ出るボタン（毎回コマンドを打たなくてよい）
        with Horizontal(id="actions"):
            yield Button("承認 (Ctrl+A)", id="btn-approve", variant="success", classes="approve-btn")
            yield Button("修正して承認", id="btn-edit", classes="approve-btn")
            yield Button("却下…", id="btn-reject", variant="error", classes="approve-btn")
            yield Button("再試行", id="btn-retry", classes="pause-btn")
            yield Button("飛ばして続行", id="btn-continue", classes="pause-btn")
            yield Button("中断", id="btn-abort", variant="error", classes="pause-btn")
        yield Input(placeholder="発言（割り込み）または /コマンド　/help で一覧", id="input")

    async def on_mount(self) -> None:
        self.query_one("#log").border_title = "会議ログ"
        self.query_one("#minutes-scroll").border_title = "議事録"
        self.query_one("#status").border_title = "状態"
        self._apply_width(self.size.width)
        q = self.e.bus.subscribe()
        self._pump_task = asyncio.create_task(self._pump(q))
        self._engine_task = asyncio.create_task(self.e.run())
        self._engine_task.add_done_callback(self._engine_done)
        self.set_interval(1.0, self._refresh_status)
        self.query_one("#input", Input).focus()

    def on_resize(self, event) -> None:
        self._apply_width(event.size.width)

    def _apply_width(self, width: int) -> None:
        self.screen.set_class(width < NARROW, "narrow")

    # ---------------------------------------------------------------- イベント表示

    async def _pump(self, q: asyncio.Queue) -> None:
        while True:
            ev = await q.get()
            for ev2 in self.buf.feed(ev):
                try:
                    await self._show(ev2)
                except Exception as exc:  # noqa: BLE001  表示の失敗で会議を止めない
                    self.log.error(f"render failed: {exc}")
            if ev.type == E.MEETING_END:
                return

    async def _add(self, label: str, body: str, who: str = "system", system: bool = False) -> None:
        log = self.query_one("#log", VerticalScroll)
        at_bottom = log.scroll_y >= log.max_scroll_y - 2
        item = LogItem(body, classes="system" if system else "")
        if not system:
            item.border_title = Text(label, style=f"bold {self.pcolors.get(who, '#cccccc')}")
            item.styles.border = ("round", self.pcolors.get(who, "#888888"))
        await log.mount(item)
        items = log.query(LogItem)
        if len(items) > MAX_LOG_ITEMS:
            await items.first().remove()
        if at_bottom:
            log.scroll_end(animate=False)

    async def _show(self, ev: E.Event) -> None:
        d = ev.data
        name = lambda n: self.e.names.get(n, n)  # noqa: E731
        match ev.type:
            case E.ROUND_START:
                t = f"議題{d['topic']} " if d.get("topic") else ""
                n = f" {d['n']}回目" if d.get("round") == "critique" else ""
                await self._add("", f"**── {t}{ROUND_LABEL.get(d['round'], d['round'])}{n} ──**", system=True)
            case E.SPEECH_END:
                who = d["participant"]
                label = f"{name(who)} · {ROUND_LABEL.get(d['round'], d['round'])} · {d['elapsed_ms'] / 1000:.0f}s"
                if d["status"] == "ok" and d["round"] == "summary" and d.get("topic") is not None:
                    await self._add("", f"· {name(who)} がまとめを作成しました（{d['elapsed_ms'] / 1000:.0f}s）", system=True)
                elif d["status"] == "ok":
                    await self._add(label, d["text"], who)
                else:
                    await self._add("", f"⚠ **{name(who)}** {d['status']}: {d['text']}", system=True)
            case E.RECORD:
                if d["kind"] == "system" and d.get("event") not in ("checkpoint", "stats"):
                    await self._add("", f"· {d['text']}", system=True)
                elif d["speaker"] == "user" and d["kind"] == "message":
                    to = f" → {name(d['to'])}" if d.get("to") else ""
                    await self._add(f"ユーザー{to}", d["text"], "user")
            case E.AWAITING_APPROVAL:
                body = format_proposal_md(d["proposal"]) + "\n\n`/approve`（Ctrl+A）承認　`/edit` 修正して承認　`/reject <理由>` 却下"
                await self._add(f"議長のまとめ · 議題{d['topic']}「{d['title']}」", body, d.get("chair") or self.e.cfg.chair)
                self.notify("議長のまとめが出ました。承認・修正・却下を選んでください", timeout=6)
            case E.PAUSED:
                await self._add(
                    "一時停止",
                    f"**{name(d['participant'])}** が応答しません: {d['reason']}\n\n"
                    f"`/retry` 再試行　`/continue` 飛ばして続行　`/abort` 中断",
                    "system",
                )
                self.notify(f"{name(d['participant'])} が応答しないため一時停止しました", severity="warning", timeout=8)
            case E.MINUTES:
                await self.query_one("#minutes", Markdown).update(d["markdown"])
            case E.NOTICE:
                self.notify(d["text"], severity="warning" if d.get("level") == "warning" else "information")
            case E.STATE:
                self._render_status(d)
            case E.MEETING_END:
                await self._add("", f"**会議終了（{d['reason']}）** 記録: `{self.s.dir}`　/quit で閉じます", system=True)

    def _engine_done(self, task: asyncio.Task) -> None:
        if task.cancelled() or task.exception() is None:
            return
        exc = task.exception()
        self.exit_code = 1
        self.notify(f"内部エラーで停止しました: {type(exc).__name__}: {exc}　/quit で閉じ、resume で再開できます",
                    severity="error", timeout=30)

    def _refresh_status(self) -> None:
        self._render_status(self.e.status())

    def _render_status(self, st: dict) -> None:
        lines = status_lines(st, self.e.names)
        self.query_one("#status", Static).update("\n".join(lines))
        self.query_one("#statusbar", Static).update(" │ ".join(lines))
        bar = self.query_one("#actions")
        bar.set_class(st.get("waiting") in ("approval", "pause"), "show")
        bar.set_class(st.get("waiting") == "pause", "paused")

    @on(Button.Pressed)
    async def _button(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "btn-reject":
            # 理由が要るので、入力欄に下書きを入れて理由だけ打ってもらう
            inp = self.query_one("#input", Input)
            inp.value = "/reject "
            inp.focus()
            inp.cursor_position = len(inp.value)
            self.notify("却下の理由を書いて Enter を押してください")
            return
        cmd = {"btn-approve": "/approve", "btn-edit": "/edit", "btn-retry": "/retry",
               "btn-continue": "/continue", "btn-abort": "/abort"}.get(bid)
        if cmd:
            await self.run_command(cmd)
            self.query_one("#input", Input).focus()

    # ---------------------------------------------------------------- 入力

    @on(Input.Submitted, "#input")
    async def _submitted(self, event: Input.Submitted) -> None:
        line = event.value
        event.input.value = ""
        await self.run_command(line)

    async def run_command(self, line: str) -> None:
        try:
            res = commands.handle(self.e, line)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"エラー: {exc}", severity="error")
            return
        if res.message:
            self.notify(res.message, severity="warning" if res.level == "warning" else "information",
                        timeout=8 if "\n" in res.message else 4)
        if res.ui_action == "quit":
            await self.action_quit_meeting()
        elif res.ui_action == "edit":
            await self._edit()

    async def _edit(self) -> None:
        prop = self.e.current_proposal()
        if prop is None or self.e.cur is None:
            return
        topic, d = self.e.cur.no, self.s.dir
        initial = load_draft(d, topic, prop)

        def done(text: str | None) -> None:
            if text is None:
                self.notify("閉じました。書きかけは保存してあるので、/edit で続きから開けます")
                return
            try:
                edited = yaml_to_proposal(text)
            except Exception as exc:  # noqa: BLE001
                save_draft(d, topic, text)
                self.notify(f"修正内容を読めませんでした（書きかけは保存済み。/edit で直せます）: {exc}", severity="error", timeout=10)
                return
            if edited is None:
                self.notify("中身が空なので承認しませんでした")
                return
            self.notify(self.e.approve(edited))
            clear_draft(d, topic)

        self.push_screen(EditScreen(initial, lambda t: save_draft(d, topic, t)), done)

    @on(Tabs.TabActivated, "#tabs")
    def _tab(self, event: Tabs.TabActivated) -> None:
        self.screen.set_class(event.tab.id == "tab-minutes", "show-minutes")

    def action_toggle_tab(self) -> None:
        tabs = self.query_one("#tabs", Tabs)
        tabs.active = "tab-log" if tabs.active == "tab-minutes" else "tab-minutes"

    async def action_next(self) -> None:
        await self.run_command("/next")

    async def action_approve(self) -> None:
        await self.run_command("/approve")

    async def action_confirm_quit(self) -> None:
        now = time.monotonic()
        if now - self._quit_armed < 3:
            await self.action_quit_meeting()
        else:
            self._quit_armed = now
            self.notify("もう一度 Ctrl+C で保存して終了します（resume で再開できます）", severity="warning", timeout=3)

    async def action_quit_meeting(self) -> None:
        if self._engine_task and not self._engine_task.done():
            self._engine_task.cancel()
            try:
                await self._engine_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._pump_task:
            try:
                await asyncio.wait_for(self._pump_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._pump_task.cancel()
        await self.s.close()
        self.exit(self.exit_code)


def run_tui(session: Session) -> int:
    app = DartmouthApp(session)
    rc = app.run()
    print(f"記録: {session.dir}\n再開: dartmouth resume {session.dir}")
    return rc or 0
