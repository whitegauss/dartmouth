"""議題ファイル: テーマ → 案件 → 議題 を1つの YAML にまとめる。

キーは日本語でも英語でも書ける（例: 「題名」= title）。画面ではテーマと案件をプルダウンで選び、
議題をカードで選ぶ。案件1つが、これまでの設定ファイル1つ（meeting の中身）に当たる。

    共通設定:            # 全案件に共通の会議設定（meeting の項目。案件の「会議設定」で上書きできる）
      critique_rounds: 2
    テーマ:
      - 名前: ホームサーバー
        説明: …
        マイルストーン:
          - キー: M1
            名前: 情報収集
            達成条件: 判断に必要な質問への回答が揃っている
            期限: 2026-12          # 2026 / 2026-12 / 2026-12-31 / 2027年3月 のどれでもよい
        案件:
          - id: homeserver        # 台帳と会議ディレクトリの名前に使う（英数字）
            名前: ホームサーバーの構築
            目的: …
            資料: [...]            # meeting.context と同じ
            会議設定: {...}        # meeting のほかの項目
            議題:
              - 題名: …
                前提: …
                グループ: …
            議論済み:              # 承認された議題がここへ移る
              - 題名: …
                会議: 2026-11-19-homeserver
                日付: '2026-11-19'

設定ファイルとして使うときは「<ファイル>#<案件id>」と書く（例: 議題.yaml#homeserver）。
"""

from __future__ import annotations

import calendar
import re
from pathlib import Path
from typing import Any

import yaml

TOP = {"共通設定": "defaults", "テーマ": "themes"}
THEME = {"名前": "name", "説明": "description", "マイルストーン": "milestones", "案件": "projects"}
PROJECT = {"名前": "name", "説明": "description", "目的": "purpose", "資料": "context", "会議設定": "meeting",
           "議題": "topics", "議論済み": "done_topics", "マイルストーン期限": "milestone_due"}
TOPIC = {"題名": "title", "前提": "premise", "グループ": "group", "参加者": "participants", "議長": "chair"}
DONE = {"題名": "title", "前提": "premise", "会議": "meeting", "日付": "done_at"}
MILESTONE = {"キー": "key", "名前": "title", "name": "title", "達成条件": "criteria", "期限": "due"}
CONTEXT = {"見出し": "label", "本文": "text", "ファイル": "file", "非公開": "private"}

# 書き戻すときに使う、日本語のキー（英語のファイルなら英語のまま）
JA_KEYS = {"topics": "議題", "done_topics": "議論済み", "title": "題名", "premise": "前提", "group": "グループ",
           "meeting": "会議", "done_at": "日付"}


def _norm(d: Any, table: dict[str, str]) -> Any:
    if not isinstance(d, dict):
        return d
    return {table.get(k, k): v for k, v in d.items()}


def is_agenda(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("テーマ", data.get("themes")), list)


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """日本語のキーを英語にそろえた形にする。"""
    top = _norm(data, TOP)
    themes = []
    for th in top.get("themes") or []:
        th = _norm(th, THEME)
        projects = []
        for p in th.get("projects") or []:
            p = _norm(p, PROJECT)
            pid = str(p.get("id") or p.get("name") or "").strip()
            if not pid:
                continue
            topics = [t if isinstance(t, str) else _norm(t, TOPIC) for t in (p.get("topics") or [])]
            done = [{"title": t} if isinstance(t, str) else _norm(t, DONE) for t in (p.get("done_topics") or [])]
            ctx = [_norm(c, CONTEXT) for c in (p.get("context") or [])]
            projects.append({**p, "id": pid, "name": str(p.get("name") or pid), "topics": topics, "done_topics": done,
                             "context": ctx, "meeting": dict(p.get("meeting") or {})})
        mss = []
        for m in th.get("milestones") or []:
            m = _norm(m, MILESTONE)
            if m.get("key") and m.get("title"):
                mss.append({"key": str(m["key"]), "title": str(m["title"]), "criteria": str(m.get("criteria") or ""),
                            "due": str(m["due"]) if m.get("due") not in (None, "") else ""})
        themes.append({"name": str(th.get("name") or "").strip() or "（名前なし）", "description": str(th.get("description") or ""),
                       "milestones": mss, "projects": projects})
    extra = {k: v for k, v in top.items() if k not in ("defaults", "themes")}
    return {"defaults": dict(top.get("defaults") or {}), "themes": themes, "extra": extra}


def load(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not is_agenda(data):
        raise ValueError(f"{path} は議題ファイル（テーマ: の一覧）ではありません")
    return normalize(data)


def split_ref(ref: str | Path) -> tuple[Path, str | None]:
    """「ファイル#案件id」を分ける。# がなければ (ファイル, None)。"""
    s = str(ref)
    if "#" in s and not Path(s).exists():
        p, pid = s.rsplit("#", 1)
        return Path(p), pid
    return Path(s), None


def find(ag: dict[str, Any], pid: str) -> tuple[dict[str, Any], dict[str, Any]]:
    for th in ag["themes"]:
        for p in th["projects"]:
            if p["id"] == pid:
                return th, p
    raise KeyError(f"案件 {pid} が議題ファイルにありません")


def config_data(data: dict[str, Any], pid: str | None) -> dict[str, Any]:
    """議題ファイルの1案件を、ふつうの設定ファイルと同じ形（meeting: …）にする。"""
    ag = normalize(data)
    if not pid:
        raise ValueError("議題ファイルは「ファイル#案件id」の形で指定してください（例: 議題.yaml#homeserver）")
    th, p = find(ag, pid)
    meeting = {**ag["defaults"], **p["meeting"]}
    meeting.update(name=p["name"], slug=meeting.get("slug") or p["id"], project=p["id"], theme=th["name"],
                   topics=p["topics"], done_topics=p["done_topics"])
    if p.get("purpose"):
        meeting["purpose"] = p["purpose"]
    if p["context"]:
        meeting["context"] = p["context"]
    return {**ag["extra"], "meeting": meeting}


def due_key(due: str) -> str | None:
    """期限を並べ替えられる日付（期間の最終日、YYYY-MM-DD）にする。読めなければ None。

    2026 → 2026-12-31、2026-03 / 2026年3月 → 2026-03-31、2026-03-15 / 2026年3月15日 → 2026-03-15、2026Q2 → 2026-06-30
    """
    s = str(due or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{4})\s*[-/年]?\s*[QqＱ]([1-4])", s) or re.fullmatch(r"(\d{4})年?第?([1-4])四半期", s)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        mo = q * 3
        return f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
    m = re.fullmatch(r"(\d{4})(?:[-/年]\s*(\d{1,2})月?(?:[-/]?\s*(\d{1,2})日?)?)?(?:年)?(?:度)?(?:末)?", s)
    if not m:
        return None
    y = int(m.group(1))
    if m.group(2) is None:
        return f"{y:04d}-12-31"
    mo = int(m.group(2))
    if not 1 <= mo <= 12:
        return None
    d = int(m.group(3)) if m.group(3) else calendar.monthrange(y, mo)[1]
    if not 1 <= d <= calendar.monthrange(y, mo)[1]:
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"
