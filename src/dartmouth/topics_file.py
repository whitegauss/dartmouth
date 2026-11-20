"""設定ファイル・議題ファイルの議題の読み書き。

- 承認された議題を「議題」から「議論済み」へ移す（mark_topics_done）
- 画面から議題を足す・直す・消す（edit_topic）

対象は、ふつうの設定ファイル（meeting.topics / meeting.done_topics）か、議題ファイルの1案件
（「議題.yaml#homeserver」。キーは日本語でも英語でもよい）。コメントや書式を残すため ruamel.yaml で読み書きし、
書き換える前の版は <名前>.bak に残す。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .agenda import JA_KEYS, split_ref

EN_KEYS = {k: k for k in JA_KEYS}


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)  # 「  - 」の字下げ（手で書いた設定ファイルと同じ形）を保つ
    return y


def _locate(data, pid: str | None, create: bool):
    """議題の一覧を持つ辺り（meeting か、議題ファイルの案件）と、使うキーの表を返す。"""
    if pid is None:
        meeting = data.get("meeting") if isinstance(data, dict) else None
        if not isinstance(meeting, dict):
            if not create:
                return None, EN_KEYS
            meeting = CommentedMap()
            data["meeting"] = meeting
        return meeting, EN_KEYS
    themes = data.get("テーマ", data.get("themes")) if isinstance(data, dict) else None
    for th in themes or []:
        for p in (th.get("案件", th.get("projects")) or []):
            if str(p.get("id") or p.get("名前") or p.get("name") or "") == pid:
                ja = any(k in p for k in ("名前", "議題", "議論済み", "目的", "資料"))
                return p, (JA_KEYS if ja else EN_KEYS)
    raise KeyError(f"案件 {pid} が議題ファイルにありません")


def _title(t, km) -> str:
    if isinstance(t, str):
        return t
    t = t or {}
    return str(t.get(km["title"], t.get("title", t.get("題名", ""))))


def _block(seq) -> None:
    """空の一覧（[]）に足したとき、インラインのままにせず箇条書きで書く。"""
    fa = getattr(seq, "fa", None)
    if fa is not None:
        fa.set_block_style()


def _save(p: Path, y: YAML, data) -> None:
    p.with_name(p.name + ".bak").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = p.with_name(f".{p.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        y.dump(data, fh)
    os.replace(tmp, p)


def mark_topics_done(config_path: Path | str, titles: list[str], meeting_id: str) -> list[str]:
    """titles のうち議題にあるものを「議論済み」へ移す。移したタイトルを返す。"""
    p, pid = split_ref(config_path)
    if not p.exists():
        return []
    y = _yaml()
    data = y.load(p.read_text(encoding="utf-8"))
    try:
        box, km = _locate(data, pid, create=False)
    except KeyError:
        return []
    if not isinstance(box, dict) or not box.get(km["topics"]):
        return []
    want = {t.strip() for t in titles}
    keep, moved = [], []
    for t in box[km["topics"]]:
        (moved if _title(t, km).strip() in want else keep).append(t)
    if not moved:
        return []
    box[km["topics"]][:] = keep
    done = box.get(km["done_topics"])
    if done is None:
        done = []
        box[km["done_topics"]] = done
    today = datetime.now().strftime("%Y-%m-%d")
    for t in moved:
        entry = CommentedMap()
        entry[km["title"]] = _title(t, km)
        premise = None if isinstance(t, str) else t.get(km["premise"], t.get("premise"))
        if premise:
            entry[km["premise"]] = premise
        entry[km["meeting"]] = meeting_id
        entry[km["done_at"]] = today
        done.append(entry)
    _block(done)
    _save(p, y, data)
    return [_title(t, km) for t in moved]


def _entry(km, title: str, premise: str = "", group: str | None = None) -> CommentedMap:
    e = CommentedMap()
    e[km["title"]] = title.strip()
    if premise.strip():
        e[km["premise"]] = premise.strip()
    if group:
        e[km["group"]] = group.strip()
    return e


def edit_topic(config_path: Path | str, action: str, title: str, *, new_title: str | None = None, premise: str | None = None,
               group: str | None = None) -> None:
    """議題を足す（add）・直す（update）・消す（remove）。title は既存の議題のタイトル（add では新しいタイトル）。"""
    title = title.strip()
    if not title:
        raise ValueError("議題のタイトルが空です")
    p, pid = split_ref(config_path)
    y = _yaml()
    data = y.load(p.read_text(encoding="utf-8")) or CommentedMap()
    box, km = _locate(data, pid, create=True)
    if box.get(km["topics"]) is None:
        box[km["topics"]] = []
    topics = box[km["topics"]]
    idx = next((i for i, t in enumerate(topics) if _title(t, km).strip() == title), None)
    if action == "add":
        if idx is not None:
            raise ValueError(f"「{title}」はもうあります")
        topics.append(_entry(km, title, premise or "", group))
        _block(topics)
    elif action in ("update", "remove"):
        if idx is None:
            raise KeyError(title)
        if action == "remove":
            del topics[idx]
        else:
            nt = (new_title or title).strip()
            if nt != title and any(_title(t, km).strip() == nt for t in topics):
                raise ValueError(f"「{nt}」はもうあります")
            old = topics[idx]
            e = old if not isinstance(old, str) else _entry(km, title)
            e[km["title"]] = nt
            for key, val in ((km["premise"], premise), (km["group"], group)):
                if val is None:
                    continue
                if val.strip():
                    e[key] = val.strip()
                else:
                    e.pop(key, None)
            topics[idx] = e
    else:
        raise ValueError(f"不明な操作: {action}")
    _save(p, y, data)


# ---------------------------------------------------------------------- 議題ファイルのマイルストーン（期限つき）

MS_JA = {"key": "キー", "title": "名前", "criteria": "達成条件", "due": "期限"}


def _theme(data, theme: str):
    themes = data.get("テーマ", data.get("themes")) if isinstance(data, dict) else None
    for th in themes or []:
        if str(th.get("名前", th.get("name", ""))).strip() == theme:
            return th, ("名前" in th or "案件" in th)
    raise KeyError(f"テーマ「{theme}」が議題ファイルにありません")


def edit_agenda_milestone(path: Path | str, theme: str, action: str, key: str, fields: dict | None = None) -> None:
    """議題ファイルのテーマのマイルストーンを足す（add）・直す（update）・消す（remove）。fields: title / criteria / due / key。"""
    p = Path(path)
    y = _yaml()
    data = y.load(p.read_text(encoding="utf-8"))
    th, ja = _theme(data, theme)
    km = MS_JA if ja else {k: k for k in MS_JA}
    lk = "マイルストーン" if ja else "milestones"
    if th.get(lk) is None:
        th[lk] = []
    mss = th[lk]
    idx = next((i for i, m in enumerate(mss) if str(m.get(km["key"], m.get("key", ""))) == key), None)
    fields = {k: v for k, v in (fields or {}).items() if k in km and v is not None}
    if action == "add":
        if idx is not None:
            raise ValueError(f"{key} はもうあります")
        e = CommentedMap()
        e[km["key"]] = key
        for k in ("title", "criteria", "due"):
            if str(fields.get(k) or "").strip():
                e[km[k]] = str(fields[k]).strip()
        mss.append(e)
        _block(mss)
    elif idx is None:
        raise KeyError(key)
    elif action == "remove":
        del mss[idx]
    elif action == "update":
        e = mss[idx]
        for k, v in fields.items():
            v = str(v).strip()
            if v:
                e[km[k]] = v
            elif k != "key":
                e.pop(km[k], None)
    elif action == "move":
        # fields["to"] の位置へ動かす
        to = max(0, min(len(mss) - 1, int((fields or {}).get("to", idx))))
        mss.insert(to, mss.pop(idx))
    else:
        raise ValueError(f"不明な操作: {action}")
    _save(p, y, data)


def set_agenda_project_due(path: Path | str, pid: str, key: str, due: str) -> None:
    """議題ファイルの案件に、マイルストーンごとの期限（共通の期限の上書き）を書く。空なら消す。"""
    p = Path(path)
    y = _yaml()
    data = y.load(p.read_text(encoding="utf-8"))
    box, km = _locate(data, pid, create=False)
    dk = "マイルストーン期限" if km is JA_KEYS else "milestone_due"
    if box.get(dk) is None:
        box[dk] = CommentedMap()
    if due.strip():
        box[dk][key] = due.strip()
    else:
        box[dk].pop(key, None)
        if not box[dk]:
            del box[dk]
    _save(p, y, data)
