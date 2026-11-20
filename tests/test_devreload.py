from __future__ import annotations

import os
import time

from dartmouth.ui import devreload


def test_snapshot_and_changed(tmp_path):
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython-312.pyc").write_text("")
    s1 = devreload.snapshot(tmp_path, ("*.py", "*.pyc"))
    assert [p.name for p in s1] == ["a.py"]  # __pycache__ は見ない
    assert devreload.changed(s1, devreload.snapshot(tmp_path, ("*.py",))) == []
    t = time.time() + 5
    os.utime(tmp_path / "a.py", (t, t))
    (tmp_path / "b.py").write_text("")
    diff = devreload.changed(s1, devreload.snapshot(tmp_path, ("*.py",)))
    assert [p.name for p in diff] == ["a.py", "b.py"]


class _Eng:
    def __init__(self, inflight, waiting):
        self.inflight, self._waiting = inflight, waiting


class _Hub:
    def __init__(self, running, eng=None):
        self.running = running
        self.session = type("S", (), {"engine": eng})() if eng else None


def test_restart_waits_while_agents_speak():
    assert devreload.restart_allowed(_Hub(False))
    assert devreload.restart_allowed(_Hub(True, _Eng({}, "approval")))
    assert devreload.restart_allowed(_Hub(True, _Eng({}, "pause")))
    assert not devreload.restart_allowed(_Hub(True, _Eng({1: object()}, None)))  # 発言中
    assert not devreload.restart_allowed(_Hub(True, _Eng({}, None)))  # ラウンドの合間（すぐ次の発言が始まる）
