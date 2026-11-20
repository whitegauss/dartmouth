"""会議フローの状態機械。会議の状態を持つのはここだけ。

INTRO → TOPIC → INDEPENDENT → CRITIQUE(×n) → SUMMARY → APPROVAL ─┬→ TOPIC（次の議題）
                                    ↑                           └→ CRITIQUE（却下: 理由つきでやり直し）
TOPIC（議題がもうない）→ FINAL → DONE

各フェーズに入るたびに transcript へ checkpoint（system 行）を書く。
再開時は最後の checkpoint からそのフェーズをやり直す。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from ..adapters.base import Adapter, Reply, TurnContext
from ..config import Config, TopicConfig
from ..persona_lint import TEMPLATE
from ..persona_output import normalize_reply
from ..storage.minutes import PROPOSAL_SCHEMA, Minutes, Proposal
from . import events as E
from .budget import Budget
from .context import ContextBuilder, Message, PromptLibrary

log = logging.getLogger("dartmouth.engine")


class Phase(StrEnum):
    INTRO = "intro"
    TOPIC = "topic"
    INDEPENDENT = "independent"
    CRITIQUE = "critique"
    SUMMARY = "summary"
    APPROVAL = "approval"
    FINAL = "final"
    DONE = "done"


PHASE_LABEL = {
    Phase.INTRO: "自己紹介",
    Phase.TOPIC: "議題の提示",
    Phase.INDEPENDENT: "独立回答",
    Phase.CRITIQUE: "相互批判",
    Phase.SUMMARY: "議長のまとめ",
    Phase.APPROVAL: "承認待ち",
    Phase.FINAL: "総括",
    Phase.DONE: "終了",
}
# checkpoint 行の round 欄に入れる値（仕様の round の語彙に合わせる）
PHASE_ROUND = {
    Phase.INTRO: "intro",
    Phase.TOPIC: "independent",
    Phase.INDEPENDENT: "independent",
    Phase.CRITIQUE: "critique",
    Phase.SUMMARY: "summary",
    Phase.APPROVAL: "approval",
    Phase.FINAL: "summary",
    Phase.DONE: "summary",
}


SEATING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "seats": {"type": "array", "items": {"type": "object", "properties": {
            "persona": {"type": "string"}, "backend": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["persona", "backend", "reason"]}},
    },
    "required": ["seats"],
}


class MeetingAborted(Exception):
    pass


@dataclass
class TopicState:
    no: int  # 1始まり
    title: str
    premise: str
    members: list[str]
    chair: str
    messages: list[Message] = field(default_factory=list)
    critique_done: int = 0
    proposal: dict[str, Any] | None = None
    rejections: list[str] = field(default_factory=list)
    materials: list[dict[str, Any]] = field(default_factory=list)  # 資料庫から渡している抜粋
    kb_requests: int = 0
    seats: list[dict[str, Any]] = field(default_factory=list)  # 議長が決めた席（persona, backend, reason）


@dataclass
class Inflight:
    participant: str
    task: asyncio.Task
    started: float
    round: str


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def extract_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        return json.loads(text[i : j + 1])
    raise ValueError("JSON が見つかりません")


def empty_stats() -> dict[str, Any]:
    return {"calls": 0, "ok": 0, "timeout": 0, "failed": 0, "elapsed_ms": 0, "chars": 0, "tokens": 0}


def stats_from_records(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in records:
        if not r.get("call"):
            continue
        s = out.setdefault(r["speaker"], empty_stats())
        s["calls"] += 1
        s[r["status"]] = s.get(r["status"], 0) + 1
        s["elapsed_ms"] += r.get("elapsed_ms") or 0
        if r.get("kind") == "message":
            s["chars"] += r.get("chars") or 0
            s["tokens"] += r.get("tokens") or 0
    return out


class Engine:
    def __init__(
        self,
        cfg: Config,
        meeting_id: str,
        adapters: dict[str, Adapter],
        *,
        meeting_dir: Path | None = None,
        prompts: PromptLibrary,
        headless: bool = False,
        bus: E.EventBus | None = None,
        kb: Any = None,
        local_llm: Any = None,
        backends: dict[str, Adapter] | None = None,
    ):
        self.cfg = cfg
        self.meeting_id = meeting_id
        self.meeting_dir = meeting_dir
        self.adapters = adapters
        self.headless = headless
        self.bus = bus or E.EventBus()
        self.names = {n: (p.display_name or n.capitalize()) for n, p in cfg.participants.items()}
        self.names["user"] = "ユーザー"
        self.ctx = ContextBuilder(cfg, prompts, self.names)
        self.prompts = prompts
        self.budget = Budget(cfg.budget)
        self.minutes = Minutes(cfg.meeting.name, cfg.meeting.purpose)
        self.topics: list[TopicConfig] = [t.model_copy() for t in cfg.meeting.topics]
        self.phase = Phase.INTRO if cfg.meeting.intro else Phase.TOPIC
        self.topic_idx = 0
        self.cur: TopicState | None = None
        self.absent: set[str] = set()
        self.seq = 0
        self.stats: dict[str, dict[str, Any]] = {}
        self.inflight: dict[int, Inflight] = {}  # キーはタスクごとの通し番号（/ask と通常の発言が重なってもよい）
        self._call_id = 0
        self._stopped: set[asyncio.Task] = set()
        self._pre_topic: list[Message] = []  # 議題が始まる前の割り込み
        self._approval: asyncio.Future | None = None
        self._pause: asyncio.Future | None = None
        self._wake = asyncio.Event()  # /next・/topic・/end で起こす
        self._waiting: str | None = None  # "next" | "topic" | "approval" | "pause"
        self._skip_rest = False
        self._end_requested = False
        self._side_tasks: set[asyncio.Task] = set()
        self._resumed = False
        self._paused_info: dict[str, Any] | None = None
        # 全議題のあとに回す延長の待ち行列（承認されたまとめの申し送り）と、回した回数
        self.extensions: list[dict[str, Any]] = []
        self.ext_count = 0
        self.kb = kb  # 資料庫（knowledge.KnowledgeBase）。なければ資料は渡さない
        self.local_llm = local_llm
        # 議長が席を決めるときの、経路（モデル）ごとのアダプタと、ペルソナの一覧
        self.backends = backends or {}
        self.seat_backend: dict[str, str] = {}
        self.backend_stats: dict[str, dict[str, Any]] = {}
        self.exhausted: dict[str, str] = {}  # 利用枠に当たった経路 → エラーの文（戻る時刻を含む）
        self.persona_lib: dict[str, tuple[dict[str, Any], str]] = {}
        self.window: dict[str, Any] = {}
        self.seat_hist: dict[str, dict[str, Any]] = {}
        if cfg.seating:
            from ..config import parse_persona

            pdir = Path(cfg.seating.personas_dir) if cfg.seating.personas_dir else prompts.root / "personas"
            for pid in cfg.seating.personas:
                f = pdir / f"{pid}.md"
                if f.exists():
                    self.persona_lib[pid] = parse_persona(f)

    # ------------------------------------------------------------------ 記録

    def _record(
        self,
        round_: str | None,
        speaker: str,
        kind: str,
        text: str,
        *,
        status: str = "ok",
        topic: int | None | str = "cur",
        elapsed_ms: int | None = None,
        tokens: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        if topic == "cur":
            topic = self.cur.no if self.cur else None
        self.seq += 1
        rec = {
            "ts": now_iso(),
            "meeting": self.meeting_id,
            "topic": topic,
            "round": round_,
            "speaker": speaker,
            "kind": kind,
            "text": text,
            "elapsed_ms": elapsed_ms,
            "chars": len(text) if kind == "message" else None,
            "tokens": tokens,
            "status": status,
            "seq": self.seq,
            **extra,
        }
        if extra.get("call") and speaker in self.seat_backend:
            b = self.seat_backend[speaker]
            rec["backend"] = b
            rec["model"] = (self.cfg.seating.backends[b].options().get("model") if self.cfg.seating else None)
        if extra.get("call") and kind == "error":
            from ..storage.archive import classify_error

            if classify_error(text) in ("利用上限", "回数上限"):
                # 利用上限・回数上限に当たった。Claude は同じアカウントの全経路、それ以外は該当バックエンドを席から外す
                adapter = self.cfg.participants[speaker].adapter if speaker in self.cfg.participants else ""
                if adapter == "claude_code" and self.cfg.seating:
                    for bid, b in self.cfg.seating.backends.items():
                        if b.adapter == "claude_code" and bid not in self.exhausted:
                            self.exhausted[bid] = text
                elif speaker in self.seat_backend and self.seat_backend[speaker] not in self.exhausted:
                    self.exhausted[self.seat_backend[speaker]] = text
        if extra.get("call") and speaker in self.seat_backend:
            b = self.seat_backend[speaker]
            rec_backend = self.backend_stats.setdefault(b, {"calls": 0, "ok": 0, "timeout": 0, "failed": 0, "skipped": 0,
                                                             "elapsed_ms": 0, "tokens": 0, "rate_limited": 0})
            rec_backend["calls"] += 1
            rec_backend[status] = rec_backend.get(status, 0) + 1
            rec_backend["elapsed_ms"] += elapsed_ms or 0
            rec_backend["tokens"] += tokens or 0
            if "回数上限" in text or "limit" in text:
                rec_backend["rate_limited"] += 1
        if extra.get("call"):
            s = self.stats.setdefault(speaker, empty_stats())
            s["calls"] += 1
            s[status] = s.get(status, 0) + 1
            s["elapsed_ms"] += elapsed_ms or 0
            if kind == "message":
                s["chars"] += len(text)
                s["tokens"] += tokens or 0
        self.bus.publish(E.RECORD, **rec)
        return rec

    def _system(self, text: str, round_: str | None = None, status: str = "ok", **extra: Any) -> dict[str, Any]:
        return self._record(round_ or PHASE_ROUND[self.phase], "system", "system", text, status=status, **extra)

    def _notice(self, text: str, level: str = "info") -> None:
        self.bus.publish(E.NOTICE, text=text, level=level)

    def _state_changed(self) -> None:
        self.bus.publish(E.STATE, **self.status())

    def _publish_minutes(self) -> None:
        self.bus.publish(E.MINUTES, markdown=self.minutes.render())

    def _checkpoint(self) -> None:
        state = {
            "phase": str(self.phase),
            "topic_idx": self.topic_idx,
            "critique_done": self.cur.critique_done if self.cur else 0,
            "proposal": self.cur.proposal if self.cur else None,
            "rejections": self.cur.rejections if self.cur else [],
            "absent": sorted(self.absent),
            "rounds_used": self.budget.rounds_used,
            "topics": [t.model_dump() for t in self.topics],
            "extensions": self.extensions,
            "ext_count": self.ext_count,
            "seats": self.cur.seats if self.cur else [],
            "backend_stats": self.backend_stats,
            "exhausted": self.exhausted,
        }
        label = PHASE_LABEL[self.phase]
        if self.cur and self.phase in (Phase.INDEPENDENT, Phase.CRITIQUE, Phase.SUMMARY, Phase.APPROVAL):
            label = f"議題{self.cur.no} {label}"
            if self.phase == Phase.CRITIQUE:
                label += f" {self.cur.critique_done + 1}回目"
        self._system(f"[{label}]", event="checkpoint", state=state)
        self._state_changed()

    # ------------------------------------------------------------------ 状態

    def members(self) -> list[str]:
        if self.cur:
            return self.cur.members
        return [n for n, p in self.cfg.participants.items() if p.enabled]

    def active(self, members: list[str]) -> list[str]:
        return [m for m in members if m not in self.absent]

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "phase": str(self.phase),
            "phase_label": PHASE_LABEL[self.phase],
            "topic_no": self.cur.no if self.cur else None,
            "topic_title": self.cur.title if self.cur else None,
            "topics_total": len(self.topics),
            "critique_done": self.cur.critique_done if self.cur else 0,
            "critique_rounds": self.cfg.meeting.critique_rounds,
            "inflight": {f.participant: {"elapsed_s": round(now - f.started, 1), "round": f.round} for f in self.inflight.values()},
            "calls_left": self.budget.calls_left,
            "rounds_left": self.budget.rounds_left,
            "absent": sorted(self.absent),
            "waiting": self._waiting,
            "paused": self._paused_info,
            "participants": {n: self.names[n] for n in self.cfg.participants if self.cfg.participants[n].enabled},
        }

    # ------------------------------------------------------------------ 実行

    async def run(self) -> None:
        handlers: dict[Phase, Callable[[], Any]] = {
            Phase.INTRO: self._intro,
            Phase.TOPIC: self._topic,
            Phase.INDEPENDENT: self._independent,
            Phase.CRITIQUE: self._critique,
            Phase.SUMMARY: self._summary,
            Phase.APPROVAL: self._approval_phase,
            Phase.FINAL: self._final,
        }
        if self._resumed:
            self._system("会議を再開しました", event="resumed")
        else:
            self._system(f"会議「{self.cfg.meeting.name}」を開始しました", event="meeting_start")
        self._publish_minutes()
        try:
            while self.phase != Phase.DONE:
                self._checkpoint()
                self.phase = await handlers[self.phase]()
            self._checkpoint()
            self.bus.publish(E.MEETING_END, reason="done")
        except MeetingAborted as e:
            self._system(f"会議を中断しました: {e}", status="failed", event="aborted")
            self.bus.publish(E.MEETING_END, reason=f"aborted: {e}")
        except asyncio.CancelledError:
            self._system("会議を中断しました（resume で再開できます）", status="skipped", event="interrupted")
            self.bus.publish(E.MEETING_END, reason="interrupted")
            raise
        finally:
            for t in list(self._side_tasks):
                t.cancel()

    async def _wait(self, what: str) -> None:
        self._waiting = what
        self._wake.clear()
        self._state_changed()
        try:
            await self._wake.wait()
        finally:
            self._waiting = None
            self._state_changed()

    async def _maybe_wait_next(self) -> None:
        if not self.cfg.meeting.auto_advance and not self.headless:
            self._notice("/next（Ctrl+N）で次のラウンドへ進みます")
            await self._wait("next")

    # ---- フェーズごとの処理

    async def _intro(self) -> Phase:
        members = self.active(self.members())
        self.bus.publish(E.ROUND_START, round="intro", topic=None, n=1)
        tmpl = self.prompts.get("intro")
        generated = []
        for m in members:
            persona = self.cfg.participants[m].persona_text()
            if persona:
                # ペルソナがあればモデルを呼ばずにそれを自己紹介にする（トークンの節約）
                self._record("intro", m, "message", persona, topic=None, source="persona")
                self.bus.publish(E.SPEECH_END, participant=m, round="intro", topic=None, status="ok", text=persona, elapsed_ms=0)
            else:
                generated.append(m)
        await asyncio.gather(*(self._call(m, "intro", tmpl) for m in generated))
        self.bus.publish(E.ROUND_END, round="intro", topic=None)
        self._system("自己紹介が終わりました", event="intro_end")
        return Phase.TOPIC

    async def _topic(self) -> Phase:
        self.cur = None
        self._skip_rest = False
        if self._end_requested:
            self._skip_extensions("会議が打ち切られたため見送り")
            return Phase.FINAL
        if self.topic_idx < len(self.topics) and not self.budget.can_call():
            self._system("予算（呼び出し回数）を使い切ったため、残りの議題を打ち切って総括に進みます", event="budget_exceeded")
            self._skip_extensions("予算切れのため見送り")
            return Phase.FINAL
        while self.topic_idx >= len(self.topics):
            # 全議題が終わったら、延長の待ち行列を（予算が残っていれば）回し、それから総括へ。
            # 議題が1つもないときだけ /topic を待つ
            if self.topic_idx > 0 and self._start_next_extension():
                break
            if self.headless or self._end_requested or self.topic_idx > 0:
                return Phase.FINAL
            self._notice("議題を /topic <議題> で追加してください（/end で会議を締めくくります）")
            await self._wait("topic")
        t = self.topics[self.topic_idx]
        enabled = [n for n, p in self.cfg.participants.items() if p.enabled]
        chair = t.chair or self.cfg.chair
        members = [n for n in enabled if t.participants is None or n in t.participants or n == chair]
        self.cur = TopicState(self.topic_idx + 1, t.title, t.premise, members, chair)
        self._system(f"議題{self.cur.no}「{t.title}」を開始します", event="topic_start", title=t.title,
                     extension_of=t.extension_of)
        self.cur.messages.extend(self._pre_topic)
        self._pre_topic.clear()
        if self.cfg.seating:
            self._seat(self.cur, await self._plan_seating(self.cur))
        if self.kb is not None and self.cfg.knowledge:
            await self._add_materials(self.cur, f"{t.title}\n{t.premise}", self.cfg.knowledge.top_k, by=None)
        return Phase.INDEPENDENT

    # ---- 席決め（議長が議題ごとに、ペルソナと経路を選ぶ）

    def available_backends(self) -> list[str]:
        """席に選べる経路: 準備できていて（API キーなどがある）、利用枠に当たっておらず、5時間枠の目安も超えていないもの。"""
        st = self.cfg.seating
        ok = [b for b, c in st.backends.items() if c.enabled and b in self.backends and b not in self.exhausted]
        w = self.window or {}
        limit_adapters = w.get("limit_adapters") or {}
        limit_backends = w.get("limit_backends") or {}
        if limit_adapters or limit_backends:
            non_limited = [
                b for b in ok
                if b not in limit_backends
                and not (st.backends[b].adapter == "claude_code" and "claude_code" in limit_adapters)
            ]
            if non_limited:
                ok = non_limited
        over_adapters: set[str] = set()
        limit = self.cfg.budget.claude_5h_tokens
        if limit and w.get("claude_tokens", 0) >= limit:
            over_adapters.add("claude_code")
        by_adapter = w.get("by_adapter") or {}
        for ad, lim in (self.cfg.budget.adapter_5h_tokens or {}).items():
            if lim and (by_adapter.get(ad) or {}).get("tokens_excl_cache_read", 0) >= lim:
                over_adapters.add(ad)
        if over_adapters:
            light = [b for b in ok if st.backends[b].adapter not in over_adapters]
            if light:
                ok = light
        return ok

    def _refresh_window(self) -> None:
        from ..storage.archive import window_usage

        try:
            self.window = window_usage([Path(self.cfg.meetings_dir)])
        except Exception as e:  # noqa: BLE001
            log.info("window usage failed: %s", e)
            self.window = {}

    def _seating_info(self) -> tuple[str, str]:
        st = self.cfg.seating
        personas = "\n".join(f"- {pid}: {m['name']}（{m['summary']}）" for pid, (m, _) in self.persona_lib.items())
        lines = []
        avail = self.available_backends()
        for bid, b in st.backends.items():
            if bid not in avail:
                continue
            o = b.options()
            u = self.backend_stats.get(bid)
            used = (f"この会議での実績: {u['calls']}回、平均 {u['elapsed_ms'] / max(u['calls'], 1) / 1000:.0f}秒、"
                    f"失敗 {u.get('failed', 0) + u.get('timeout', 0)}回、回数上限 {u['rate_limited']}回") if u else "この会議ではまだ使っていない"
            scope = "機密も見てよい" if b.share_scope == "all" else "機密は伏せ字で届く"
            lines.append(f"- {bid}: {b.adapter} / {o.get('model', '既定')}（{scope}）。{o.get('note', '')} {used}")
        w = self.window or {}
        by_adapter = w.get("by_adapter") or {}
        ad_limits = dict(self.cfg.budget.adapter_5h_tokens or {})
        if self.cfg.budget.claude_5h_tokens and "claude_code" not in ad_limits:
            ad_limits["claude_code"] = self.cfg.budget.claude_5h_tokens
        enabled_adapters = list(dict.fromkeys(st.backends[b].adapter for b in avail))
        usage_parts = []
        for ad in enabled_adapters:
            info = by_adapter.get(ad) or {}
            tok = info.get("tokens_excl_cache_read", 0)
            calls = info.get("calls", 0)
            lim = ad_limits.get(ad)
            usage_parts.append(f"{ad}: {calls}回 / {tok:,}トークン" + (f"（目安上限 {lim:,}）" if lim else ""))
        if usage_parts:
            lines.append("（直近5時間の消費: " + "、".join(usage_parts) + "）")
        recent_limited = [b for b in st.backends if b not in avail and b not in self.exhausted and st.backends[b].enabled and b in self.backends]
        if self.exhausted or recent_limited:
            dropped = list(dict.fromkeys([*self.exhausted.keys(), *recent_limited]))
            lines.append(f"（利用上限に当たって今は使えない経路: {', '.join(dropped)}）")
        hist = [h for h in self.seat_hist.values() if h["backend"] in avail]
        if hist:
            hist.sort(key=lambda h: (h["persona"], -(h["avg_score"] or 0)))
            rows = []
            for h in hist:
                parts = [f"{k.capitalize()} {v['avg_score']}({v['rated']})" for k, v in (h.get("by_chair") or {}).items()]
                detail = f"（{' / '.join(parts)}）" if parts else f"（{h['rated']} 回分）"
                rows.append(f"  - {h['persona']} × {h['backend']}: 座った {h['seated']} 回、"
                            f"議長の採点 {h['avg_score'] if h['avg_score'] is not None else '—'}{detail}、"
                            f"1回あたり {h['tokens_per_call'] or '—'} トークン")
            lines.append("これまでの組み合わせの成績（ペルソナ × 経路）:\n" + "\n".join(rows))
        return personas, "\n".join(lines)

    def _fixed_seats(self, avail: list[str]) -> list[dict[str, Any]]:
        """毎議題かならず座る席。経路が使えないとき（利用枠など）は、使える経路に振り替える。"""
        out = []
        for f in self.cfg.seating.fixed:
            pid, bid = f.get("persona", ""), f.get("backend", "")
            if pid not in self.persona_lib:
                continue
            reason = f.get("reason") or "固定の席"
            rot = f.get("backends")
            if rot:
                # ローテーション: これまでの会議で、このペルソナが座った回数が少ない経路から使う（得意不得意を比べるため）
                cands = [b for b in rot if b in avail] or list(rot)
                seated = {b: (self.seat_hist.get(f"{pid}|{b}") or {}).get("seated", 0) for b in cands}
                bid = min(cands, key=lambda b: (seated[b], cands.index(b)))
                reason += f"（ローテーション: {bid}、これまで {seated[bid]} 回）"
            if bid not in avail:
                if not avail:
                    continue
                reason += f"（{bid} が使えないので {avail[0]} に振り替え）"
                bid = avail[0]
            out.append({"persona": pid, "backend": bid, "reason": reason, "fixed": True})
        return out

    async def _plan_seating(self, cur: TopicState) -> list[dict[str, Any]]:
        st = self.cfg.seating
        self._refresh_window()
        try:
            from ..storage.archive import seat_history

            self.seat_hist = seat_history([Path(self.cfg.meetings_dir)])
        except Exception as e:  # noqa: BLE001
            log.info("seat history failed: %s", e)
            self.seat_hist = {}
        personas, backends = self._seating_info()
        avail = self.available_backends()
        fixed = self._fixed_seats(avail)
        tmpl = self.prompts.get("rounds/seating")
        fixed_desc = "、".join(f"{self.persona_lib[s_['persona']][0]['name']}（{s_['persona']} / {s_['backend']}）" for s_ in fixed)
        extra = {"personas": personas, "backends": backends, "min_seats": str(st.min_seats), "max_seats": str(st.max_seats),
                 "budget": self.budget.describe(),
                 "fixed": (f"固定の席（あなたが選ぶ席とは別に、かならず座る）: {fixed_desc}。このペルソナは選ばないこと。" if fixed else "")}
        reply = await self._call(cur.chair, "plan", tmpl, extra=extra, schema=SEATING_SCHEMA, force=True)
        seats: list[dict[str, Any]] = []
        if reply is not None:
            try:
                data = reply.structured if reply.structured is not None else extract_json(reply.text)
                for x in data.get("seats", []):
                    pid, bid = str(x.get("persona", "")), str(x.get("backend", ""))
                    if pid in self.persona_lib and bid in avail and all(s_["persona"] != pid for s_ in seats + fixed):
                        seats.append({"persona": pid, "backend": bid, "reason": str(x.get("reason", ""))[:300]})
            except Exception as e:  # noqa: BLE001
                log.info("seating parse failed: %s", e)
        seats = seats[: st.max_seats]
        self._seat_fallbacks = 0
        if len(seats) < st.min_seats:
            # 足りなければ、一覧の順に埋める（経路は順番に割り当てる）
            enabled = avail or [b for b in st.backends if b in self.backends]
            for pid in self.persona_lib:
                if len(seats) >= st.min_seats:
                    break
                if all(s_["persona"] != pid for s_ in seats + fixed):
                    seats.append({"persona": pid, "backend": enabled[len(seats) % len(enabled)], "reason": "（議長の指定が足りないので自動で補った）"})
                    self._seat_fallbacks += 1
        self._explore(seats, avail)
        return fixed + seats

    def _explore(self, seats: list[dict[str, Any]], avail: list[str]) -> None:
        """試行席: 議長が選んだ席のうち explore_seats 席だけ、そのペルソナでまだ試した回数の少ない経路に替える。

        どの役にどのモデルが向くかの成績（議長の採点）を、全部の役で集めるため。替えるのは、いまの組み合わせを
        いちばん多く試している席から。試した回数が同じなら替えない。
        """
        n = self.cfg.seating.explore_seats
        if n <= 0 or len(avail) < 2:
            return
        count = lambda p, b: (self.seat_hist.get(f"{p}|{b}") or {}).get("seated", 0)  # noqa: E731
        order = sorted(range(len(seats)), key=lambda i: -count(seats[i]["persona"], seats[i]["backend"]))
        done = 0
        for i in order:
            if done >= n:
                break
            s_ = seats[i]
            cur = count(s_["persona"], s_["backend"])
            alt = min(avail, key=lambda b: (count(s_["persona"], b), avail.index(b)))
            if alt != s_["backend"] and count(s_["persona"], alt) < cur:
                s_["reason"] = f"{s_.get('reason', '')}（試行: {s_['backend']} → {alt}。成績を集めるため、試した回数の少ない経路に替えた）"
                s_["backend"], s_["explored"] = alt, True
                done += 1

    def _seat(self, cur: TopicState, seats: list[dict[str, Any]], record: bool = True) -> None:
        from ..config import ParticipantConfig

        st = self.cfg.seating
        ids = []
        for seat in seats:
            pid, bid = seat["persona"], seat["backend"]
            meta, body = self.persona_lib[pid]
            b = st.backends[bid]
            data = b.model_dump(exclude={"role", "display_name", "persona", "persona_file", "optional"}) | (b.model_extra or {})
            data.update(role=meta.get("role", "member"), display_name=meta["name"], persona=body, optional=True)
            self.cfg.participants[pid] = ParticipantConfig.model_validate(data)
            self.adapters[pid] = self.backends[bid]
            self.names[pid] = meta["name"]
            self.seat_backend[pid] = bid
            ids.append(pid)
        cur.seats = seats
        cur.members = [cur.chair] + ids
        if record:
            desc = "、".join(f"{self.names[s_['persona']]}（{s_['backend']}）" for s_ in seats)
            reasons = "\n".join(f"- {self.names[s_['persona']]}（{s_['backend']}）: {s_['reason']}" for s_ in seats)
            self._system(f"議長が席を決めました: {desc}\n{reasons}", event="seating", fallbacks=getattr(self, "_seat_fallbacks", 0), seats=[
                {**s_, "name": self.names[s_["persona"]], "role": self.cfg.participants[s_["persona"]].role,
                 "model": st.backends[s_["backend"]].options().get("model"), "adapter": st.backends[s_["backend"]].adapter}
                for s_ in seats])
        self._state_changed()

    # ---- 資料庫

    REQUEST_RE = re.compile(r"^\s*[【\[]資料請求[】\]]\s*[:：]?\s*(.+?)\s*$", re.M)

    async def _add_materials(self, cur: TopicState, query: str, k: int, by: str | None) -> list[dict[str, Any]]:
        from ..knowledge.index import chunk_to_dict

        have = {m["id"] for m in cur.materials}
        found = await asyncio.to_thread(self.kb.search, query, k, True, have)
        new = [chunk_to_dict(c) | ({"requested": True} if by else {}) for c in found]
        if self.local_llm is not None:
            # 考えさせない仕事（要約）はローカル LLM に任せる。失敗しても原文で続ける
            for m in new:
                try:
                    m["short"] = await asyncio.to_thread(self.local_llm.compress, m["text"], cur.title)
                except Exception as e:  # noqa: BLE001
                    log.info("local llm compress failed: %s", e)
        cur.materials += new
        cites = "、".join(f"`{m['path']}:{m['start']}-{m['end']}`" for m in new) or "（見つかりませんでした）"
        who = f"{self.names.get(by, by)} の資料請求「{query.strip()[:60]}」" if by else "議題の見出しで検索"
        self._system(f"資料庫から関連資料を {len(new)} 件追加しました（{who}）: {cites}",
                     event="kb_materials" if by is None else "kb_lookup", ids=[m["id"] for m in new], query=query, by=by)
        return new

    async def _handle_requests(self, cur: TopicState, speaker: str, text: str) -> None:
        if self.kb is None or not self.cfg.knowledge:
            return
        for q in self.REQUEST_RE.findall(text)[:2]:
            key = re.sub(r"\s+", " ", q).strip().lower()
            done = getattr(cur, "_queries", None)
            if done is None:
                done = cur._queries = set()  # type: ignore[attr-defined]
            if key in done:
                # 同じ検索語はこの議題で一度だけ探す（上限を同じ請求で使い切らない）
                self._system(f"「{q[:40]}」はこの議題ですでに探したので、関連資料はそのままです", event="kb_duplicate", status="skipped")
                continue
            done.add(key)
            if cur.kb_requests >= self.cfg.knowledge.requests_per_topic:
                self._system(f"資料請求の上限（議題ごとに {self.cfg.knowledge.requests_per_topic} 回）に達したので、「{q[:40]}」は探しませんでした",
                             event="kb_limit", status="skipped")
                return
            cur.kb_requests += 1
            await self._add_materials(cur, q, self.cfg.knowledge.request_k, by=speaker)

    def discussants(self, cur: TopicState) -> list[str]:
        """独立回答・相互批判で発言する参加者。"""
        ms = self.active(cur.members)
        if not self.cfg.meeting.chair_participates:
            ms = [m for m in ms if m != cur.chair]
        return ms

    async def _independent(self) -> Phase:
        cur = self._require_cur()
        members = self.discussants(cur)
        if not self.budget.can_start_round(len(members)):
            self._system("予算に達したため議長のまとめに進みます", event="budget_exceeded")
            return Phase.SUMMARY
        self.budget.rounds_used += 1
        self.bus.publish(E.ROUND_START, round="independent", topic=cur.no, n=1)
        tmpl = self.prompts.get("rounds/independent")
        # 独立回答では他の参加者の回答を見せない（ユーザーの発言だけ渡す）
        only_user = lambda msgs: [m for m in msgs if m.speaker == "user"]  # noqa: E731
        await asyncio.gather(*(self._call(m, "independent", tmpl, filter_messages=only_user) for m in members))
        self.bus.publish(E.ROUND_END, round="independent", topic=cur.no)
        await self._maybe_wait_next()
        return Phase.CRITIQUE

    async def _critique(self) -> Phase:
        cur = self._require_cur()
        if cur.critique_done >= self.cfg.meeting.critique_rounds or self._skip_rest:
            self._skip_rest = False
            return Phase.SUMMARY
        members = self.discussants(cur)
        if not self.budget.can_start_round(len(members)):
            self._system("予算に達したため議長のまとめに進みます", event="budget_exceeded")
            return Phase.SUMMARY
        self.budget.rounds_used += 1
        n = cur.critique_done + 1
        self.bus.publish(E.ROUND_START, round="critique", topic=cur.no, n=n)
        tmpl = self.prompts.get("rounds/critique")
        extra = {"rejection": self._rejection_note(cur), "extension_hint": ""}
        if n == self.cfg.meeting.critique_rounds and self._can_request_extension(cur):
            extra["extension_hint"] = self.prompts.get("rounds/extension_hint")
        if self.cfg.meeting.critique_parallel:
            await asyncio.gather(*(self._call(m, "critique", tmpl, extra=extra) for m in members))
        else:
            # 1人ずつ。前の人の批判や、途中の割り込みが次の人の文脈に入る
            for m in members:
                if m in self.absent:
                    continue
                await self._call(m, "critique", tmpl, extra=extra)
        self.bus.publish(E.ROUND_END, round="critique", topic=cur.no)
        cur.critique_done = n
        await self._maybe_wait_next()
        return Phase.CRITIQUE

    async def _summary(self) -> Phase:
        cur = self._require_cur()
        self.bus.publish(E.ROUND_START, round="summary", topic=cur.no, n=1)
        tmpl = self.prompts.get("rounds/summary")
        seat_list = "、".join(f"{s_['persona']}（{self.names.get(s_['persona'], s_['persona'])}）" for s_ in cur.seats)
        extra = {"seat_rule": (f"seat_scores には、各席（{seat_list}）の貢献を1〜5の整数で付ける（具体性・根拠・ほかの席にない視点）。note に一言理由。"
                               if cur.seats else "seat_scores は空の配列にする。"),
                 "rejection": self._rejection_note(cur), "schema": json.dumps(PROPOSAL_SCHEMA, ensure_ascii=False),
                 "extension_rule": self.prompts.get("rounds/extension_rule_yes" if self._can_request_extension(cur) else "rounds/extension_rule_no")}
        reply = await self._call(cur.chair, "summary", tmpl, extra=extra, schema=PROPOSAL_SCHEMA, force=True)
        proposal: Proposal | None = None
        if reply is not None:
            proposal = self._parse_proposal(reply)
            if proposal is None:
                # 形式が崩れていたら1回だけ再要求する
                self._system("議長のまとめが JSON として読めなかったので、もう一度頼みます", event="summary_retry", status="failed")
                retry = self.prompts.get("rounds/summary_retry")
                reply2 = await self._call(cur.chair, "summary", tmpl + "\n\n" + retry, extra=extra, schema=PROPOSAL_SCHEMA, force=True)
                proposal = self._parse_proposal(reply2) if reply2 else None
                if proposal is None:
                    raw = (reply2 or reply).text
                    proposal = Proposal(summary=raw)
                    self._notice("議長のまとめがJSON形式になりませんでした。/edit で整えてから承認できます", "warning")
        if proposal is None:
            proposal = Proposal(summary="（議長のまとめを取得できませんでした。/edit で記入してください）")
        if proposal.extension_requested and not self._can_request_extension(cur):
            proposal.extension_requested = False
        if proposal.extension_requested and not proposal.handover.strip():
            # 延長を申し出るなら申し送りは必須。1回だけ書き直しを求める
            ask = self.prompts.get("rounds/handover_required")
            reply3 = await self._call(cur.chair, "summary", tmpl + "\n\n" + ask, extra=extra, schema=PROPOSAL_SCHEMA, force=True)
            p3 = self._parse_proposal(reply3) if reply3 else None
            if p3 is not None and (not p3.extension_requested or p3.handover.strip()):
                proposal = p3
            else:
                proposal.extension_requested = False
                self._notice("議長が申し送りを書かなかったので、延長の申し出は取り下げました", "warning")
        if cur.seats and proposal.seat_scores:
            by = {s_["persona"]: s_ for s_ in cur.seats}
            scores = []
            for sc in proposal.seat_scores:
                seat = by.get(str(sc.get("persona", "")))
                if not seat:
                    continue
                try:
                    score = max(1, min(5, int(sc.get("score"))))
                except (TypeError, ValueError):
                    continue
                b = self.cfg.seating.backends[seat["backend"]]
                scores.append({"persona": seat["persona"], "backend": seat["backend"], "model": b.options().get("model"),
                               "adapter": b.adapter, "score": score, "note": str(sc.get("note", ""))[:200]})
            if scores:
                desc = "、".join(f"{self.names.get(x['persona'], x['persona'])}（{x['backend']}）{x['score']}" for x in scores)
                ch_p = self.cfg.participants.get(self.cfg.chair)
                ch_model = str((ch_p.options().get("model") if ch_p else "") or "")
                self._system(f"議長の採点: {desc}", event="seat_scores", scores=scores,
                             chair_model=ch_model, chair_adapter=ch_p.adapter if ch_p else "")
        cur.proposal = proposal.model_dump()
        self.bus.publish(E.ROUND_END, round="summary", topic=cur.no)
        return Phase.APPROVAL

    def _parse_proposal(self, reply: Reply) -> Proposal | None:
        try:
            data = reply.structured if reply.structured is not None else extract_json(reply.text)
            return Proposal.model_validate(data)
        except Exception as e:  # noqa: BLE001
            log.info("proposal parse failed: %s", e)
            return None

    async def _approval_phase(self) -> Phase:
        cur = self._require_cur()
        proposal = Proposal.model_validate(cur.proposal or {})
        if self.cfg.meeting.approval == "auto":
            # 人間の承認を待たずに進む。人間はあとで決定事項を取り下げられる
            rec = self._system(f"議題{cur.no}のまとめを自動で承認しました", round_="approval", event="approved",
                               proposal=proposal.model_dump(), title=cur.title, auto=True)
            self.minutes.apply(cur.no, cur.title, proposal, rec["ts"])
            self._after_approval(cur, proposal)
            self._publish_minutes()
            self.topic_idx += 1
            return Phase.TOPIC
        if self.headless:
            self.minutes.pending[cur.no] = (cur.title, proposal)
            self._system(f"議題{cur.no}のまとめを承認待ちのまま保留しました", status="skipped", event="pending", proposal=proposal.model_dump(), title=cur.title)
            self._publish_minutes()
            self.topic_idx += 1
            return Phase.TOPIC
        loop = asyncio.get_running_loop()
        self._approval = loop.create_future()
        self.bus.publish(E.AWAITING_APPROVAL, topic=cur.no, title=cur.title, chair=cur.chair, proposal=proposal.model_dump())
        self._waiting = "approval"
        self._state_changed()
        try:
            action, payload = await self._approval
        finally:
            self._approval = None
            self._waiting = None
        if action == "approve":
            approved = Proposal.model_validate(payload) if payload is not None else proposal
            rec = self._system(
                f"議題{cur.no}のまとめが承認されました" + ("（修正あり）" if payload is not None else ""),
                round_="approval", event="approved", proposal=approved.model_dump(), title=cur.title,
            )
            self.minutes.apply(cur.no, cur.title, approved, rec["ts"])
            self._after_approval(cur, approved)
            self._publish_minutes()
            self.topic_idx += 1
            return Phase.TOPIC
        reason = str(payload or "").strip() or "（理由なし）"
        self._record("approval", "user", "message", f"却下: {reason}", event="rejected")
        cur.messages.append(Message("user", "approval", f"却下: {reason}", self.seq))
        cur.rejections.append(reason)
        cur.critique_done = 0
        cur.proposal = None
        self._skip_rest = False
        return Phase.CRITIQUE

    # ---- 延長

    def _topic_cfg(self, no: int) -> TopicConfig | None:
        return self.topics[no - 1] if 1 <= no <= len(self.topics) else None

    def _can_request_extension(self, cur: TopicState) -> bool:
        t = self._topic_cfg(cur.no)
        return self.cfg.meeting.extensions and not (t and t.extension_of)

    def _handover(self, no: int, title: str, handover: str, reason: str, status: str, **extra: Any) -> None:
        self.minutes.handovers[no] = {"title": title, "handover": handover, "reason": reason, "status": status}
        self._system(f"議題{no}の申し送り: {status}", event="handover", topic=no, title=title,
                     handover=handover, reason=reason, status=status, **extra)

    def _after_approval(self, cur: TopicState, p: Proposal) -> None:
        t = self._topic_cfg(cur.no)
        if t and t.extension_of:
            # 延長した議題が終わったら、元の議題の申し送りを「議論済み」にする
            h = self.minutes.handovers.get(t.extension_of)
            if h:
                self._handover(t.extension_of, h["title"], h["handover"], h["reason"], f"延長（議題{cur.no}）で議論済み")
            return
        if not p.extension_requested:
            return
        if not p.handover.strip():
            self._notice("延長の申し出に申し送りがないので、延長しません", "warning")
            return
        if not self.cfg.meeting.extensions:
            self._handover(cur.no, cur.title, p.handover, p.extension_reason, "延長は無効の設定のため見送り")
            return
        self.extensions.append({"topic": cur.no, "title": cur.title, "handover": p.handover, "reason": p.extension_reason})
        self._handover(cur.no, cur.title, p.handover, p.extension_reason, "全議題のあと、予算が残っていれば延長")

    def _skip_extensions(self, why: str) -> None:
        for e in self.extensions:
            self._handover(e["topic"], e["title"], e["handover"], e["reason"], why)
        self.extensions = []

    def _start_next_extension(self) -> bool:
        """待ち行列の先頭の延長を、予算が残っていれば新しい議題として足す。足したら True。"""
        while self.extensions:
            if self.ext_count >= self.cfg.meeting.max_extensions:
                self._skip_extensions(f"延長の上限（{self.cfg.meeting.max_extensions}回）に達したため見送り")
                return False
            e = self.extensions[0]
            orig = self._topic_cfg(e["topic"])
            need = self._extension_need(orig)
            ok, why = self.budget.can_afford(need)
            if not ok or self.budget.rounds_left < 1 + self.cfg.meeting.critique_rounds:
                self._skip_extensions("予算が足りないため見送り（" + (why or "ラウンドの残りが足りません") + "）")
                return False
            self.extensions.pop(0)
            self.ext_count += 1
            premise = self.prompts.get("rounds/extension_premise")
            premise = premise.replace("$orig_no", str(e["topic"])).replace("$orig_title", e["title"]) \
                .replace("$reason", e["reason"] or "（記載なし）").replace("$handover", e["handover"])
            self.topics.append(TopicConfig(title=f"【延長】{e['title']}", premise=premise,
                                           participants=orig.participants if orig else None,
                                           chair=orig.chair if orig else None, extension_of=e["topic"]))
            self._handover(e["topic"], e["title"], e["handover"], e["reason"], f"延長中（議題{len(self.topics)}）")
            self._system(f"議題{e['topic']}「{e['title']}」を延長します（議題{len(self.topics)}）", event="extension_started",
                         topic=None, extension_of=e["topic"])
            return True
        return False

    def _extension_need(self, orig: TopicConfig | None) -> int:
        """延長1本に要る呼び出し回数の見込み（独立回答＋相互批判、まとめ、総括）。"""
        enabled = [n for n, p in self.cfg.participants.items() if p.enabled]
        chair = (orig.chair if orig else None) or self.cfg.chair
        members = [n for n in enabled if orig is None or orig.participants is None or n in orig.participants or n == chair]
        n_disc = len([m for m in members if m not in self.absent and (self.cfg.meeting.chair_participates or m != chair)])
        return n_disc * (1 + self.cfg.meeting.critique_rounds) + 2

    def extend_after_done(self, topic: int, note: str = "") -> int:
        """終わった会議を、ユーザーの指示で開き直し、議題 topic の延長を1本足す。足した議題の番号を返す。

        議長の申し送りがあればそれを、なければ元の議題の要約を引き継ぐ。延長1本分の予算（呼び出し回数・
        ラウンド）を足すので、会議の予算を使い切っていても回せる。延長が終わったら総括を書き直す。
        """
        if self.phase != Phase.DONE:
            raise ValueError("終わった会議だけ延長できます（進行中・中断中なら再開してください）")
        orig = self._topic_cfg(topic)
        if orig is None:
            raise ValueError(f"議題{topic}はありません（1〜{len(self.topics)}）")
        note = note.strip()
        title = orig.title.removeprefix("【延長】")
        h = self.minutes.handovers.get(topic) or {}
        handover = (h.get("handover") or "").strip()
        if not handover:
            s = self.minutes.topics.get(topic)
            lines = ["（議長の申し送りはありません。元の議題のまとめを引き継ぎます）"]
            if s and s.summary:
                lines += ["要約:", s.summary]
            if s and s.disagreements:
                lines += ["対立点:"] + [f"- {x}" for x in s.disagreements]
            ids = [x.id for x in self.minutes.questions if x.topic == topic] + [x.id for x in self.minutes.todos if x.topic == topic]
            if ids:
                lines += ["この議題の未決事項・ToDo: " + "、".join(ids) + "（現在の議事録を参照）"]
            handover = "\n".join(lines)
        if note:
            handover += f"\n\nユーザーの指示（最優先で扱う）:\n{note}"
        reason = note or h.get("reason") or "ユーザーの指示"
        # 予算: 延長1本分を、今の残り（使い切って超えていればその分も）に足す
        need = self._extension_need(orig)
        extra_calls = need + max(0, self.budget.calls_used - self.budget.cfg.max_calls)
        extra_rounds = 1 + self.cfg.meeting.critique_rounds + max(0, self.budget.rounds_used - self.budget.cfg.max_rounds)
        self.budget.cfg.max_calls += extra_calls
        self.budget.cfg.max_rounds += extra_rounds
        premise = self.prompts.get("rounds/extension_premise")
        premise = premise.replace("$orig_no", str(topic)).replace("$orig_title", title) \
            .replace("$reason", reason).replace("$handover", handover)
        self.topics.append(TopicConfig(title=f"【延長】{title}", premise=premise, participants=orig.participants,
                                       chair=orig.chair, extension_of=topic))
        self.topic_idx = len(self.topics) - 1
        self._end_requested = False
        self.phase = Phase.TOPIC
        no = len(self.topics)
        self._handover(topic, title, handover, reason, f"ユーザーの指示で延長中（議題{no}）")
        self._system(f"ユーザーの指示で、議題{topic}「{title}」を延長します（議題{no}）", event="user_extension",
                     topic=None, extension_of=topic, extra_calls=extra_calls, extra_rounds=extra_rounds)
        self._checkpoint()
        return no

    async def _final(self) -> Phase:
        self.cur = None
        self.bus.publish(E.ROUND_START, round="summary", topic=None, n=1)
        tmpl = self.prompts.get("rounds/final")
        reply = await self._call(self.cfg.chair, "summary", tmpl, force=True)
        text = reply.text if reply else "（総括を取得できませんでした）"
        self.minutes.final_summary = text
        self._system("会議の総括を議事録に書きました", round_="summary", event="final", summary=text, topic=None)
        self._system("統計", round_="summary", event="stats", stats=self.stats, topic=None)
        self._publish_minutes()
        self.bus.publish(E.ROUND_END, round="summary", topic=None)
        return Phase.DONE

    def _require_cur(self) -> TopicState:
        assert self.cur is not None
        return self.cur

    def _rejection_note(self, cur: TopicState) -> str:
        if not cur.rejections:
            return ""
        return "ユーザーは前回のまとめを却下しました。理由: " + " / ".join(cur.rejections) + "\nこの理由を踏まえて議論し直してください。"

    # ------------------------------------------------------------------ 1回の発言

    def _failover_on_limit(self, name: str) -> bool:
        """利用上限に当たったとき、別プロバイダ（Gemini <-> Claude）または別バックエンドへ自動で切り替える。"""
        p = self.cfg.participants.get(name)
        if not p:
            return False
        if name in self.seat_backend and self.cfg.seating:
            avail = self.available_backends()
            if not avail:
                return False
            other = [b for b in avail if self.cfg.seating.backends[b].adapter != p.adapter]
            fb = other[0] if other else avail[0]
            bcfg = self.cfg.seating.backends[fb]
            self.adapters[name] = self.backends[fb]
            self.seat_backend[name] = fb
            data = p.model_dump(exclude_none=True)
            data.update(adapter=bcfg.adapter, **bcfg.options())
            from ..config import ParticipantConfig

            self.cfg.participants[name] = ParticipantConfig.model_validate(data)
            self._record("seating", "system", "system", f"{self.names[name]} が利用上限のため {fb} に切り替えました",
                         event="failover", participant=name, backend=fb)
            return True
        if name == self.cfg.chair and p.adapter != "dummy":
            from ..adapters import create_adapter
            from ..config import apply_chair_override

            target = "opus" if p.adapter in ("antigravity_cli", "gemini_api") else ("neon" if p.adapter == "claude_code" else None)
            if not target:
                return False
            apply_chair_override(self.cfg, chair=target)
            np = self.cfg.participants[name]
            self.adapters[name] = create_adapter(name, np.adapter, np.role, np.timeout_s, np.options())
            self.names[name] = np.display_name or name
            self._record("seating", "system", "system", f"議長が利用上限のため {self.names[name]} に切り替えました",
                         event="failover", participant=name, chair=target)
            return True
        return False

    async def _call(
        self,
        name: str,
        round_: str,
        template: str,
        *,
        extra: dict[str, str] | None = None,
        schema: dict[str, Any] | None = None,
        force: bool = False,
        filter_messages: Callable[[list[Message]], list[Message]] | None = None,
        to_user: bool = False,
    ) -> Reply | None:
        p = self.cfg.participants[name]
        # 記録先の議題は呼び出し開始時に固定する（/ask の回答が次の議題に紛れ込まないように）
        cur = self.cur
        topic_no = cur.no if cur else None
        if name in self.absent and not force:
            self._record(round_, name, "system", f"{self.names[name]} は欠席中です", status="skipped")
            return None
        if not force and not self.budget.can_call():
            self._record(round_, name, "system", "予算切れのため発言をスキップしました", status="skipped")
            return None
        adapter = self.adapters[name]
        switched = False
        while True:
            failover_retry = False
            for attempt in range(p.retries + 1):
                self.budget.calls_used += 1
                msgs = list(cur.messages) if cur else list(self._pre_topic)
                if filter_messages:
                    msgs = filter_messages(msgs)
                # 発言の直前に組み立てる（＝直前までの割り込みが文脈に入る）
                prompt = self.ctx.build(
                    participant=name,
                    role=self._effective_role(name, cur),
                    round_name=round_,
                    materials=cur.materials if cur else None,
                    round_template=template,
                    topic_no=topic_no,
                    topic_title=cur.title if cur else None,
                    topic_premise=cur.premise if cur else "",
                    messages=msgs,
                    # 議題ごとの要約まで毎ターン送るとトークンが膨らむので、短い版を渡す
                    minutes_md=self.minutes.render_compact(),
                    members=self.members(),
                    extra=extra,
                )
                tctx = TurnContext(prompt, name, p.role, round_, topic_no, self.meeting_id, self.meeting_dir, schema)
                task = asyncio.create_task(adapter.respond(tctx), name=f"respond:{name}")
                started = time.monotonic()
                self._call_id += 1
                call_id = self._call_id
                self.inflight[call_id] = Inflight(name, task, started, round_)
                self.bus.publish(E.SPEECH_START, participant=name, round=round_, topic=topic_no, attempt=attempt + 1)
                self._state_changed()
                status, err = "ok", ""
                reply: Reply | None = None
                try:
                    reply = await asyncio.wait_for(task, timeout=p.timeout_s)
                except asyncio.TimeoutError:
                    status, err = "timeout", f"{p.timeout_s:g}秒以内に応答がありませんでした"
                except asyncio.CancelledError:
                    me = asyncio.current_task()
                    if task in self._stopped and not (me and me.cancelling()):
                        self._stopped.discard(task)
                        status, err = "skipped", "/stop で中断しました"
                    else:
                        # サブプロセスの kill と回収が終わるまで待ってから抜ける
                        task.cancel()
                        await asyncio.wait({task}, timeout=8)
                        raise
                except Exception as e:  # noqa: BLE001
                    status, err = "failed", f"{type(e).__name__}: {e}"
                finally:
                    self.inflight.pop(call_id, None)
                elapsed = int((time.monotonic() - started) * 1000)
                if reply is not None:
                    # 議長のまとめ（JSON・総括）は議論の発言ではないので文字数上限をかけない
                    text, cut = (reply.text.strip(), False) if round_ == "summary" else self.budget.truncate(reply.text.strip())
                    format_violation = None
                    context_text = text
                    persona_meta, _ = self.persona_lib.get(name, ({}, ""))
                    if round_ in ("independent", "critique") and persona_meta.get("template") == TEMPLATE:
                        context_text, repaired = normalize_reply(text)
                        format_violation = repaired or None
                    # 議題前に始めた /ask の回答が、待っている間に始まった議題に入るようにする
                    target = cur if cur is not None else (self.cur if round_ == "interjection" else None)
                    rec_topic = target.no if target is not None else None
                    self.budget.tokens_used += reply.tokens or 0
                    usage = {k: reply.meta[k] for k in ("turns", "usage", "cost_usd") if reply.meta.get(k) is not None}
                    self._record(round_, name, "message", text, topic=rec_topic, elapsed_ms=elapsed, tokens=reply.tokens,
                                 call=True, truncated=cut or None, to="user" if to_user else None,
                                 prompt_chars=len(prompt), prompt_parts=dict(getattr(self.ctx, "last_parts", {}) or {}),
                                 attempt=attempt + 1, format_violation=format_violation,
                                 context_text=context_text if format_violation else None, **usage)
                    if round_ not in ("intro", "summary", "plan"):  # 自己紹介と席決めは議題の文脈に入れない
                        (target.messages if target is not None else self._pre_topic).append(Message(name, round_, context_text, self.seq))
                        if target is not None:
                            await self._handle_requests(target, name, text)
                    self.bus.publish(E.SPEECH_END, participant=name, round=round_, topic=topic_no, status="ok", text=text, elapsed_ms=elapsed)
                    self._state_changed()
                    reply.text = text
                    return reply
                self._record(round_, name, "error", err, topic=topic_no, status=status, elapsed_ms=elapsed, call=True)
                self.bus.publish(E.SPEECH_END, participant=name, round=round_, topic=topic_no, status=status, text=err, elapsed_ms=elapsed)
                self.bus.publish(E.ERROR, participant=name, text=err, status=status)
                self._state_changed()
                if status == "skipped":
                    return None
                if not switched:
                    from ..storage.archive import classify_error

                    if classify_error(err) in ("利用上限", "回数上限") and self._failover_on_limit(name):
                        switched = True
                        failover_retry = True
                        p = self.cfg.participants[name]
                        adapter = self.adapters[name]
                        break
            if failover_retry:
                continue
            # 再試行しても失敗
            if self._failure_policy(name, cur) == "skip":
                self._record(round_, name, "system", f"{self.names[name]} はこのラウンドを欠席扱いにします", status="skipped")
                return None
            decision = await self._ask_pause(name, err)
            if decision == "skip":
                self._record(round_, name, "system", f"{self.names[name]} の発言を飛ばして続行します", status="skipped")
                return None
            if decision == "abort":
                raise MeetingAborted(f"{self.names[name]} が応答しません")
            # retry: もう一度ループ

    def _effective_role(self, name: str, cur: TopicState | None) -> str:
        """議題ごとに議長を交代したときは、その議題の議長を chair として扱う。"""
        p = self.cfg.participants[name]
        if cur is None:
            return p.role
        if cur.chair == name:
            return "chair"
        if p.role == "chair":
            return "member"
        return p.role

    def _failure_policy(self, name: str, cur: TopicState | None) -> str:
        p = self.cfg.participants[name]
        if p.on_failure:
            return p.on_failure
        return "pause" if self._effective_role(name, cur) == "chair" and not p.optional else "skip"

    async def _ask_pause(self, name: str, err: str) -> str:
        if self.headless:
            raise MeetingAborted(f"必須参加者 {self.names[name]} が失敗しました: {err}")
        self._paused_info = {"participant": name, "reason": err}
        self._system(f"{self.names[name]} が応答しないため会議を一時停止しました: {err}", status="failed", event="paused")
        self._pause = asyncio.get_running_loop().create_future()
        self._waiting = "pause"
        self.bus.publish(E.PAUSED, participant=name, reason=err)
        self._state_changed()
        try:
            return await self._pause
        finally:
            self._pause = None
            self._waiting = None
            self._paused_info = None
            self._state_changed()

    # ------------------------------------------------------------------ ユーザー操作（UI から呼ぶ）

    def interject(self, text: str) -> None:
        """割り込み発言。実行中の発言は止めず、次の発言から全員の文脈に入る。"""
        text = text.strip()
        if not text:
            return
        self._record("interjection", "user", "message", text)
        msg = Message("user", "interjection", text, self.seq)
        (self.cur.messages if self.cur else self._pre_topic).append(msg)

    def add_topic(self, title: str, premise: str = "") -> None:
        self.topics.append(TopicConfig(title=title, premise=premise))
        self._system(f"議題{len(self.topics)}「{title}」を追加しました", event="topic_added", title=title, premise=premise, topic=None)
        if self._waiting == "topic":
            self._wake.set()
        self._state_changed()

    def end(self) -> None:
        """残りの議題を打ち切り、今の議題が終わったら総括に進む。"""
        if not self._end_requested:
            self._system("残りの議題を打ち切ります（今の議題が終わったら総括）", event="end_requested", topic=None)
        self._end_requested = True
        if self._waiting == "topic":
            self._wake.set()

    def next(self) -> str:
        if self._waiting in ("next", "topic"):
            if self._waiting == "topic":
                self.end()
            self._wake.set()
            return "次へ進みます"
        if self.phase in (Phase.INDEPENDENT, Phase.CRITIQUE):
            self._skip_rest = True
            return "このラウンドが終わったら議長のまとめに進みます"
        if self._waiting == "approval":
            return "承認待ちです。/approve /edit /reject のどれかを選んでください"
        return "今は進めるラウンドがありません"

    def approve(self, proposal: dict[str, Any] | None = None) -> str:
        if self._approval is None or self._approval.done():
            return "承認待ちのまとめはありません"
        if proposal is not None:
            Proposal.model_validate(proposal)  # 形式チェック（崩れていれば例外）
        self._approval.set_result(("approve", proposal))
        return "承認しました"

    def reject(self, reason: str) -> str:
        if self._approval is None or self._approval.done():
            return "承認待ちのまとめはありません"
        self._approval.set_result(("reject", reason))
        return "却下しました。相互批判からやり直します"

    def current_proposal(self) -> dict[str, Any] | None:
        return self.cur.proposal if self.cur and self._waiting == "approval" else None

    def resolve_pause(self, decision: str) -> str:
        if self._pause is None or self._pause.done():
            return "一時停止していません"
        self._pause.set_result(decision)
        return {"retry": "再試行します", "skip": "飛ばして続行します", "abort": "会議を中断します"}[decision]

    def skip(self, name: str) -> str:
        if name not in self.cfg.participants:
            return f"参加者 {name} はいません"
        self.absent.add(name)
        self._system(f"{self.names[name]} が一時欠席します", event="absent", participant=name, status="skipped")
        if self._pause is not None and self._paused_info and self._paused_info.get("participant") == name:
            self.resolve_pause("skip")
        self._state_changed()
        return f"{self.names[name]} を欠席にしました"

    def join(self, name: str) -> str:
        if name not in self.cfg.participants:
            return f"参加者 {name} はいません"
        self.absent.discard(name)
        self._system(f"{self.names[name]} が復帰しました", event="rejoin", participant=name)
        self._state_changed()
        return f"{self.names[name]} が復帰しました"

    def ask(self, name: str, question: str) -> str:
        if name not in self.cfg.participants or not self.cfg.participants[name].enabled:
            return f"参加者 {name} はいません"
        self._record("interjection", "user", "message", question, to=name)
        (self.cur.messages if self.cur else self._pre_topic).append(Message("user", "interjection", question, self.seq, to=name))
        tmpl = self.prompts.get("rounds/ask")
        t = asyncio.create_task(self._call(name, "interjection", tmpl, extra={"question": question}, to_user=True))
        self._side_tasks.add(t)
        t.add_done_callback(self._side_tasks.discard)
        return f"{self.names[name]} に質問しました"

    def stop(self) -> str:
        if not self.inflight:
            return "実行中の発言はありません"
        for f in list(self.inflight.values()):
            self._stopped.add(f.task)
            f.task.cancel()
        return "実行中の発言を中断しました"

    def budget_report(self) -> str:
        lines = [self.budget.describe()]
        self._refresh_window()
        w = self.window or {}
        limit = self.cfg.budget.claude_5h_tokens
        lines.append(f"直近5時間の Claude: {w.get('claude_tokens', 0):,} トークン（{w.get('claude_calls', 0)}回）"
                     + (f"／目安 {limit:,}" if limit else "") + (f"　利用枠に当たった経路: {', '.join(self.exhausted)}" if self.exhausted else ""))
        for n, s in self.stats.items():
            tok = f"、{s['tokens']}トークン" if s["tokens"] else ""
            lines.append(f"  {self.names.get(n, n)}: {s['calls']}回（成功{s['ok']}）、{s['elapsed_ms'] / 1000:.0f}秒、{s['chars']}字{tok}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 再開

    def restore(self, records: list[dict[str, Any]]) -> None:
        """transcript から状態を復元する。最後の checkpoint のフェーズからやり直す。"""
        cps = [r for r in records if r.get("event") == "checkpoint"]
        self.seq = max((r.get("seq") or 0 for r in records), default=0)
        self.stats = stats_from_records(records)
        self.budget.calls_used = sum(s["calls"] for s in self.stats.values())
        self.budget.tokens_used = sum(s["tokens"] for s in self.stats.values())
        for r in records:
            ev = r.get("event")
            if ev == "approved":
                self.minutes.apply(r["topic"], r.get("title", ""), Proposal.model_validate(r["proposal"]), r["ts"])
            elif ev == "pending":
                self.minutes.pending[r["topic"]] = (r.get("title", ""), Proposal.model_validate(r["proposal"]))
            elif ev == "final":
                self.minutes.final_summary = r.get("summary", "")
            elif ev == "handover":
                self.minutes.handovers[r["topic"]] = {k: r.get(k, "") for k in ("title", "handover", "reason", "status")}
        self._resumed = True
        if not cps:
            return
        cp = cps[-1]
        st = cp["state"]
        self.phase = Phase(st["phase"])
        self.topic_idx = st["topic_idx"]
        self.absent = set(st.get("absent", []))
        self.budget.rounds_used = st.get("rounds_used", 0)
        self.topics = [TopicConfig.model_validate(t) for t in st.get("topics", [])]
        self.extensions = list(st.get("extensions", []))
        self.ext_count = st.get("ext_count", 0)
        # checkpoint の後の操作（議題の追加・欠席・復帰・打ち切り）を再適用する
        for r in records:
            if r.get("event") == "end_requested":
                self._end_requested = True
            elif r.get("event") == "user_extension":
                # 会議のあとにユーザーが足した延長: 打ち切りを解き、足した予算を戻す
                self._end_requested = False
                self.budget.cfg.max_calls += r.get("extra_calls", 0)
                self.budget.cfg.max_rounds += r.get("extra_rounds", 0)
            if r["seq"] <= cp["seq"]:
                continue
            ev = r.get("event")
            if ev == "topic_added":
                self.topics.append(TopicConfig(title=r["title"], premise=r.get("premise") or ""))
            elif ev == "absent":
                self.absent.add(r["participant"])
            elif ev == "rejoin":
                self.absent.discard(r["participant"])
        if self.phase in (Phase.INDEPENDENT, Phase.CRITIQUE, Phase.SUMMARY, Phase.APPROVAL):
            t = self.topics[self.topic_idx]
            enabled = [n for n, p in self.cfg.participants.items() if p.enabled]
            chair = t.chair or self.cfg.chair
            members = [n for n in enabled if t.participants is None or n in t.participants or n == chair]
            cur = TopicState(self.topic_idx + 1, t.title, t.premise, members, chair)
            cur.critique_done = st.get("critique_done", 0)
            cur.proposal = st.get("proposal")
            cur.rejections = list(st.get("rejections", []))
            cur.messages, _ = self._completed_messages(records, cur.no)
            self.backend_stats = st.get("backend_stats", {}) or {}
            self.exhausted = st.get("exhausted", {}) or {}
            if self.cfg.seating and st.get("seats"):
                self._seat(cur, st["seats"], record=False)
            if self.kb is not None:
                for r in records:
                    if r.get("topic") == cur.no and r.get("event") in ("kb_materials", "kb_lookup"):
                        if r.get("event") == "kb_lookup":
                            cur.kb_requests += 1
                        for cid in r.get("ids", []):
                            c = self.kb.get(cid)
                            if c and all(m["id"] != cid for m in cur.materials):
                                from ..knowledge.index import chunk_to_dict
                                cur.materials.append(chunk_to_dict(c))
            self.cur = cur
        else:
            self.cur = None
            # 議題が始まる前の割り込み・/ask は、次の議題の文脈に入る
            _, self._pre_topic = self._completed_messages(records, self.topic_idx + 1)

    @staticmethod
    def _completed_messages(records: list[dict[str, Any]], topic_no: int) -> tuple[list[Message], list[Message]]:
        """(議題 topic_no の文脈, まだ議題が始まっていない割り込み) を返す。

        - checkpoint から次の checkpoint までが1つのフェーズ。途中で中断されたフェーズの発言は、
          再開後にそのフェーズをやり直すので捨てる。中断の印は interrupted / aborted のほか、
          kill -9 などで印が残らなかった場合に備えて resumed（再開した印）も使う
        - ユーザーの割り込みと /ask のやり取りは中断されても残す
        - 議題が始まる前（topic: null）の割り込みは、その次に始まった議題の文脈に入る
        """
        out: list[Message] = []
        pending: list[Message] = []
        pre: list[Message] = []
        for r in records:
            ev = r.get("event")
            if ev == "checkpoint":
                out.extend(pending)
                pending = []
                continue
            if ev in ("interrupted", "aborted", "resumed"):
                pending = []
                continue
            if ev == "topic_start":
                if r.get("topic") == topic_no:
                    out.extend(pre)
                pre = []
                continue
            if r.get("kind") != "message" or r.get("round") in ("summary", "intro"):
                continue
            to = r.get("to") if r.get("to") != "user" else None
            m = Message(r["speaker"], r["round"], r.get("context_text") or r["text"], r["seq"], to=to)
            if r.get("topic") is None:
                if r.get("round") == "interjection":
                    pre.append(m)
                continue
            if r.get("topic") != topic_no:
                continue
            if r.get("round") in ("interjection", "approval"):
                out.append(m)
            else:
                pending.append(m)
        return sorted(out, key=lambda m: m.seq), pre
