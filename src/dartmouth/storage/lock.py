"""会議ディレクトリのロック。1つの会議を2つのプロセスが同時に進めないようにする（flock。プロセスが死ねば自動で外れる）。"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

LOCK = ".lock"


class MeetingLocked(RuntimeError):
    pass


class MeetingLock:
    def __init__(self, d: Path):
        self.path = Path(d) / LOCK
        self.fd: int | None = None

    def acquire(self) -> "MeetingLock":
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)  # 既定で子プロセス・exec に引き継がない
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            pid = self.path.read_text(encoding="utf-8").strip() if self.path.exists() else "?"
            raise MeetingLocked(f"この会議は別のプロセス（pid {pid}）で進行中です") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self.fd = fd
        return self

    def release(self) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def is_locked(d: Path) -> bool:
    """いま誰かがこの会議を進めているか（自分のプロセスが持っている場合も True）。"""
    p = Path(d) / LOCK
    if not p.exists():
        return False
    fd = os.open(p, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
