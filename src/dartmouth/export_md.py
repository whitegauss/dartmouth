"""全会議・全アイテムを1つの Markdown に書き出す（AI に棚卸しを任せるため）。

データは読むだけ。アイテム（決定事項・未決事項・ToDo）と状態は各会議の記録（storage 層の archive.summarize）から取り、
Web の「決定・未決・ToDo」と同じ件数になる。台帳（ledger.sqlite）があれば読み取り専用で開き、
根拠・分かればよいこと・ボール（次に誰が動く番か）・台帳での状態を足す。

並びは決定的: 会議は日付（開始日時）→ 会議ID の順、アイテムは ID の順。生成日時の行のほかは、
同じデータからは毎回バイト単位で同じ出力になる。

    dm export-md --out exports/context-pack.md
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .storage import archive
from .storage.minutes import owner_label

KIND_JA = {"decision": "決定事項", "question": "未決事項", "todo": "ToDo"}
# Web の「決定・未決・ToDo」の画面と同じ状態の呼び方（記録上は done が真か偽か）
STATUS_JA = {("decision", False): "決定", ("decision", True): "取り下げ",
             ("question", False): "未解決", ("question", True): "解決済み",
             ("todo", False): "未完了", ("todo", True): "完了"}
KEYS = (("decision", "decisions"), ("question", "open_questions"), ("todo", "todos"))
TITLE_MAX = 80


def _id_key(item_id: str) -> tuple[str, int, str]:
    m = re.match(r"([A-Za-z]+)-(\d+)$", item_id or "")
    return (m.group(1), int(m.group(2)), item_id) if m else (item_id or "", 0, item_id or "")


def _one(text: str) -> str:
    return " ".join((text or "").split())


def _title(text: str) -> str:
    first = _one((text or "").splitlines()[0] if (text or "").strip() else "")
    return first if len(first) <= TITLE_MAX else first[:TITLE_MAX] + "…"


def _meeting_dirs(dirs: Iterable[Path]) -> list[Path]:
    out = []
    for base in dirs:
        base = Path(base)
        if base.is_dir():
            out += [d for d in base.iterdir() if (d / "transcript.jsonl").exists()]
    return out


def _ledger_rows(ledger_path: Path | None) -> dict[str, dict[str, Any]]:
    """台帳の行（uid → 行＋ボール）。台帳がなければ空。読み取り専用で開く。"""
    if not ledger_path or not Path(ledger_path).exists():
        return {}
    from .ledger import Ledger, ball_of

    lg = Ledger.readonly(ledger_path)
    try:
        rows = {r["uid"]: r for r in lg.q("SELECT * FROM items")}
        for uid, r in rows.items():
            blocked = r["status"] in ("open", "held") and bool(lg.blockers(uid))
            r["ball_now"] = ball_of(r, blocked)
            r["blocked_by"] = [b["dst"] for b in lg.blockers(uid)] if blocked else []
        return rows
    finally:
        lg.close()


def export_markdown(meeting_dirs: Iterable[Path], ledger_path: Path | None = None, now: datetime | None = None) -> str:
    from .ledger import BALL_LABEL, KIND_LABEL, STATUS_LABEL

    led = _ledger_rows(ledger_path)
    meetings = []
    for d in _meeting_dirs(meeting_dirs):
        sm = archive.summarize(d)
        topics = {t["no"]: t["title"] for t in sm["topics"]}
        items = []
        for kind, key in KEYS:
            for it in sm[key]:
                items.append({**it, "kind": kind, "topic_title": topics.get(it.get("topic"), "")})
        items.sort(key=lambda x: _id_key(x["id"]))
        meetings.append({"sm": sm, "items": items})
    meetings.sort(key=lambda m: ((m["sm"]["started"] or ""), m["sm"]["id"]))

    all_items = [it for m in meetings for it in m["items"]]
    open_n = sum(1 for it in all_items if it["kind"] != "decision" and not it["done"])
    by_status: dict[str, int] = {}
    for it in all_items:
        lab = STATUS_JA[(it["kind"], it["done"])]
        by_status[f"{KIND_JA[it['kind']]}・{lab}"] = by_status.get(f"{KIND_JA[it['kind']]}・{lab}", 0) + 1
    led_status: dict[str, int] = {}
    for m in meetings:
        for it in m["items"]:
            r = led.get(f"{m['sm']['id']}:{it['id']}")
            if r:
                led_status[r["status"]] = led_status.get(r["status"], 0) + 1

    out = ["# Dartmouth 全アイテム（エクスポート）", "",
           f"- 生成日時: {(now or datetime.now().astimezone()).isoformat(timespec='seconds')}",
           f"- 会議数: {len(meetings)}（アーカイブ済み {sum(1 for m in meetings if m['sm']['archived'])}・"
           f"アイテムのある会議 {sum(1 for m in meetings if m['items'])}）",
           f"- アイテム総数: {len(all_items)}（決定事項 {sum(1 for i in all_items if i['kind'] == 'decision')}・"
           f"未決事項 {sum(1 for i in all_items if i['kind'] == 'question')}・ToDo {sum(1 for i in all_items if i['kind'] == 'todo')}）",
           f"- 開いている: {open_n}（未解決の未決事項と未完了の ToDo）",
           "- 状態別（会議の記録の値）: " + "、".join(f"{k} {v}" for k, v in sorted(by_status.items())),
           ]
    if led:
        out.append("- 台帳での状態別: " + "、".join(f"{k}（{STATUS_LABEL.get(k, k)}） {v}" for k, v in sorted(led_status.items())))
    out += ["",
            "> 読み方: アイテムの ID は会議ごとの番号（D=決定事項、Q=未決事項、T=ToDo）。会議をまたいで一意な ID は `会議ID:番号`。"
            "タイトルは本文の1行目（80字まで）で、本文は全文。ボール＝次に誰が動く番か（本人／AI／外部待ち／依存待ち／保留）。"
            "「台帳」の行は ledger.sqlite にある分類（根拠・分かればよいこと）で、会議の記録とは別に付けたもの。"]

    for m in meetings:
        sm, items = m["sm"], m["items"]
        date = (sm["started"] or "")[:10]
        n_open = sum(1 for it in items if it["kind"] != "decision" and not it["done"])
        out += ["", f"## {sm['id']}「{sm['name']}」（{date}）", "",
                f"- アーカイブ: {'済み（' + str(sm['archived']) + '）' if sm['archived'] else 'なし'}",
                f"- 会議の状態: {sm['state']}",
                f"- 開始: {sm['started'] or '—'} / 最終更新: {sm['updated'] or '—'}",
                f"- アイテム: {len(items)} 件（開いている {n_open} 件）"]
        for it in items:
            uid = f"{sm['id']}:{it['id']}"
            r = led.get(uid)
            lines = ["", f"### [{it['id']}] {_title(it['text'])}", "",
                     f"- ID: {it['id']}（{uid}）",
                     f"- 種別: {KIND_JA[it['kind']]}（{it['kind']}）"
                     + (f" / 台帳の分類: {KIND_LABEL.get(r['kind'], r['kind'])}（{r['kind']}）" if r and r["kind"] != it["kind"] else ""),
                     f"- 状態: {STATUS_JA[(it['kind'], it['done'])]}（done={str(it['done']).lower()}）"
                     + (f" / 台帳: {STATUS_LABEL.get(r['status'], r['status'])}（{r['status']}）" if r else "")]
            if r and r.get("ball_now") and r["ball_now"] != "none":
                lines.append(f"- ボール: {BALL_LABEL[r['ball_now']]}" + ("（手で設定）" if r.get("ball") else "")
                             + (f" ← 待ち: {'、'.join(r['blocked_by'])}" if r.get("blocked_by") else ""))
            elif not r and owner_label(it.get("owner")):
                lines.append(f"- ボール: {owner_label(it.get('owner'))}")
            if it.get("owner"):
                lines.append(f"- 担当（記録の値）: {it['owner']}")
            if it.get("topic_title"):
                lines.append(f"- 議題: {it.get('topic')}. {it['topic_title']}")
            if it.get("approved_at"):
                lines.append(f"- 承認: {it['approved_at']}")
            if it.get("status_ts"):
                lines.append(f"- 状態の更新: {it['status_ts']}")
            if it.get("note"):
                lines.append(f"- メモ: {_one(it['note'])}")
            if r and r.get("rationale"):
                lines.append(f"- 根拠（台帳）: {_one(r['rationale'])}")
            if r and r.get("needs"):
                lab = {"unknown": "分かればよいこと", "discussion": "決める相手・内容"}.get(r["kind"], "完了条件")
                lines.append(f"- {lab}（台帳）: {_one(r['needs'])}")
            if r and r.get("status_note"):
                lines.append(f"- 台帳のメモ: {_one(r['status_note'])}")
            lines += ["", "本文:", "", *[f"> {ln}" if ln else ">" for ln in (it["text"] or "").splitlines()]]
            out += lines
    return "\n".join(out) + "\n"
