from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from dartmouth.config import Config
from dartmouth.engine import events as E
from dartmouth.session import Session, create_session
from dartmouth.storage.transcript import read_transcript

ROOT = Path(__file__).resolve().parents[1]


def make_config(tmp_path: Path, participants: dict[str, dict[str, Any]] | None = None, **meeting: Any) -> Config:
    parts = participants or {
        "claude": {"adapter": "dummy", "role": "chair"},
        "gemini": {"adapter": "dummy", "role": "researcher"},
        "critic": {"adapter": "dummy", "role": "third_opinion", "optional": True},
    }
    budget = meeting.pop("budget", {})
    m = {"name": "test", "purpose": "テスト会議", "topics": ["議題A", "議題B"], **meeting}
    return Config.model_validate(
        {
            "meeting": m,
            "budget": budget,
            "participants": parts,
            "prompts_dir": str(ROOT / "prompts"),
            "meetings_dir": str(tmp_path / "meetings"),
        }
    )


class Driver:
    """UI の代わりにイベントを見て操作する。"""

    def __init__(self, session: Session):
        self.s = session
        self.e = session.engine
        self.q = session.engine.bus.subscribe()
        self.events: list[E.Event] = []

    async def until(self, pred, timeout: float = 10) -> E.Event:
        async def _w():
            while True:
                ev = await self.q.get()
                self.events.append(ev)
                if pred(ev):
                    return ev

        return await asyncio.wait_for(_w(), timeout)

    def records(self) -> list[dict[str, Any]]:
        return list(read_transcript(self.s.dir / "transcript.jsonl"))


async def run_auto(session: Session, on_approval=None, timeout: float = 20) -> Driver:
    """承認待ちになったら on_approval(engine, event)（既定: 承認）を呼びながら最後まで回す。"""
    d = Driver(session)
    task = asyncio.create_task(session.engine.run())

    async def loop():
        while True:
            ev = await d.q.get()
            d.events.append(ev)
            if ev.type == E.AWAITING_APPROVAL:
                (on_approval or (lambda e, ev: e.approve()))(session.engine, ev)
            if ev.type == E.MEETING_END:
                return

    await asyncio.wait_for(asyncio.gather(task, loop()), timeout)
    await session.close()
    return d


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def new_session():
    created: list[Session] = []

    def _make(cfg: Config, **kw) -> Session:
        s = create_session(cfg, **kw)
        created.append(s)
        return s

    yield _make


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    # 会議の終わりに台帳へ取り込むので、テストでは本物の台帳（個人用ディレクトリ）に書かない
    monkeypatch.setenv("DARTMOUTH_LEDGER", str(tmp_path / "ledger.sqlite"))
