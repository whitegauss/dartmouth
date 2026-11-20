"""表示側で共通に使う小物。"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from typing import Any

import yaml

from ..engine import events as E
from ..storage.minutes import Proposal

PALETTE = ["#d97757", "#4f8ef7", "#3fb950", "#c678dd", "#e5c07b", "#56b6c2", "#e06c75"]
ROUND_LABEL = {"intro": "自己紹介", "plan": "席決め", "independent": "独立回答", "critique": "相互批判", "summary": "まとめ",
               "approval": "承認", "interjection": "割り込み"}

EDIT_HEADER = """\
# 議長のまとめを修正してください。
# TUI: Ctrl+S で保存して承認 / Esc で閉じる（書きかけは自動保存され、次の /edit で続きから開きます）
# 簡易表示: エディタで保存して閉じると承認。中身を空にして保存すると承認を取りやめます。
"""
DRAFT = "edit_draft.yaml"


def colors_for(engine) -> dict[str, str]:
    out = {"user": "#ffffff", "system": "#888888"}
    for i, (n, p) in enumerate(engine.cfg.participants.items()):
        out[n] = p.color or PALETTE[i % len(PALETTE)]
    return out


def proposal_to_yaml(p: dict[str, Any]) -> str:
    return EDIT_HEADER + yaml.safe_dump(Proposal.model_validate(p).model_dump(), allow_unicode=True, sort_keys=False, width=1000)


def yaml_to_proposal(text: str) -> dict[str, Any] | None:
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")).strip()
    if not body:
        return None
    return Proposal.model_validate(yaml.safe_load(body)).model_dump()


def draft_path(meeting_dir, topic: int):
    from pathlib import Path

    return Path(meeting_dir) / f"edit_draft_topic{topic}.yaml"


def load_draft(meeting_dir, topic: int, proposal: dict[str, Any]) -> str:
    """書きかけの下書きがあればそれを、なければ議長のまとめを返す。"""
    p = draft_path(meeting_dir, topic) if meeting_dir else None
    if p is not None and p.exists():
        return p.read_text(encoding="utf-8")
    return proposal_to_yaml(proposal)


def save_draft(meeting_dir, topic: int, text: str) -> None:
    if not meeting_dir:
        return
    p = draft_path(meeting_dir, topic)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def clear_draft(meeting_dir, topic: int) -> None:
    if meeting_dir:
        draft_path(meeting_dir, topic).unlink(missing_ok=True)


def edit_text(initial: str, suffix: str = ".yaml", path=None) -> str:
    """外部エディタで編集した結果を返す（簡易表示用）。

    $VISUAL / $EDITOR がなければ、画面下に操作方法が出る nano を使う（vi は操作に慣れが要るため最後の手段）。
    path を渡すと、そのファイル（会議ディレクトリの下書き）を直接編集するので、途中で落ちても内容が残る。
    """
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("nano" if _which("nano") else "vi")
    if path is None:
        fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="dartmouth-")
        os.close(fd)
        target, remove = tmp, True
    else:
        target, remove = str(path), False
    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(initial)
        subprocess.run([*shlex.split(editor), target], check=False)
        with open(target, encoding="utf-8") as fh:
            return fh.read()
    finally:
        if remove:
            os.unlink(target)


def _which(cmd: str) -> bool:
    from shutil import which

    return which(cmd) is not None


def format_proposal_md(p: dict[str, Any]) -> str:
    pr = Proposal.model_validate(p)
    out = []
    if pr.summary:
        out += [pr.summary.strip(), ""]
    # 承認の対象（決定事項案）を先に、その根拠（合意点・対立点）をあとに
    if pr.decisions:
        out += ["**決定事項案**"] + [f"- {x}" for x in pr.decisions] + [""]
    for label, items in (("未決事項", pr.open_questions), ("ToDo", pr.todos)):
        if items:
            out += [f"**{label}**"] + [f"- {x.text}" + (f"（担当: {x.owner}）" if x.owner else "") for x in items] + [""]
    for label, items in (("合意点", pr.agreements), ("対立点", pr.disagreements)):
        if items:
            out += [f"**{label}**"] + [f"- {x}" for x in items] + [""]
    if pr.extension_requested:
        out += ["**延長の申し出**（全議題のあと、予算が残っていれば延長。取り消すなら /edit で extension_requested を false に）",
                f"理由: {pr.extension_reason or '（記載なし）'}", "", "申し送り:", pr.handover.strip(), ""]
    return "\n".join(out).strip()


class ArrivalBuffer:
    """display: all_at_once のとき、独立回答ラウンドの発言をそろってから出す。"""

    def __init__(self, mode: str):
        self.mode = mode
        self.buf: list[E.Event] = []
        self.holding = False

    def feed(self, ev: E.Event) -> list[E.Event]:
        if self.mode != "all_at_once":
            return [ev]
        if ev.type == E.ROUND_START and ev.data.get("round") == "independent":
            self.holding = True
            return [ev]
        if self.holding and ev.type == E.SPEECH_END and ev.data.get("round") == "independent" and ev.data.get("status") == "ok":
            self.buf.append(ev)  # 失敗・タイムアウトはすぐ見せる
            return []
        if self.holding and (
            (ev.type == E.ROUND_END and ev.data.get("round") == "independent") or ev.type in (E.PAUSED, E.MEETING_END)
        ):
            # ラウンドが終わったとき、または一時停止・終了で止まったときは、溜めていた回答を出す
            self.holding = ev.type == E.PAUSED
            out, self.buf = self.buf + [ev], []
            return out
        return [ev]


def status_lines(st: dict[str, Any], names: dict[str, str]) -> list[str]:
    lines = []
    if st.get("topic_no"):
        lines.append(f"議題 {st['topic_no']}/{st['topics_total']} {st['phase_label']}")
        if st["phase"] == "critique":
            lines[-1] += f" {min(st['critique_done'] + 1, max(st['critique_rounds'], 1))}/{st['critique_rounds']}"
    else:
        lines.append(st["phase_label"])
    for n, f in st.get("inflight", {}).items():
        lines.append(f"{names.get(n, n)} 応答中… {f['elapsed_s']:.0f}秒")
    if st.get("absent"):
        lines.append("欠席: " + ", ".join(names.get(n, n) for n in st["absent"]))
    w = st.get("waiting")
    if w == "approval":
        lines.append("承認待ち: /approve /edit /reject")
    elif w == "pause":
        p = st.get("paused") or {}
        lines.append(f"一時停止: {names.get(p.get('participant'), '')} 失敗")
        lines.append("/retry /continue /abort")
    elif w == "next":
        lines.append("/next で次へ")
    elif w == "topic":
        lines.append("/topic で議題を追加")
    lines.append(f"予算 残り {st['calls_left']}回")
    return lines
