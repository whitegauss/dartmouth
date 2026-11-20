"""`dm web --dev` の自動反映。

- 画面のファイル（web_static/）が変わったら、ブラウザに再読み込みさせる（サーバーはそのまま）
- Python のコードが変わったら、サーバーを再起動する。トークンと Tailscale の中継は引き継ぎ、
  開いていた会議は再起動後に自動で再開する。エージェントが発言している最中は再起動を待つ
  （発言のやり直しでトークンを無駄にしないため）
- プロンプト（prompts/）は毎回読み直しているので、何もしなくても次の発言から反映される

変更の検出はファイルの更新日時を 0.5 秒ごとに見るだけ（依存を増やさない）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_TOKEN = "DARTMOUTH_DEV_TOKEN"
ENV_RESUME = "DARTMOUTH_DEV_RESUME"
ENV_RELAY = "DARTMOUTH_DEV_RELAY"

PACKAGE = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().with_name("web_static")


def snapshot(root: Path, patterns: tuple[str, ...]) -> dict[Path, float]:
    out: dict[Path, float] = {}
    for pat in patterns:
        for p in root.rglob(pat):
            if "__pycache__" in p.parts:
                continue
            try:
                out[p] = p.stat().st_mtime
            except OSError:
                pass
    return out


def changed(old: dict[Path, float], new: dict[Path, float]) -> list[Path]:
    return sorted({p for p in old.keys() | new.keys() if old.get(p) != new.get(p)})


def restart_allowed(hub) -> bool:
    """会議が進んでいない、または誰も発言しておらず人の操作を待っているときだけ再起動してよい。"""
    if not hub.running or hub.session is None:
        return True
    eng = hub.session.engine
    return not eng.inflight and eng._waiting in ("approval", "pause", "topic", "next")


def exec_restart(token: str, resume_dir: Path | None, relay_active: bool) -> None:
    """同じ引数で自分自身を起動し直す（プロセスは置き換わる）。"""
    env = dict(os.environ)
    env[ENV_TOKEN] = token
    env.pop(ENV_RESUME, None)
    if resume_dir:
        env[ENV_RESUME] = str(resume_dir)
    if relay_active:
        env[ENV_RELAY] = "1"
    else:
        env.pop(ENV_RELAY, None)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(sys.executable, [sys.executable, "-m", "dartmouth.cli", *sys.argv[1:]], env)
