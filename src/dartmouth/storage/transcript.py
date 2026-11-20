"""transcript.jsonl: 1行1イベント、追記のみ。1行ごとに fsync してディスクへ書く。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

from ..engine.events import RECORD, Event

ROUNDS = ("intro", "plan", "independent", "critique", "summary", "approval", "interjection")
KINDS = ("message", "error", "system")
STATUSES = ("ok", "timeout", "failed", "skipped")

# 仕様で必ず持つキー（この順で書く）
BASE_KEYS = ("ts", "meeting", "topic", "round", "speaker", "kind", "text", "elapsed_ms", "chars", "tokens", "status")


class TranscriptWriter:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        ordered = {k: record.get(k) for k in BASE_KEYS}
        ordered.update({k: v for k, v in record.items() if k not in ordered})
        self._fh.write(json.dumps(ordered, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def handle(self, ev: Event) -> None:
        if ev.type == RECORD:
            self.write(ev.data)

    def close(self) -> None:
        self._fh.close()


def read_transcript(path: Path) -> Iterator[dict[str, Any]]:
    """壊れた最終行（書き込み途中で落ちた場合）は読み飛ばす。"""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
