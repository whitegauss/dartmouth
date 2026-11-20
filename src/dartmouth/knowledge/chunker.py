"""ファイルを、出典（パスと行番号）つきのかたまりに分ける。エージェントが `ファイル:行番号` で引用できるようにする。"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class RawChunk:
    start: int  # 1始まり
    end: int
    text: str


MAX_LINES = 60
MAX_CHARS = 1500


def chunk_text(text: str, suffix: str) -> list[RawChunk]:
    lines = text.splitlines()
    if suffix == ".py":
        bounds = [i for i, l in enumerate(lines) if re.match(r"(async\s+def|def|class)\s", l) or re.match(r"    (async\s+def|def)\s", l)]
    elif suffix in (".md", ".markdown"):
        bounds = [i for i, l in enumerate(lines) if re.match(r"#{1,4}\s", l)]
    else:
        bounds = []
    starts = sorted(set([0] + bounds))
    out: list[RawChunk] = []
    for a, b in zip(starts, starts[1:] + [len(lines)]):
        out += _split_long(lines, a, b)
    return [c for c in out if c.text.strip()]


def _split_long(lines: list[str], a: int, b: int) -> list[RawChunk]:
    """長い区間は MAX_LINES / MAX_CHARS ごとに切る。"""
    out = []
    i = a
    while i < b:
        j, size = i, 0
        while j < b and j - i < MAX_LINES and size + len(lines[j]) <= MAX_CHARS:
            size += len(lines[j]) + 1
            j += 1
        if j == i:  # 1行だけで上限を超える
            j = i + 1
        out.append(RawChunk(i + 1, j, "\n".join(lines[i:j])))
        i = j
    return out
