"""個人用ディレクトリ（dartmouth-personal）と台帳の置き場所。"""

from __future__ import annotations

import os
from pathlib import Path


def personal_dir() -> Path:
    """DARTMOUTH_PERSONAL_DIR、なければ ../dartmouth-personal、それもなければ ~/dartmouth-personal。"""
    if os.environ.get("DARTMOUTH_PERSONAL_DIR"):
        return Path(os.environ["DARTMOUTH_PERSONAL_DIR"])
    rel = Path("../dartmouth-personal")
    return rel if rel.is_dir() else Path.home() / "dartmouth-personal"


def ledger_path() -> Path:
    """台帳（SQLite）の場所。DARTMOUTH_LEDGER、なければ個人用ディレクトリ、それもなければカレントの ledger.sqlite。"""
    if os.environ.get("DARTMOUTH_LEDGER"):
        return Path(os.environ["DARTMOUTH_LEDGER"])
    p = personal_dir()
    return (p if p.is_dir() else Path(".")) / "ledger.sqlite"
