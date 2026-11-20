"""エンジンが発行するイベントと、その配信。

表示（TUI・簡易表示）と保存は、ここで発行されるイベントを購読するだけにする。
- 同期ハンドラ: publish の中で即座に呼ばれる（transcript の即時書き込みなど、順序と確実性が要るもの）
- 非同期キュー: UI 向け。publish はブロックしない
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("dartmouth.events")

# イベント種別
RECORD = "record"  # transcript.jsonl に書く1行。data はその行の dict
SPEECH_START = "speech_start"  # data: participant, round, topic
SPEECH_END = "speech_end"  # data: participant, round, topic, status, text, elapsed_ms
ROUND_START = "round_start"  # data: round, topic, n
ROUND_END = "round_end"  # data: round, topic
STATE = "state"  # data: Engine.status() の内容
MINUTES = "minutes_updated"  # data: markdown
AWAITING_APPROVAL = "awaiting_approval"  # data: topic, proposal
PAUSED = "paused"  # data: participant, reason
NOTICE = "notice"  # data: text, level   （コマンドへの応答など、記録しない通知）
ERROR = "error"  # data: participant, text, status
MEETING_END = "meeting_end"  # data: reason


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[Handler] = []
        self._queues: list[asyncio.Queue[Event]] = []

    def add_handler(self, handler: Handler) -> None:
        self._handlers.append(handler)

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def publish(self, type_: str, **data: Any) -> Event:
        ev = Event(type_, data)
        for h in self._handlers:
            try:
                h(ev)
            except Exception:  # 保存の失敗で会議を止めない（ログには残す）
                log.exception("event handler failed for %s", type_)
                if type_ == RECORD:
                    raise
        for q in self._queues:
            q.put_nowait(ev)
        return ev
