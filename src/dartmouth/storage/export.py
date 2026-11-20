"""議事録から要件定義書の下書き（Markdown）を作る。LLM は使わず、議事録の内容を並べ替えるだけ。"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..config import Config
from .minutes import Minutes


def render_requirements_draft(m: Minutes, cfg: Config, stats: dict[str, dict[str, Any]] | None = None) -> str:
    out = [f"# {cfg.meeting.name} 要件定義（下書き）", "", f"{date.today().isoformat()} · Dartmouth の議事録から自動生成", ""]
    out += ["## 目的", cfg.meeting.purpose or "（未設定）", ""]
    out += ["## 要件（承認された決定事項）", "", "| ID | 要件 | 出典 |", "|---|---|---|"]
    for i, d in enumerate(m.decisions, 1):
        out.append(f"| R-{i:02d} | {d.text.replace('|', '\\|')} | {d.id}（議題{d.topic}） |")
    if not m.decisions:
        out.append("| — | （承認された決定事項なし） | — |")
    out += ["", "## 議題ごとの背景"]
    for idx in sorted(m.topics):
        s = m.topics[idx]
        out += [f"### {idx}. {s.title}", s.summary.strip() or "（要約なし）", ""]
        if s.disagreements:
            out += ["検討した対立案:"] + [f"- {x}" for x in s.disagreements] + [""]
    out += ["## 未決事項"]
    out += [f"- [ ] {q.text}{f'（担当: {q.owner}）' if q.owner else ''}" for q in m.questions] or ["- （なし）"]
    out += ["", "## ToDo"]
    out += [f"- [ ] {t.text}{f'（担当: {t.owner}）' if t.owner else ''}" for t in m.todos] or ["- （なし）"]
    if m.pending:
        out += ["", "## 承認待ちの提案（要件には未反映）"]
        for idx in sorted(m.pending):
            title, p = m.pending[idx]
            out += [f"### {idx}. {title}"] + [f"- {d}" for d in p.decisions]
    if m.final_summary:
        out += ["", "## 会議の総括", m.final_summary.strip()]
    if stats:
        out += ["", "## 付録: 参加者ごとの呼び出し実績", "", "| 参加者 | 呼び出し | 成功 | 所要時間 | 文字数 | トークン |", "|---|---|---|---|---|---|"]
        for n, s in stats.items():
            out.append(f"| {n} | {s['calls']} | {s.get('ok', 0)} | {s['elapsed_ms'] / 1000:.0f}秒 | {s['chars']} | {s['tokens'] or '—'} |")
    return "\n".join(out).rstrip() + "\n"
