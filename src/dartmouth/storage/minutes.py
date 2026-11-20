"""議事録（minutes.md）。

議事録の状態は transcript の承認イベントから再構築できる（＝ transcript が正）。
minutes.md はその状態を毎回丸ごと書き出したもの。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Item(BaseModel):
    text: str
    owner: str | None = None


class Proposal(BaseModel):
    """議長のまとめ（決まったJSON形式）。"""

    agreements: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    open_questions: list[Item] = Field(default_factory=list)
    todos: list[Item] = Field(default_factory=list)
    summary: str = ""
    # 延長の申し出。申し出るなら申し送り（handover）が必須
    extension_requested: bool = False
    extension_reason: str = ""
    handover: str = ""
    # 議長が付ける各席の貢献（1〜5）。ペルソナ×モデルの得意不得意を見つけるため。議事録には出さない
    seat_scores: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("summary", "handover", "extension_reason", mode="before")
    @classmethod
    def _unescape(cls, v: Any) -> Any:
        # モデルが改行を「\\n」の2文字で書いてくることがあるので、改行に直す
        if isinstance(v, str) and "\n" not in v and "\\n" in v:
            return v.replace("\\n", "\n")
        return v

    @field_validator("open_questions", "todos", mode="before")
    @classmethod
    def _items(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [{"text": x} if isinstance(x, str) else x for x in v]
        return v


PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agreements": {"type": "array", "items": {"type": "string"}},
        "disagreements": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "open_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "owner": {"type": "string"}},
                "required": ["text"],
            },
        },
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "owner": {"type": "string"}},
                "required": ["text"],
            },
        },
        "summary": {"type": "string"},
        "extension_requested": {"type": "boolean"},
        "extension_reason": {"type": "string"},
        "handover": {"type": "string"},
        "seat_scores": {"type": "array", "items": {"type": "object", "properties": {
            "persona": {"type": "string"}, "score": {"type": "integer"}, "note": {"type": "string"}},
            "required": ["persona", "score"]}},
    },
    "required": ["agreements", "disagreements", "decisions", "open_questions", "todos", "summary",
                 "extension_requested", "extension_reason", "handover"],
}


@dataclass
class Decision:
    id: str
    text: str
    topic: int
    approved_at: str


@dataclass
class Question:
    id: str
    text: str
    owner: str | None
    topic: int


@dataclass
class Todo:
    id: str
    text: str
    owner: str | None
    topic: int


@dataclass
class TopicSummary:
    title: str
    agreements: list[str]
    disagreements: list[str]
    summary: str


@dataclass
class Minutes:
    name: str
    purpose: str = ""
    decisions: list[Decision] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    todos: list[Todo] = field(default_factory=list)
    topics: dict[int, TopicSummary] = field(default_factory=dict)
    pending: dict[int, tuple[str, Proposal]] = field(default_factory=dict)
    final_summary: str = ""
    # 申し送り。議題番号 → {title, handover, reason, status}
    handovers: dict[int, dict[str, Any]] = field(default_factory=dict)

    def apply(self, topic: int, title: str, proposal: Proposal, ts: str) -> None:
        """承認されたまとめを反映する。決定事項に入るのはここを通ったものだけ。"""
        when = _fmt_ts(ts)
        for d in proposal.decisions:
            self.decisions.append(Decision(f"D-{len(self.decisions) + 1:03d}", d, topic, when))
        for q in proposal.open_questions:
            self.questions.append(Question(f"Q-{len(self.questions) + 1:03d}", q.text, q.owner, topic))
        for t in proposal.todos:
            self.todos.append(Todo(f"T-{len(self.todos) + 1:03d}", t.text, t.owner, topic))
        self.topics[topic] = TopicSummary(title, proposal.agreements, proposal.disagreements, proposal.summary)
        self.pending.pop(topic, None)

    def render_compact(self) -> str:
        """エージェントに毎ターン渡す短い版。決定事項・未決事項・ToDo だけで、議題ごとの要約や総括は入れない。"""
        out = ["## 決定事項"]
        out += [f"- [{d.id}] {d.text}" for d in self.decisions] or ["- （なし）"]
        out += ["## 未決事項"]
        out += [f"- [{q.id}] {q.text}{_owner(q.owner)}" for q in self.questions] or ["- （なし）"]
        out += ["## ToDo"]
        out += [f"- [{t.id}] {t.text}{_owner(t.owner)}" for t in self.todos] or ["- （なし）"]
        return "\n".join(out)

    def render(self) -> str:
        out = [f"# 議事録: {self.name}", ""]
        if self.purpose:
            out += [f"> 目的: {self.purpose}", ""]
        out += ["## 決定事項"]
        out += [f"- [{d.id}] {d.text}（議題{d.topic}、承認 {d.approved_at}）" for d in self.decisions] or ["- （なし）"]
        out += ["", "## 未決事項"]
        out += [f"- [{q.id}] {q.text}{_owner(q.owner)}" for q in self.questions] or ["- （なし）"]
        out += ["", "## ToDo"]
        out += [f"- [ ] [{t.id}] {t.text}{_owner(t.owner)}" for t in self.todos] or ["- （なし）"]
        out += ["", "## 議題ごとの要約"]
        for idx in sorted(self.topics):
            s = self.topics[idx]
            out += [f"### 議題{idx}: {s.title}"]
            if s.summary:
                out += [demote(s.summary)]
            if s.agreements:
                out += ["", "合意点:"] + [f"- {a}" for a in s.agreements]
            if s.disagreements:
                out += ["", "対立点:"] + [f"- {a}" for a in s.disagreements]
            out += [""]
        if self.pending:
            out += ["## 承認待ち（決定事項には未反映）"]
            for idx in sorted(self.pending):
                title, p = self.pending[idx]
                out += [f"### 議題{idx}: {title}"] + [f"- 決定事項案: {d}" for d in p.decisions] + [""]
        if self.handovers:
            out += ["## 申し送り"]
            for idx in sorted(self.handovers):
                h = self.handovers[idx]
                out += [f"### 議題{idx}: {h['title']}（{h['status']}）"]
                if h.get("reason"):
                    out += [f"延長の理由: {h['reason']}", ""]
                out += [demote(h["handover"]), ""]
        if self.final_summary:
            out += ["## 総括", demote(self.final_summary), ""]
        return "\n".join(out).rstrip() + "\n"

    def write(self, path: Path) -> None:
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(self.render(), encoding="utf-8")
        os.replace(tmp, path)


def demote(md: str, top: int = 4) -> str:
    """埋め込む Markdown の見出しを、いちばん浅いものが top レベルになるようにずらす（議事録の章立てを崩さない）。"""
    lines = md.strip().splitlines()

    def level(line: str) -> int:
        h = len(line) - len(line.lstrip("#"))
        return h if 0 < h <= 6 and line[h : h + 1] == " " else 0

    levels = [level(l) for l in lines if level(l)]
    if not levels:
        return "\n".join(lines)
    shift = top - min(levels)
    return "\n".join("#" * max(1, min(6, level(l) + shift)) + l[level(l):] if level(l) else l for l in lines)


HUMAN_OWNERS = {"user", "ユーザー", "本人", "me"}
EXTERNAL_OWNERS = {"external", "外部", "外部待ち"}


def owner_label(o: str | None) -> str:
    """担当の表示。本人か外部待ちのときだけ出す。AI がやるもの（参加者の id・空）は担当を出さない。"""
    o = (o or "").strip()
    return "本人" if o in HUMAN_OWNERS else "外部待ち" if o in EXTERNAL_OWNERS else ""


def _owner(o: str | None) -> str:
    lab = owner_label(o)
    return f"（担当: {lab}）" if lab else ""


def _fmt_ts(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


# ---------------------------------------------------------------------- 議事録の digest（次の会議に渡す短い版）

DIGEST_SECTIONS = {"決定事項": "decision", "未決事項": "question", "ToDo": "todo"}


def make_digest(markdown: str, limit: int = 3000, done: set[str] | None = None, item_chars: int = 110,
                index_max: int = 10) -> str:
    """議事録から「決定事項・未決事項・ToDo」だけを LLM なしで抜き出した短い版を作る。

    - 済んだ項目（done に入っている ID）は除く
    - 各項目は item_chars 字で切る（末尾の「（議題1、承認 …）」は外す）
    - limit を超えたら、未決と ToDo は全部残し、決定事項は新しい順に入るだけ入れ、残りは「ID＋先頭」の索引（最大 index_max 件）にする
    - 見出しが見つからないときは、末尾 limit 字を返す（呼び出し側で警告する）
    """
    import re

    from ..engine.context import redact

    done = done or set()
    markdown = redact(markdown)  # 非公開の部分は、切り詰める前に伏せ字にする（開きタグだけが残って漏れないように）
    sec: dict[str, list[tuple[str, str]]] = {"decision": [], "question": [], "todo": []}
    cur = None
    title = ""
    for line in markdown.splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        if line.startswith("## "):
            cur = DIGEST_SECTIONS.get(line[3:].strip())
            continue
        if cur is None:
            continue
        m = re.match(r"^- (?:\[[ x]\] )?\[([DQT]-\d+)\]\s*(.*)$", line)
        if not m or m.group(1) in done:
            continue
        text = re.sub(r"[（(]議題\d+[^）)]*承認[^）)]*[）)]\s*$", "", m.group(2)).strip()
        if len(text) > item_chars:
            text = text[: item_chars - 1] + "…"
        sec[cur].append((m.group(1), text))
    if not any(sec.values()):
        return markdown[-limit:]

    def block(name: str, items: list[tuple[str, str]]) -> str:
        return f"## {name}\n" + "\n".join(f"- [{i}] {t}" for i, t in items) + "\n" if items else ""

    head = f"# {title}（要点）\n" if title else ""

    def short(items: list[tuple[str, str]], n: int) -> list[tuple[str, str]]:
        return [(i, t if len(t) <= n else t[: n - 1] + "…") for i, t in items]

    rest = head + block("未決事項（未解決のもの）", sec["question"]) + block("ToDo（未完了のもの）", sec["todo"])
    if len(rest) > limit * 0.6:
        # 未決と ToDo は全部残すが、決定事項の場所を空けるため1件を短くする
        rest = head + block("未決事項（未解決のもの）", short(sec["question"], 50)) + block("ToDo（未完了のもの）", short(sec["todo"], 50))
    kept: list[tuple[str, str]] = []
    over: list[tuple[str, str]] = []
    for item in reversed(sec["decision"]):  # 新しい順
        if len(rest) + len(block("決定事項", kept + [item])) <= limit:
            kept.append(item)
        else:
            over.append(item)
    kept.sort(key=lambda x: int(x[0][2:]))
    out = head + block("決定事項", kept) + rest[len(head):]
    if over:
        over.sort(key=lambda x: int(x[0][2:]))
        idx = [f"{i} {t[:20]}…" for i, t in over[:index_max]]
        more = f" ほか{len(over) - index_max}件" if len(over) > index_max else ""
        out += "## 決定事項（入りきらなかったものの索引。全文は元の議事録にある）\n" + " / ".join(idx) + more + "\n"
    return out
