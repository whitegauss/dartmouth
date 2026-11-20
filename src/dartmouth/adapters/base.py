"""全エージェント共通のアダプタインターフェース。

新しいエージェントを足すときは Adapter を継承したクラスを1つ書き、
config.yaml の adapter に登録名か "パッケージ.モジュール:クラス名" を書く。
エンジンの変更は不要。
"""

from __future__ import annotations

import abc
import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("dartmouth.adapter")


class AdapterError(Exception):
    """アダプタの呼び出しに失敗した（終了コード異常、出力の解析失敗、セレクタ切れなど）。"""


@dataclass
class TurnContext:
    """1回の発言のためにエンジンがアダプタへ渡すもの。"""

    prompt: str  # 役割・目的・議事録・今の議題の発言・指示をすべて含む完成済みのプロンプト
    participant: str
    role: str
    round: str  # intro / independent / critique / summary / interjection
    topic: int | None
    meeting_id: str
    meeting_dir: Path | None = None
    # 議長のまとめなど、決まったJSONで返してほしいときの JSON Schema
    response_schema: dict[str, Any] | None = None


@dataclass
class Reply:
    text: str
    tokens: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # response_schema を渡したときに、アダプタが構造化出力を直接取れた場合に入れる
    structured: Any = None


class Adapter(abc.ABC):
    """プロンプトを受け取って発言を返す。タイムアウトはエンジン側でも強制する。"""

    def __init__(self, name: str, role: str, timeout_s: float, options: dict[str, Any] | None = None):
        self.name = name
        self.role = role
        self.timeout_s = timeout_s
        self.options = options or {}

    @abc.abstractmethod
    async def respond(self, ctx: TurnContext) -> Reply: ...

    async def close(self) -> None:
        """ブラウザなどの後片付け。"""


async def run_subprocess(
    argv: list[str],
    *,
    stdin: str | None = None,
    timeout_s: float,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    name: str = "",
) -> tuple[int, str, str]:
    """シェルを通さずにコマンドを実行する。タイムアウト・キャンセル時はプロセスグループごと kill する。"""
    log.info("[%s] exec %s (cwd=%s)", name, _redact_argv(argv), cwd)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        start_new_session=True,  # 子孫プロセスもまとめて kill できるようにする
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout=timeout_s
        )
    except BaseException:
        _kill_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except BaseException:
            pass
        log.warning("[%s] killed (timeout or cancelled)", name)
        raise
    o, e = out.decode(errors="replace"), err.decode(errors="replace")
    log.info("[%s] exit=%s stderr=%s", name, proc.returncode, e[-2000:].strip())
    return proc.returncode or 0, o, e


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _redact_argv(argv: list[str]) -> str:
    # 長い引数（プロンプト本文）はログに残さない
    return " ".join(a if len(a) <= 200 else f"<{len(a)} chars>" for a in argv)
