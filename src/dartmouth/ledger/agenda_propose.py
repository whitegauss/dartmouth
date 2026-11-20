"""議長に、台帳の未解決事項を束ねて、ちょうどいい大きさの議題にしてもらう。

未解決事項（協議事項・不明点・未分類の未決）が変わらないうちは、前の提案を使い回す（AI を呼ばない）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import KIND_LABEL, Ledger, now

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topics": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "goal": {"type": "string"}, "why": {"type": "string"},
                           "item_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "goal", "item_ids"]}},
        "skipped": {"type": "array", "items": {
            "type": "object", "properties": {"uid": {"type": "string"}, "reason": {"type": "string"}}, "required": ["uid", "reason"]}},
    },
    "required": ["topics"],
}
MAX_ITEMS = 40  # 議長に渡す未解決事項の上限（重要なものから）

TABLE = """CREATE TABLE IF NOT EXISTS agenda_proposals(
  project TEXT PRIMARY KEY, items_hash TEXT, created_at TEXT, model TEXT, data TEXT)"""


def candidates(lg: Ledger, project: str) -> list[dict[str, Any]]:
    """案件の未解決事項を、重要な順に（止めている数・現在地のマイルストーン・協議事項）。保留は除く。"""
    pid = lg.resolve_project(project)
    p = lg.db.execute("SELECT theme FROM projects WHERE id=?", (pid,)).fetchone()
    current = None
    if p:
        row = next((r for r in lg.matrix(p["theme"])["rows"] if r["project"]["id"] == pid), None)
        current = row["current"] if row else None
    out = []
    for it in lg.items(project=pid, kind="discussion,unknown,question", status="open"):
        if it["status"] != "open":
            continue
        enables = [e for e in lg.item(it["uid"])["enables"] if e["status"] in ("open", "held")]
        score = len(enables) * 2 + (2 if current and it["milestone"] == current else 0) + (1 if it["kind"] == "discussion" else 0)
        out.append({"uid": it["uid"], "kind": it["kind"], "text": it["text"], "needs": it["needs"], "owner": it["owner"],
                    "milestone": it["milestone"], "milestone_key": it["milestone_key"], "meeting": it["meeting"],
                    "topic_title": it["topic_title"], "unlocks": len(enables), "score": score})
    out.sort(key=lambda x: x["meeting"], reverse=True)  # 同じ点数なら新しい会議のものから
    out.sort(key=lambda x: -x["score"])
    return out


def items_hash(items: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(sorted(i["uid"] for i in items)).encode()).hexdigest()[:16]


def cached(lg: Ledger, project: str) -> dict[str, Any] | None:
    """前の提案。未解決事項が変わっていれば None。"""
    lg.db.execute(TABLE)
    r = lg.db.execute("SELECT * FROM agenda_proposals WHERE project=?", (lg.resolve_project(project),)).fetchone()
    if not r:
        return None
    if r["items_hash"] != items_hash(candidates(lg, project)[:MAX_ITEMS]):
        return None
    return {**json.loads(r["data"]), "created_at": r["created_at"], "model": r["model"], "cached": True}


def build_prompt(lg: Ledger, project: str, items: list[dict[str, Any]], template: str, purpose: str = "") -> str:
    pid = lg.resolve_project(project)
    p = lg.db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    cur = "（未設定）"
    if p:
        mx = lg.matrix(p["theme"])
        row = next((r for r in mx["rows"] if r["project"]["id"] == pid), None)
        m = next((m for m in mx["milestones"] if row and m["id"] == row["current"]), None)
        if m:
            cur = f"{m['key']} {m['title']}" + (f"（期限 {m['due']}）" if m.get("due") else "")
    lines = []
    for it in items:
        lines.append(f"- {it['uid']} ／ {KIND_LABEL.get(it['kind'], it['kind'])} ／ 進む{it['unlocks']}件 ／ {it['milestone_key'] or '-'} ／ "
                     f"{' '.join(it['text'].split())} ／ {' '.join((it['needs'] or '').split()) or '-'}")
    n = len(items)
    max_t = 1 if n <= 2 else (2 if n <= 6 else (3 if n <= 12 else 4))
    return (template.replace("$project", p["name"] if p else project).replace("$purpose", purpose or "（なし）")
            .replace("$milestone", cur).replace("$min_topics", "1").replace("$max_topics", str(max_t))
            .replace("$items", "\n".join(lines)))


def premise_of(topic: dict[str, Any], by_uid: dict[str, dict[str, Any]]) -> str:
    """議題の前提: 決めること＋束ねた未解決事項の本文（議長の要約に頼らず、元の文をそのまま入れる）。"""
    out = [f"この議題で決めること: {topic.get('goal', '').strip()}", "", "検討する未解決事項（台帳より）:"]
    for uid in topic.get("item_ids", []):
        it = by_uid.get(uid)
        if not it:
            continue
        line = f"- [{uid}]（{KIND_LABEL.get(it['kind'], it['kind'])}）{' '.join(it['text'].split())}"
        if it.get("needs"):
            line += f" — 分かればよいこと・決める相手: {' '.join(it['needs'].split())}"
        out.append(line)
    if topic.get("why"):
        out += ["", f"議長のメモ: {topic['why'].strip()}"]
    return "\n".join(out)


async def propose(lg: Ledger, adapter: Any, project: str, template: str, purpose: str = "") -> dict[str, Any]:
    from ..adapters.base import TurnContext
    from ..engine.flow import extract_json

    items = candidates(lg, project)[:MAX_ITEMS]
    if not items:
        return {"topics": [], "skipped": [], "items": 0}
    ctx = TurnContext(prompt=build_prompt(lg, project, items, template, purpose), participant=getattr(adapter, "name", "chair"),
                      role="chair", round="agenda", topic=None, meeting_id="agenda", response_schema=SCHEMA)
    reply = await adapter.respond(ctx)
    data = reply.structured if reply.structured is not None else extract_json(reply.text)
    by_uid = {i["uid"]: i for i in items}
    topics = []
    used: set[str] = set()
    for t in data.get("topics") or []:
        ids = [u for u in (t.get("item_ids") or []) if u in by_uid and u not in used]
        if not ids or not str(t.get("title") or "").strip():
            continue
        used.update(ids)
        t = {"title": str(t["title"]).strip(), "goal": str(t.get("goal") or ""), "why": str(t.get("why") or ""), "item_ids": ids}
        t["premise"] = premise_of(t, by_uid)
        t["items"] = [{"uid": u, "kind": by_uid[u]["kind"], "text": by_uid[u]["text"]} for u in ids]
        topics.append(t)
    skipped = [s for s in (data.get("skipped") or []) if isinstance(s, dict) and s.get("uid") in by_uid and s["uid"] not in used]
    result = {"topics": topics, "skipped": skipped, "items": len(items),
              "left_out": [u for u in by_uid if u not in used and u not in {s["uid"] for s in skipped}]}
    model = str((getattr(adapter, "options", {}) or {}).get("model") or getattr(adapter, "name", ""))
    lg.db.execute(TABLE)
    lg.db.execute("INSERT OR REPLACE INTO agenda_proposals VALUES(?,?,?,?,?)",
                  (lg.resolve_project(project), items_hash(items), now(), model, json.dumps(result, ensure_ascii=False)))
    lg.db.commit()
    return {**result, "created_at": now(), "model": model, "cached": False}
