"""簡易表示（標準入出力）。ヘッドレス実行もここ。"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from typing import TextIO

from ..engine import commands
from ..engine import events as E
from ..session import Session
from .common import ArrivalBuffer, ROUND_LABEL, clear_draft, draft_path, edit_text, format_proposal_md, load_draft, yaml_to_proposal


def _ansi(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"\033[1;38;2;{r};{g};{b}m"


class PlainPrinter:
    def __init__(self, session: Session, out: TextIO = sys.stdout, color: bool | None = None):
        self.s = session
        self.e = session.engine
        self.out = out
        self.color = out.isatty() if color is None else color
        from .common import colors_for

        self.colors = colors_for(self.e)
        self.buf = ArrivalBuffer(session.cfg.meeting.display)

    def label(self, who: str) -> str:
        name = self.e.names.get(who, who)
        if not self.color:
            return f"[{name}]"
        return f"{_ansi(self.colors.get(who, '#cccccc'))}[{name}]\033[0m"

    def p(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def show(self, ev: E.Event) -> None:
        for ev in self.buf.feed(ev):
            self._show(ev)

    def _show(self, ev: E.Event) -> None:
        d = ev.data
        match ev.type:
            case E.ROUND_START:
                t = f"議題{d['topic']} " if d.get("topic") else ""
                n = f" {d['n']}回目" if d.get("round") == "critique" else ""
                self.p(f"\n── {t}{ROUND_LABEL.get(d['round'], d['round'])}{n} ──")
            case E.SPEECH_START:
                self.p(f"{self.label(d['participant'])} 考え中…" + (f"（再試行 {d['attempt'] - 1}）" if d.get("attempt", 1) > 1 else ""))
            case E.SPEECH_END:
                if d["status"] == "ok" and d["round"] == "summary" and d.get("topic") is not None:
                    self.p(f"{self.label(d['participant'])} まとめを作成しました ({d['elapsed_ms'] / 1000:.1f}s)")
                elif d["status"] == "ok":
                    self.p(f"{self.label(d['participant'])} ({d['elapsed_ms'] / 1000:.1f}s)\n{d['text']}\n")
                else:
                    self.p(f"{self.label(d['participant'])} ⚠ {d['status']}: {d['text']}")
            case E.RECORD:
                if d["kind"] == "system" and d.get("event") not in ("checkpoint", "stats"):
                    self.p(f"  · {d['text']}")
                elif d["speaker"] == "user" and d["kind"] == "message":
                    self.p(f"{self.label('user')} {d['text']}")
            case E.AWAITING_APPROVAL:
                self.p(f"\n=== 議題{d['topic']}「{d['title']}」議長のまとめ ===\n{format_proposal_md(d['proposal'])}")
                self.p("→ /approve 承認  /edit 修正して承認  /reject <理由> 却下")
            case E.PAUSED:
                self.p(f"\n!!! {self.e.names[d['participant']]} が応答しません: {d['reason']}\n→ /retry 再試行  /continue 飛ばして続行  /abort 中断  /skip {d['participant']}")
            case E.NOTICE:
                self.p(f"  » {d['text']}")
            case E.MEETING_END:
                self.p(f"\n会議終了（{d['reason']}）  記録: {self.s.dir}")


class _Stdin:
    """/edit でエディタを開いている間、標準入力の読み取りを止めるための入れ物。"""

    transport: asyncio.ReadTransport | None = None

    @classmethod
    def pause(cls) -> None:
        if cls.transport is not None:
            cls.transport.pause_reading()
            os.set_blocking(sys.stdin.fileno(), True)  # エディタは通常のブロッキング入力を期待する

    @classmethod
    def resume(cls) -> None:
        if cls.transport is not None:
            os.set_blocking(sys.stdin.fileno(), False)
            cls.transport.resume_reading()


async def _stdin_lines():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
        pipe_like = stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or sys.stdin.isatty()
    except (OSError, ValueError):
        pipe_like = False
    try:
        if not pipe_like:
            raise PermissionError("stdin is not a pipe or tty")
        _Stdin.transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    except (PermissionError, ValueError, OSError):
        # 標準入力が通常ファイルや /dev/null のときはパイプとして扱えないので、スレッドで読む
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return
            yield line.rstrip("\n")
    while True:
        line = await reader.readline()
        if not line:
            return
        yield line.decode(errors="replace").rstrip("\n")


async def run_plain(session: Session, *, interactive: bool = True, out: TextIO = sys.stdout) -> int:
    printer = PlainPrinter(session, out)
    q = session.engine.bus.subscribe()
    engine_task = asyncio.create_task(session.engine.run())

    async def pump():
        while True:
            ev = await q.get()
            printer.show(ev)
            if ev.type == E.MEETING_END:
                return

    async def read_input():
        async for line in _stdin_lines():
            res = commands.handle(session.engine, line)
            if res.message:
                printer.p(f"  » {res.message}")
            if res.ui_action == "quit":
                engine_task.cancel()
                return
            if res.ui_action == "edit":
                prop = session.engine.current_proposal()
                if prop is None:
                    continue
                topic = session.engine.cur.no
                _Stdin.pause()
                try:
                    # 会議ディレクトリの下書きを直接編集する（落ちても残り、次の /edit で続きから開く）
                    text = await asyncio.to_thread(
                        edit_text, load_draft(session.dir, topic, prop), path=draft_path(session.dir, topic))
                finally:
                    _Stdin.resume()
                try:
                    edited = yaml_to_proposal(text)
                except Exception as e:  # noqa: BLE001
                    printer.p(f"  » 修正内容を読めませんでした: {e}")
                    continue
                if edited is None:
                    clear_draft(session.dir, topic)
                    printer.p("  » 承認を取りやめました")
                else:
                    printer.p(f"  » {session.engine.approve(edited)}")
                    clear_draft(session.dir, topic)

    pump_task = asyncio.create_task(pump())
    input_task = asyncio.create_task(read_input()) if interactive else None
    rc = 0
    try:
        try:
            await asyncio.wait({engine_task}, return_when=asyncio.ALL_COMPLETED)
        except asyncio.CancelledError:
            engine_task.cancel()
        try:
            await engine_task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001  想定外の失敗でも後片付けはする
            printer.p(f"\n内部エラーで停止しました: {type(e).__name__}: {e}\n再開: dartmouth resume {session.dir}")
            rc = 1
        try:
            await asyncio.wait_for(pump_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pump_task.cancel()
    finally:
        if input_task:
            input_task.cancel()
        await session.close()
    return rc
