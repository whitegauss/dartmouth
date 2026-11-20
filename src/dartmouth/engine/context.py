"""各ターンでエージェントに渡すプロンプトを組み立てる。

渡すのは「会議の目的＋現在の議事録＋今の議題の発言」だけ。過去の議題の発言は渡さない。
share_scope=public_only の参加者には、非公開の印がついた情報を取り除いてから渡す。

非公開の印:
- <private> … </private> で囲んだ部分（閉じタグがなければ末尾まで）
- <!-- private --> … <!-- /private --> で囲んだ部分（同上。Markdown の資料で表示を崩さない書き方）
- 行頭が [private] または [非公開] の行
- config の meeting.context で private: true にした資料
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Iterable

from ..config import Config

REDACTED = "［非公開情報のため省略］"
_TAG = re.compile(r"<private>.*?(?:</private>|\Z)", re.S | re.I)
# 行頭に限らず、[非公開] / [private] から行末までを伏せる（「- [D-001] [非公開] …」「却下: [非公開] …」など）
_LINE = re.compile(r"\[(?:private|非公開)\][^\n]*", re.I)


_COMMENT = re.compile(r"<!--\s*private\s*-->.*?(?:<!--\s*/private\s*-->|\Z)", re.S | re.I)


def redact(text: str) -> str:
    text = _COMMENT.sub(REDACTED, text)
    text = _TAG.sub(REDACTED, text)
    return _LINE.sub(REDACTED, text)


@dataclass
class Message:
    """今の議題の中の1発言（エンジンが保持し、プロンプトに入れる）。"""

    speaker: str
    round: str
    text: str
    seq: int
    to: str | None = None  # /ask の宛先


def default_prompts_dir() -> Path:
    here = Path(__file__).resolve()
    for cand in (Path.cwd() / "prompts", here.parents[3] / "prompts"):
        if (cand / "intro.md").exists():
            return cand
    raise FileNotFoundError("prompts directory not found; set prompts_dir in config")


class PromptLibrary:
    """prompts/ 以下のテンプレート。コードを変えずに文言を調整できる。"""

    def __init__(self, root: Path):
        self.root = root

    def get(self, name: str, fallback: str | None = None) -> str:
        p = self.root / f"{name}.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
        if fallback is not None:
            return self.get(fallback)
        raise FileNotFoundError(p)

    def role(self, role: str) -> str:
        return self.get(role, fallback="member")


class ContextBuilder:
    def __init__(self, cfg: Config, prompts: PromptLibrary, display_names: dict[str, str]):
        self.cfg = cfg
        self.prompts = prompts
        self.names = display_names

    def _materials(self, public_only: bool, round_name: str | None = None) -> list[str]:
        out = []
        for item in self.cfg.meeting.context:
            if item.private and public_only:
                continue
            if item.rounds is not None and round_name is not None and round_name not in item.rounds:
                continue  # このラウンドには渡さない資料（大きな資料を毎ターン送らないため）
            body = item.text if item.text is not None else Path(item.file).read_text(encoding="utf-8")  # type: ignore[arg-type]
            label = item.label or (Path(item.file).name if item.file else "資料")
            out.append(f"### {label}{'（非公開）' if item.private else ''}\n{body.strip()}")
        return out

    def build(
        self,
        *,
        participant: str,
        round_template: str,
        role: str | None = None,
        round_name: str | None = None,
        materials: list[dict] | None = None,
        topic_no: int | None,
        topic_title: str | None,
        topic_premise: str,
        messages: Iterable[Message],
        minutes_md: str,
        members: list[str],
        extra: dict[str, str] | None = None,
    ) -> str:
        p = self.cfg.participants[participant]
        public_only = p.share_scope == "public_only"
        clean = redact if public_only else (lambda s: s)
        vars_ = {
            "name": participant,
            "display_name": self.names[participant],
            "meeting_name": self.cfg.meeting.name,
            "purpose": self.cfg.meeting.purpose or "（未設定）",
            "language": self.cfg.meeting.language,
            "max_chars": str(self.cfg.budget.max_chars_per_message),
            "topic_title": topic_title or "",
            "chair": self.names.get(self._chair_for(topic_no), ""),
            **(extra or {}),
        }
        if public_only:
            # 却下理由・/ask の質問・議題名などテンプレートに差し込む値も伏せる
            vars_ = {k: (redact(v) if isinstance(v, str) else v) for k, v in vars_.items()}
        sec: list[str] = []
        stance = Template(self.prompts.role(role or p.role)).safe_substitute(vars_).strip()
        persona = p.persona_text()
        if persona:
            stance += "\n\n## あなたのペルソナ（できること・できないこと・得意なこと）\n" + clean(persona)
        sec.append("# あなたの立場\n" + stance)
        roster = "\n".join(
            f"- {self.names[m]}（id: {m}, 役割: {self.cfg.participants[m].role}）" + ("  ← あなた" if m == participant else "")
            for m in members
        )
        sec.append(
            f"# 会議\n- 会議名: {self.cfg.meeting.name}\n- 目的: {clean(vars_['purpose'])}\n"
            f"- 参加者:\n{roster}\n- ユーザー（id: user）: 議題の提示・割り込み・承認を行う人間"
        )
        parts: dict[str, int] = {}
        mats = self._materials(public_only, round_name)
        if mats:
            sec.append("# 前提資料\n" + clean("\n\n".join(mats)))
            parts["background"] = len(sec[-1])
        sec.append("# 現在の議事録\n" + clean(minutes_md.strip()))
        parts["minutes"] = len(sec[-1])
        mats = self._materials_section(materials or [], public_only, clean, round_name, messages)
        if mats:
            sec.append(mats)
            parts["materials"] = len(mats)
        if topic_no is not None:
            t = f"# 今の議題\n議題{topic_no}: {clean(topic_title or '')}"
            if topic_premise.strip():
                t += f"\n前提条件:\n{clean(topic_premise.strip())}"
            sec.append(t)
            lines = []
            for m in messages:
                who = "ユーザー" if m.speaker == "user" else self.names.get(m.speaker, m.speaker)
                tag = {"independent": "独立回答", "critique": "相互批判", "interjection": "割り込み",
                       "approval": "差し戻し", "summary": "議長まとめ"}.get(m.round, m.round)
                to = f" → {self.names.get(m.to, m.to)}" if m.to else ""
                lines.append(f"## [{who}{to}]（{tag}）\n{clean(m.text.strip())}")
            sec.append("# この議題のこれまでの発言\n" + ("\n\n".join(lines) if lines else "（まだありません）"))
            parts["messages"] = len(sec[-1])
        sec.append("# 今回の指示\n" + Template(round_template).safe_substitute(vars_).strip())
        rules = [
            f"- {self.cfg.meeting.language}で答える。今回の指示にある長さの目安を守る（{self.cfg.budget.max_chars_per_message}字を超えた分は切り捨てられる）。",
            "- 会議中は読み取り専用。ファイルの書き込みやコマンドの実行はしない。",
            "- 他の参加者の名前を騙ったり、他者の発言を書いたりしない。自分の発言だけを書く。",
            "- 確認の質問・作業計画の提示・承認の依頼はしない。足りない情報は仮定を明記して、そのまま回答を書く。",
        ]
        rules += self._tool_rules(participant, round_name)
        if self.cfg.knowledge is not None and round_name in ("independent", "critique", "interjection"):
            rules.append("- 「関連資料」にない情報が要るときは、発言の最後に「【資料請求】検索語」を1行（最大2行）書く。"
                         "資料庫（文書・コード・過去の議事録）から探して、次の発言者から関連資料に加わる。資料を引用するときは `パス:行` を添える。")
        if public_only:
            rules.append("- 省略された非公開情報を推測で補わない。")
        else:
            rules.append("- <private>…</private> や <!-- private -->…<!-- /private --> で囲まれた情報、「（非公開）」の資料に触れる場合は、その部分を <private>…</private> で囲んで書く。")
        sec.append("# ルール\n" + "\n".join(rules))
        out = "\n\n".join(sec) + "\n"
        parts["other"] = len(out) - sum(parts.values())
        self.last_parts = parts  # プロンプトの内訳（文字数）。dm stats で資料由来の割合を見るため
        return out

    def _materials_section(self, materials: list[dict], public_only: bool, clean, round_name: str | None = None,
                           messages: Iterable[Message] = ()) -> str:
        """資料庫から選んだ抜粋。文字数の上限を超えたら、ローカル LLM の要約（あれば）か見出しだけにする。

        相互批判とまとめでは上限を半分にし、発言で引用された資料と、資料請求で足した資料を先に渡す
        （独立回答で一度全部読んでいるので、毎ターン同じ資料を送り直さない）。
        """
        if not materials:
            return ""
        limit = self.cfg.knowledge.max_chars if self.cfg.knowledge else 8000
        if round_name in ("critique", "summary"):
            limit //= 2
            said = "\n".join(m.text for m in messages)
            cited = lambda m: m["path"] in said  # noqa: E731
            materials = sorted(materials, key=lambda m: (not cited(m), not m.get("requested"), 0))
        out, used = [], 0
        for m in materials:
            if m.get("private") and public_only:
                continue
            head = f"### `{m['path']}:{m['start']}-{m['end']}`"
            body = m["text"]
            if used + len(body) > limit:
                body = m.get("short") or ""
                if not body or used + len(body) > limit:
                    out.append(head + "（上限のため本文は省略。必要なら【資料請求】で絞り込む）")
                    continue
                head += "（ローカル LLM による要約）"
            used += len(body)
            out.append(f"{head}\n```\n{clean(body)}\n```")
        return "# 関連資料（資料庫から選んだ抜粋）\n" + "\n\n".join(out)

    def _tool_rules(self, participant: str, round_name: str | None) -> list[str]:
        """このラウンドでファイルを読めるかどうかを伝える（伝えないと、読めても読まずに答える）。"""
        p = self.cfg.participants[participant]
        opts = p.options()
        cwd = opts.get("cwd")
        if not cwd or p.adapter not in ("claude_code", "antigravity_cli") or round_name is None:
            return []
        rounds = opts.get("tools_rounds", ["independent", "interjection"])
        if round_name in rounds:
            return [f"- このラウンドでは、作業ディレクトリ {cwd} のファイルを読める（読み取りのみ）。コードや設定の挙動を根拠にするときは、"
                    "実際に開いて確かめ、`ファイル名:行番号` を添える。開いていないことは推測と明記する。"]
        return ["- このラウンドではファイルを読めない。コードの挙動は、これまでの発言で `ファイル名:行番号` つきで引用されたものだけを根拠にし、"
                "それ以外は推測と明記する。"]

    def _chair_for(self, topic_no: int | None) -> str:
        if topic_no is not None and 1 <= topic_no <= len(self.cfg.meeting.topics):
            c = self.cfg.meeting.topics[topic_no - 1].chair
            if c:
                return c
        return self.cfg.chair
