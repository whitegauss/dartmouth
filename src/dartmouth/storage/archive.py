"""終わった会議・中断した会議を読むための処理（表示に依存しない）。

- 会議の要約（状態、決定事項・未決事項・ToDo、総括）
- 項目の状態（ToDo の完了、未決事項の解決など）。会議ディレクトリの item_status.json に保存する。
  transcript.jsonl は会議の記録なので書き換えない
- 使用量（参加者ごとの呼び出し回数・時間・トークン・費用）
- 全会議を横断した検索
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..engine.flow import PHASE_LABEL, stats_from_records
from .minutes import Minutes, Proposal
from .transcript import read_transcript

ITEM_STATUS = "item_status.json"
ARCHIVED = "archived"  # このファイルがある会議はアーカイブ（一覧と「決定・未決・ToDo」から隠す。記録と集計は残す）


def _locked(d: Path) -> bool:
    from .lock import is_locked

    return is_locked(d)


def is_archived(d: Path) -> str | None:
    """アーカイブした理由（ファイルの中身）を返す。アーカイブしていなければ None。"""
    p = d / ARCHIVED
    return p.read_text(encoding="utf-8").strip() or "（理由なし）" if p.exists() else None


def set_archived(d: Path, archived: bool, reason: str = "") -> None:
    p = d / ARCHIVED
    if archived:
        p.write_text(f"{datetime.now().strftime('%Y-%m-%d')} {reason}".strip() + "\n", encoding="utf-8")
    else:
        p.unlink(missing_ok=True)
KINDS = {"D": "decision", "Q": "question", "T": "todo"}


def records(d: Path) -> list[dict[str, Any]]:
    return list(read_transcript(d / "transcript.jsonl"))


def minutes_from(recs: Iterable[dict[str, Any]], name: str) -> Minutes:
    m = Minutes(name)
    for r in recs:
        ev = r.get("event")
        if ev == "approved":
            m.apply(r["topic"], r.get("title", ""), Proposal.model_validate(r["proposal"]), r["ts"])
        elif ev == "pending":
            m.pending[r["topic"]] = (r.get("title", ""), Proposal.model_validate(r["proposal"]))
        elif ev == "final":
            m.final_summary = r.get("summary", "")
    return m


def _handovers(recs: Iterable[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """議題ごとの最新の申し送り（議題番号 → title, status）。"""
    out: dict[str, dict[str, str]] = {}
    for r in recs:
        if r.get("event") == "handover" and r.get("topic") is not None:
            out[str(r["topic"])] = {"title": r.get("title", ""), "status": r.get("status", "")}
    return out


def meeting_name(recs: list[dict[str, Any]], default: str) -> str:
    for r in recs:
        if r.get("event") == "meeting_start" and "「" in (r.get("text") or ""):
            return r["text"].split("「", 1)[1].split("」", 1)[0]
    return default


def meeting_state(recs: list[dict[str, Any]]) -> tuple[str | None, str]:
    cps = [r for r in recs if r.get("event") == "checkpoint"]
    phase = cps[-1]["state"]["phase"] if cps else None
    last_ev = next((r.get("event") for r in reversed(recs) if r.get("event") in ("interrupted", "aborted", "checkpoint", "resumed")), None)
    if phase == "done":
        return phase, "終了"
    if phase == "approval":
        return phase, "承認待ちで中断"
    if last_ev in ("interrupted", "aborted"):
        return phase, "中断"
    return phase, "中断（記録の途中）"


def load_item_status(d: Path) -> dict[str, dict[str, Any]]:
    p = d / ITEM_STATUS
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_item_status(d: Path, item_id: str, done: bool, note: str = "") -> dict[str, Any]:
    data = load_item_status(d)
    entry = {"done": done, "note": note, "ts": datetime.now().astimezone().isoformat(timespec="seconds")}
    data[item_id] = entry
    tmp = d / (ITEM_STATUS + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / ITEM_STATUS)
    if (d / "minutes.md").exists():
        try:
            from ..session import write_digest

            write_digest(d)  # 済んだ項目を digest から外す
        except Exception:  # noqa: BLE001
            pass
    return entry


def summarize(d: Path, recs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    recs = records(d) if recs is None else recs
    phase, state = meeting_state(recs)
    m = minutes_from(recs, d.name)
    status = load_item_status(d)

    def st(item_id: str) -> dict[str, Any]:
        s = status.get(item_id) or {}
        return {"done": bool(s.get("done")), "note": s.get("note", ""), "status_ts": s.get("ts")}

    running = _locked(d)
    if running and phase not in (None, "done"):
        state = "進行中"
    return {
        "id": d.name,
        "name": meeting_name(recs, d.name),
        "state": state,
        "phase": phase,
        "phase_label": PHASE_LABEL.get(phase, "") if phase else "",
        "running": running,
        "resumable": phase not in (None, "done") and not running,
        "extendable": phase == "done" and not running,
        "handovers": _handovers(recs),
        "archived": is_archived(d),
        "started": recs[0]["ts"] if recs else None,
        "updated": recs[-1]["ts"] if recs else None,
        "topics": [{"no": n, "title": s.title} for n, s in sorted(m.topics.items())],
        "decisions": [{"id": x.id, "text": x.text, "topic": x.topic, "approved_at": x.approved_at, **st(x.id)} for x in m.decisions],
        "open_questions": [{"id": q.id, "text": q.text, "owner": q.owner, "topic": q.topic, **st(q.id)} for q in m.questions],
        "todos": [{"id": t.id, "text": t.text, "owner": t.owner, "topic": t.topic, **st(t.id)} for t in m.todos],
        "pending": [{"topic": k, "title": v[0], "decisions": v[1].decisions} for k, v in sorted(m.pending.items())],
        "final_summary": m.final_summary,
        "minutes_compact": m.render_compact(),
        "minutes_md": m.render(),
    }


def participants_info(d: Path) -> dict[str, dict[str, Any]]:
    """会議で使った設定から、参加者の表示名・アダプタ・モデル・役割を取る。"""
    try:
        cfg = yaml.safe_load((d / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    out = {}
    for n, p in (cfg.get("participants") or {}).items():
        if not isinstance(p, dict):
            continue
        out[n] = {"display_name": p.get("display_name") or n.capitalize(), "adapter": p.get("adapter"),
                  "model": p.get("model"), "role": p.get("role", "member")}
    # 議長が議題ごとに決めた席（設定ファイルには無い参加者）
    for r in read_transcript(d / "transcript.jsonl"):
        if r.get("event") == "seating":
            for s in r.get("seats", []):
                out[s["persona"]] = {"display_name": s.get("name", s["persona"]), "adapter": s.get("adapter"),
                                     "model": s.get("model"), "role": s.get("role", "member")}
    return out


def usage(d: Path, recs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    recs = records(d) if recs is None else recs
    stats = stats_from_records(recs)
    info = participants_info(d)
    cost: dict[str, float] = {}
    prompt_chars: dict[str, int] = {}
    for r in recs:
        if r.get("call"):
            if r.get("cost_usd"):
                cost[r["speaker"]] = cost.get(r["speaker"], 0.0) + float(r["cost_usd"])
            if r.get("prompt_chars"):
                prompt_chars[r["speaker"]] = prompt_chars.get(r["speaker"], 0) + int(r["prompt_chars"])
    per = []
    for n, s in stats.items():
        i = info.get(n, {})
        per.append({
            "participant": n,
            "display_name": i.get("display_name", n),
            "model": i.get("model"),
            "adapter": i.get("adapter"),
            "role": i.get("role"),
            "calls": s["calls"],
            "ok": s.get("ok", 0),
            "timeout": s.get("timeout", 0),
            "failed": s.get("failed", 0),
            "skipped": s.get("skipped", 0),
            "elapsed_s": round(s["elapsed_ms"] / 1000, 1),
            "avg_s": round(s["elapsed_ms"] / 1000 / s["calls"], 1) if s["calls"] else 0,
            "chars": s["chars"],
            "tokens": s["tokens"],
            "cost_usd": round(cost.get(n, 0.0), 4),
            "prompt_chars": prompt_chars.get(n, 0),
        })
    per.sort(key=lambda x: x["participant"])
    totals = {k: sum(p[k] for p in per) for k in ("calls", "ok", "timeout", "failed", "tokens", "chars", "prompt_chars")}
    totals["elapsed_s"] = round(sum(p["elapsed_s"] for p in per), 1)
    totals["cost_usd"] = round(sum(p["cost_usd"] for p in per), 4)
    return {"id": d.name, "name": meeting_name(recs, d.name), "started": recs[0]["ts"] if recs else None,
            "participants": per, "totals": totals}


def search(dirs: Iterable[Path], q: str, limit: int = 100) -> list[dict[str, Any]]:
    q = q.strip().lower()
    if not q:
        return []
    hits: list[dict[str, Any]] = []
    for base in dirs:
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir(), reverse=True):
            if not (d / "transcript.jsonl").exists():
                continue
            recs = records(d)
            info = participants_info(d)
            name = meeting_name(recs, d.name)
            for r in recs:
                if r.get("kind") != "message":
                    continue
                text = r.get("text") or ""
                i = text.lower().find(q)
                if i == -1:
                    continue
                spk = r.get("speaker") or ""
                hits.append({
                    "meeting": d.name, "meeting_name": name, "seq": r.get("seq"), "topic": r.get("topic"),
                    "round": r.get("round"), "speaker": spk,
                    "speaker_name": "ユーザー" if spk == "user" else info.get(spk, {}).get("display_name", spk),
                    "snippet": ("…" if i > 60 else "") + text[max(0, i - 60): i + len(q) + 80] + ("…" if i + len(q) + 80 < len(text) else ""),
                    "ts": r.get("ts"),
                })
                if len(hits) >= limit:
                    return hits
    return hits


# ---------------------------------------------------------------------- 集計（dm stats）

STAT_COLUMNS = [
    "meeting", "participant", "display_name", "model", "round", "calls", "ok", "timeout", "failed", "skipped",
    "elapsed_s", "avg_s", "turns_total", "turns_max", "input", "cache_creation", "cache_read", "output", "thinking",
    "tokens_total", "tokens_excl_cache_read", "prompt_chars_avg", "context_ratio", "retried_ok", "errors",
]
CONTEXT_PARTS = ("background", "materials", "minutes")  # 資料由来（前提資料・関連資料・議事録）


def classify_error(text: str) -> str:
    t = text or ""
    tl = t.lower()
    if "hit your" in t and "limit" in t:
        return "利用上限"
    if "回数上限（429）" in t:
        return "回数上限"
    if any(k in tl for k in ("quota", "rate limit", "resource_exhausted", "too many requests", "exhausted")):
        return "利用上限"
    if "denied" in t:
        return "権限の拒否で空の応答"
    if "秒以内に応答がありません" in t:
        return "タイムアウト"
    if "/stop" in t:
        return "中断"
    if "empty" in t:
        return "空の応答"
    return "その他"


def stat_rows(d: Path, recs: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """参加者×ラウンドごとの集計行と、会議全体の集計を返す。

    トークンの内訳は記録に usage がある呼び出しだけ（古い会議は tokens_total だけ）。
    tokens_excl_cache_read は課金・クォータに近い値（cache read を除く）。
    """
    recs = records(d) if recs is None else recs
    info = participants_info(d)
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for r in recs:
        if not r.get("call"):
            continue
        spk, rnd = r["speaker"], r["round"]
        i = info.get(spk, {})
        row = rows.setdefault((spk, rnd), {c: 0 for c in STAT_COLUMNS} | {
            "meeting": d.name, "participant": spk, "display_name": i.get("display_name", spk), "model": i.get("model") or "",
            "round": rnd, "errors": {}, "_prompt": [], "_usage_calls": 0, "_ctx": 0, "_pp": 0})
        row["calls"] += 1
        row[r["status"]] = row.get(r["status"], 0) + 1
        row["elapsed_s"] += (r.get("elapsed_ms") or 0) / 1000
        if r.get("kind") == "error":
            k = classify_error(r.get("text", ""))
            row["errors"][k] = row["errors"].get(k, 0) + 1
            continue
        turns = r.get("turns") or 0
        row["turns_total"] += turns
        row["turns_max"] = max(row["turns_max"], turns)
        u = r.get("usage") or {}
        if u:
            row["_usage_calls"] += 1
        inp = int(u.get("input_tokens") or 0)
        cc = int(u.get("cache_creation_input_tokens") or 0)
        cr = int(u.get("cache_read_input_tokens") or u.get("cache_read_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        th = int(u.get("thinking_tokens") or 0)
        row["input"] += inp
        row["cache_creation"] += cc
        row["cache_read"] += cr
        row["output"] += out
        row["thinking"] += th
        tot = int(r.get("tokens") or 0)
        row["tokens_total"] += tot
        # Claude の tokens は cache read を含む（合計から引く）。agy の total_tokens は cache read を含まない（そのまま）。
        # usage が無い古い記録は分けられないので総量をそのまま入れる
        includes_cr = "cache_read_input_tokens" in u
        row["tokens_excl_cache_read"] += (tot - cr) if includes_cr else tot
        if r.get("prompt_chars"):
            row["_prompt"].append(int(r["prompt_chars"]))
        pp = r.get("prompt_parts") or {}
        if pp:
            row["_ctx"] += sum(int(pp.get(k, 0)) for k in CONTEXT_PARTS)
            row["_pp"] += sum(int(v) for v in pp.values())
        if (r.get("attempt") or 1) > 1:
            row["retried_ok"] += 1
    out_rows = []
    for row in rows.values():
        ok_calls = row["calls"] - row.get("timeout", 0) - row.get("failed", 0) - row.get("skipped", 0)
        row["avg_s"] = round(row["elapsed_s"] / row["calls"], 1) if row["calls"] else 0
        row["elapsed_s"] = round(row["elapsed_s"], 1)
        row["prompt_chars_avg"] = round(sum(row["_prompt"]) / len(row["_prompt"])) if row["_prompt"] else ""
        if not row["_usage_calls"] and ok_calls:
            for k in ("input", "cache_creation", "cache_read", "output", "thinking"):
                row[k] = ""  # 記録なし
        row["errors"] = "; ".join(f"{k}×{v}" for k, v in row["errors"].items())
        row["context_ratio"] = round(row["_ctx"] / row["_pp"], 2) if row["_pp"] else ""
        for k in ("_prompt", "_usage_calls", "_ctx", "_pp"):
            row.pop(k)
        out_rows.append(row)
    order = {"intro": 0, "independent": 1, "critique": 2, "summary": 3, "interjection": 4}
    out_rows.sort(key=lambda x: (x["participant"], order.get(x["round"], 9)))
    chair = next((n for n, i in info.items() if i.get("role") == "chair"), None)
    total = {
        "meeting": d.name,
        "name": meeting_name(recs, d.name),
        "calls": sum(x["calls"] for x in out_rows),
        "elapsed_s": round(sum(x["elapsed_s"] for x in out_rows), 1),
        "wall_s": _wall_seconds(recs),
        "tokens_total": sum(int(x["tokens_total"] or 0) for x in out_rows),
        "tokens_excl_cache_read": sum(int(x["tokens_excl_cache_read"] or 0) for x in out_rows),
        "failures": sum(x.get("timeout", 0) + x.get("failed", 0) for x in out_rows),
        "rejects": sum(1 for r in recs if r.get("event") == "rejected"),
        "chair_summary_s": round(sum(x["elapsed_s"] for x in out_rows if x["participant"] == chair and x["round"] == "summary"), 1),
        "discussants_s": round(sum(x["elapsed_s"] for x in out_rows if x["participant"] != chair), 1),
        "intro_model_calls": sum(x["calls"] for x in out_rows if x["round"] == "intro"),
        "auto_approved": sum(1 for r in recs if r.get("event") == "approved" and r.get("auto")),
        "seat_fallbacks": sum(int(r.get("fallbacks") or 0) for r in recs if r.get("event") == "seating"),
        "format_violations": sum(1 for r in recs if r.get("format_violation")),
        "summary_retries": sum(1 for r in recs if r.get("event") == "summary_retry"),
        "kb_duplicates": sum(1 for r in recs if r.get("event") == "kb_duplicate"),
        "retried_ok": sum(int(x["retried_ok"] or 0) for x in out_rows),
        "empty_responses": sum(1 for r in recs if r.get("call") and r.get("kind") == "error" and "空" in classify_error(r.get("text", ""))),
    }
    ctx = sum(sum(int((r.get("prompt_parts") or {}).get(k, 0)) for k in CONTEXT_PARTS) for r in recs if r.get("call"))
    allp = sum(sum(int(v) for v in (r.get("prompt_parts") or {}).values()) for r in recs if r.get("call"))
    total["context_ratio"] = round(ctx / allp, 2) if allp else None
    return out_rows, total


def _wall_seconds(recs: list[dict[str, Any]]) -> float | None:
    """会議の開始から最後の記録までの実時間（中断していた時間も含む）。"""
    try:
        t0 = datetime.fromisoformat(recs[0]["ts"])
        t1 = datetime.fromisoformat(recs[-1]["ts"])
        return round((t1 - t0).total_seconds(), 1)
    except (IndexError, KeyError, ValueError):
        return None


# ---------------------------------------------------------------------- 5時間枠（Claude の利用枠）

def window_usage(dirs: Iterable[Path], hours: float = 5, now: datetime | None = None) -> dict[str, Any]:
    """直近 hours 時間の呼び出しを、経路の種類（adapter）ごとに合計する。全会議をまたいで数える。

    Claude の利用枠（5時間制限）の目安にする。トークンは cache read を除いた値。
    """
    now = now or datetime.now().astimezone()
    since = now.timestamp() - hours * 3600
    by: dict[str, dict[str, Any]] = {}
    hits: list[str] = []
    limit_adapters: dict[str, str] = {}
    limit_backends: dict[str, str] = {}
    for base in dirs:
        if not Path(base).is_dir():
            continue
        for d in Path(base).iterdir():
            tp = d / "transcript.jsonl"
            if not tp.exists() or tp.stat().st_mtime < since:
                continue
            info = participants_info(d)
            for r in read_transcript(tp):
                if not r.get("call"):
                    continue
                try:
                    ts = datetime.fromisoformat(r["ts"]).timestamp()
                except (KeyError, ValueError):
                    continue
                if ts < since:
                    continue
                adapter = (info.get(r["speaker"]) or {}).get("adapter") or "unknown"
                a = by.setdefault(adapter, {"calls": 0, "tokens_excl_cache_read": 0, "tokens_total": 0})
                a["calls"] += 1
                u = r.get("usage") or {}
                tot = int(r.get("tokens") or 0)
                cr = int(u.get("cache_read_input_tokens") or 0)
                a["tokens_total"] += tot
                a["tokens_excl_cache_read"] += tot - cr if "cache_read_input_tokens" in u else tot
                if r.get("kind") == "error" and classify_error(r.get("text", "")) in ("利用上限", "回数上限"):
                    hits.append(r["text"])
                    limit_adapters[adapter] = r["text"]
                    if r.get("backend"):
                        limit_backends[str(r["backend"])] = r["text"]
    claude = by.get("claude_code", {"calls": 0, "tokens_excl_cache_read": 0, "tokens_total": 0})
    return {"hours": hours, "by_adapter": by, "claude_tokens": claude["tokens_excl_cache_read"],
            "claude_calls": claude["calls"], "limit_hits": len(hits), "last_limit_message": hits[-1] if hits else None,
            "limit_adapters": limit_adapters, "limit_backends": limit_backends}


# ---------------------------------------------------------------------- ペルソナ×モデルの成績

def _scorer_key(model: str, adapter: str) -> str:
    ml = (model or "").lower()
    if "argon" in ml:
        return "argon"
    if "opus" in ml or adapter == "claude_code":
        return "opus"
    return model or adapter or "other"


def seat_history(dirs: Iterable[Path]) -> dict[str, dict[str, Any]]:
    """全会議の「ペルソナ×経路（モデル）」ごとの、座った回数・議長の採点の平均・1回あたりのトークン。

    キーは "persona|backend"。議長の席決めの材料と、得意不得意の一覧に使う。
    採点は全体（avg_score）に加えて、議長別（Opus / Argon）も集計する。
    """
    out: dict[str, dict[str, Any]] = {}
    for base in dirs:
        if not Path(base).is_dir():
            continue
        for d in Path(base).iterdir():
            tp = d / "transcript.jsonl"
            if not tp.exists():
                continue
            info = participants_info(d)
            chair_info = next((v for v in info.values() if v.get("role") == "chair"), {})
            def_model = str(chair_info.get("model") or "")
            def_adapter = str(chair_info.get("adapter") or "")
            recs = list(read_transcript(tp))
            # 同じ議題の席決め・採点が複数あるとき（中断と再開など）は、最後のものだけ数える
            last_seat = {r.get("topic"): i for i, r in enumerate(recs) if r.get("event") == "seating"}
            last_score = {r.get("topic"): i for i, r in enumerate(recs) if r.get("event") == "seat_scores"}
            for i, r in enumerate(recs):
                ev = r.get("event")
                if (ev == "seating" and last_seat.get(r.get("topic")) != i) or (ev == "seat_scores" and last_score.get(r.get("topic")) != i):
                    continue
                if ev == "seating":
                    for st in r.get("seats", []):
                        k = f"{st['persona']}|{st['backend']}"
                        h = out.setdefault(k, {"persona": st["persona"], "name": st.get("name", st["persona"]), "backend": st["backend"],
                                               "model": st.get("model"), "seated": 0, "scores": [], "scores_by_chair": {},
                                               "calls": 0, "tokens": 0})
                        h["seated"] += 1
                elif ev == "seat_scores":
                    scorer = _scorer_key(str(r.get("chair_model") or def_model), str(r.get("chair_adapter") or def_adapter))
                    for sc in r.get("scores", []):
                        k = f"{sc['persona']}|{sc['backend']}"
                        if k in out and isinstance(sc.get("score"), (int, float)):
                            val = float(sc["score"])
                            out[k]["scores"].append(val)
                            out[k]["scores_by_chair"].setdefault(scorer, []).append(val)
                elif r.get("call") and r.get("backend") and r.get("kind") == "message":
                    k = f"{r['speaker']}|{r['backend']}"
                    if k in out:
                        out[k]["calls"] += 1
                        u = r.get("usage") or {}
                        tot = int(r.get("tokens") or 0)
                        out[k]["tokens"] += tot - int(u.get("cache_read_input_tokens") or 0) if "cache_read_input_tokens" in u else tot
    for h in out.values():
        h["avg_score"] = round(sum(h["scores"]) / len(h["scores"]), 2) if h["scores"] else None
        h["rated"] = len(h["scores"])
        by_chair: dict[str, dict[str, Any]] = {}
        for ck, vals in h["scores_by_chair"].items():
            by_chair[ck] = {"avg_score": round(sum(vals) / len(vals), 2), "rated": len(vals)}
        h["by_chair"] = by_chair
        h["opus_avg_score"] = (by_chair.get("opus") or {}).get("avg_score")
        h["opus_rated"] = (by_chair.get("opus") or {}).get("rated", 0)
        h["argon_avg_score"] = (by_chair.get("argon") or {}).get("avg_score")
        h["argon_rated"] = (by_chair.get("argon") or {}).get("rated", 0)
        h["tokens_per_call"] = round(h["tokens"] / h["calls"]) if h["calls"] else None
        del h["scores"]
        del h["scores_by_chair"]
    return out
