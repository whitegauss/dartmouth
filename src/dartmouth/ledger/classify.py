"""台帳の項目を AI でまとめて分類する（バッチ）。

会議中の議長のプロンプトは膨らませず、会議のあとに未分類の項目だけを案件ごとに束ねて1回ずつ呼ぶ。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from . import EXECUTABLE, Ledger, now, topic_context

log = logging.getLogger("dartmouth.ledger")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "kind": {"type": "string", "enum": ["decision", "premise", "unknown", "discussion", "todo"]},
                    "rationale": {"type": "string"},
                    "needs": {"type": "string"},
                    "executable": {"type": "string", "enum": list(EXECUTABLE)},
                    "domain": {"type": "string", "enum": ["software", "hardware", "both", "life", "other"]},
                    "milestone": {"type": ["string", "null"]},
                    "requires": {"type": "array", "items": {"type": "string"}},
                    "supersedes": {"type": "array", "items": {"type": "string"}},
                    "defines_milestone": {
                        "type": ["object", "null"],
                        "properties": {"key": {"type": "string"}, "title": {"type": "string"}, "criteria": {"type": "string"}},
                    },
                },
                "required": ["uid", "kind", "rationale", "needs", "executable", "domain", "milestone", "requires", "supersedes"],
            },
        },
    },
    "required": ["items"],
}

SRC_LABEL = {"decision": "決定", "question": "未決", "todo": "ToDo", "premise": "前提"}


def _cut(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def build_prompt(lg: Ledger, project: str, batch: list[dict[str, Any]], template: str,
                 meeting_dirs: dict[str, Path]) -> str:
    p = lg.db.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
    theme = p["theme"] if p else project
    mss = lg.milestones(theme)
    ms_text = "\n".join(f"- {m['key']}: {m['title']}" + (f"（達成条件: {m['criteria']}）" if m["criteria"] else "")
                        + ("［提案中］" if m["status"] == "proposed" else "") for m in mss) \
        or "（まだありません。マイルストーンを定めている決定があれば defines_milestone で提案する）"
    # 議題の記録: 今回の項目が出た議題だけ
    topics_text = []
    seen: set[tuple[str, int]] = set()
    ctx_cache: dict[str, dict[int, dict[str, Any]]] = {}
    for it in batch:
        key = (it["meeting"], it["topic"])
        if it["topic"] is None or key in seen or it["meeting"] not in meeting_dirs:
            continue
        seen.add(key)
        if it["meeting"] not in ctx_cache:
            try:
                ctx_cache[it["meeting"]] = topic_context(meeting_dirs[it["meeting"]])
            except Exception:  # noqa: BLE001
                ctx_cache[it["meeting"]] = {}
        t = ctx_cache[it["meeting"]].get(it["topic"])
        if not t:
            continue
        lines = [f"## {it['meeting']} 議題{it['topic']}「{t['title']}」", _cut(t["summary"], 600)]
        if t["agreements"]:
            lines += ["合意点:"] + [f"- {_cut(a, 200)}" for a in t["agreements"][:6]]
        topics_text.append("\n".join(lines))
    batch_uids = {it["uid"] for it in batch}
    existing = [x for x in lg.items(project=project, status=None) if x["uid"] not in batch_uids and x["status"] != "dropped"][:80]
    ex_text = "\n".join(f"- {x['uid']} [{SRC_LABEL.get(x['kind'], x['kind'])}・{x['status']}] {_cut(x['text'], 90)}" for x in existing) or "（なし）"
    items_text = "\n".join(f"- uid: {it['uid']} ／ 会議の分類: {SRC_LABEL.get(it['src_kind'], it['src_kind'])} ／ "
                           f"議題: {it['topic_title'] or '-'}\n  {it['text']}" for it in batch)
    return (template.replace("$project", p["name"] if p else project).replace("$theme", theme)
            .replace("$milestones", ms_text).replace("$topics", "\n\n".join(topics_text) or "（なし）")
            .replace("$existing", ex_text).replace("$items", items_text))


async def classify(lg: Ledger, adapter: Any, template: str, meeting_dirs: dict[str, Path], *,
                   project: str | None = None, batch_size: int = 20, max_batches: int = 10,
                   reclassify: bool = False, progress: Callable[[str], None] | None = None) -> dict[str, int]:
    """未分類（reclassify なら全件）の項目を案件ごとに batch_size 件ずつ分類する。"""
    from ..adapters.base import TurnContext
    from ..engine.flow import extract_json

    where = "archived=0" + ("" if reclassify else " AND classified_at IS NULL")
    args: list[Any] = []
    if project:
        where += " AND project=?"
        args.append(project)
    rows = lg.q(f"SELECT * FROM items WHERE {where} ORDER BY project, meeting_date, meeting, local_id", args)
    by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_project[r["project"]].append(r)
    done = batches = failed = proposed = 0
    for pid, its in by_project.items():
        theme_row = lg.db.execute("SELECT theme FROM projects WHERE id=?", (pid,)).fetchone()
        theme = theme_row["theme"] if theme_row else pid
        for i in range(0, len(its), batch_size):
            if batches >= max_batches:
                return {"classified": done, "batches": batches, "failed": failed, "proposed_milestones": proposed,
                        "remaining": len(rows) - done}
            batch = its[i : i + batch_size]
            batches += 1
            prompt = build_prompt(lg, pid, batch, template, meeting_dirs)
            if progress:
                progress(f"{pid}: {len(batch)} 件を分類中（{batches} 回目）")
            ctx = TurnContext(prompt=prompt, participant=getattr(adapter, "name", "ledger"), role="chair", round="ledger",
                              topic=None, meeting_id="ledger", response_schema=SCHEMA)
            try:
                reply = await adapter.respond(ctx)
                data = reply.structured if reply.structured is not None else extract_json(reply.text)
                results = {c["uid"]: c for c in data.get("items", []) if isinstance(c, dict) and c.get("uid")}
            except Exception as e:  # noqa: BLE001
                log.warning("ledger classify failed for %s: %s", pid, e)
                failed += len(batch)
                if progress:
                    progress(f"{pid}: 失敗しました（{e}）")
                continue
            classifier = str((getattr(adapter, "options", {}) or {}).get("model") or getattr(adapter, "name", "ai"))
            with lg.tx():
                for it in batch:
                    c = results.get(it["uid"])
                    if not c:
                        failed += 1
                        continue
                    dm = c.get("defines_milestone")
                    if isinstance(dm, dict) and dm.get("key") and dm.get("title"):
                        if lg.db.execute("SELECT 1 FROM milestones WHERE theme=? AND key=?", (theme, dm["key"])).fetchone() is None:
                            r = lg.db.execute("SELECT COALESCE(MAX(ord), 0) + 10 FROM milestones WHERE theme=?", (theme,)).fetchone()
                            lg.db.execute("INSERT INTO milestones(theme, ord, key, title, criteria, status, source)"
                                          " VALUES(?,?,?,?,?, 'proposed', ?)",
                                          (theme, r[0], dm["key"], dm["title"], dm.get("criteria", ""), it["uid"]))
                            proposed += 1
                    lg.apply_classification(it["uid"], c, classifier, theme)
                    done += 1
    lg.db.execute("INSERT OR REPLACE INTO meta VALUES('classified_at', ?)", (now(),))
    lg.db.commit()
    return {"classified": done, "batches": batches, "failed": failed, "proposed_milestones": proposed, "remaining": 0}


def meeting_dir_map(meeting_dirs: list[Path]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for base in meeting_dirs:
        if not base.is_dir():
            continue
        for d in ([base] if (base / "transcript.jsonl").exists() else base.iterdir()):
            if (d / "transcript.jsonl").exists():
                out.setdefault(d.name, d)
    return out


def chair_adapter(config_path: str, agents_path: str | None = None, fallback_agents: str | None = None,
                  chair: str | None = None, effort: str | None = None):
    """設定ファイル（議題ファイルなら「ファイル#案件id」）の議長と同じアダプタを作る。chair / effort で議長を上書きできる。"""
    from ..adapters import create_adapter
    from ..agenda import split_ref
    from ..config import apply_chair_override, load_config

    f, _ = split_ref(config_path)
    if not agents_path:
        import yaml

        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict) and "participants" not in raw and "extends" not in raw:
            for cand in (f.parent / "agents.yaml", Path("agents.yaml")):
                if cand.exists():
                    agents_path = str(cand)
                    break
            else:
                agents_path = fallback_agents
    cfg = apply_chair_override(load_config(config_path, agents_path=agents_path), chair=chair, effort=effort)
    ch = cfg.participants[cfg.chair]
    opts = ch.options()
    opts["tools_rounds"] = []
    opts.pop("cwd", None)
    return create_adapter(cfg.chair, ch.adapter, ch.role, max(ch.timeout_s, 300), opts)
