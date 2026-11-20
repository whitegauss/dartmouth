from __future__ import annotations

import yaml

from dartmouth.config import load_config
from dartmouth.topics_file import mark_topics_done

from .conftest import ROOT, run_auto

SRC = """# 会議の設定（コメントは残る）
meeting:
  name: t
  topics:
    - 文字列の議題
    - title: 前提つきの議題   # 行末のコメント
      premise: 予算は1万円
    - まだ残る議題
participants:
  chair: {adapter: dummy, role: chair}
"""


def test_mark_topics_done_keeps_comments(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(SRC)
    moved = mark_topics_done(p, ["文字列の議題", "前提つきの議題", "設定にない議題"], "2026-11-18-t")
    assert moved == ["文字列の議題", "前提つきの議題"]
    text = p.read_text()
    assert "# 会議の設定（コメントは残る）" in text and (tmp_path / "c.yaml.bak").read_text() == SRC
    assert "\n    - まだ残る議題" in text  # 元の字下げを保つ
    cfg = load_config(p)
    assert [t.title for t in cfg.meeting.topics] == ["まだ残る議題"]
    assert [(d.title, d.meeting) for d in cfg.meeting.done_topics] == [("文字列の議題", "2026-11-18-t"), ("前提つきの議題", "2026-11-18-t")]
    assert yaml.safe_load(text)["meeting"]["done_topics"][1]["premise"] == "予算は1万円"
    assert mark_topics_done(p, ["設定にない議題"], "x") == []


async def test_approved_topics_move_out_of_source_config(tmp_path, new_session):
    p = tmp_path / "src.yaml"
    p.write_text(f"""meeting:
  name: src
  intro: false
  topics: [議題A, 議題B]
meetings_dir: {tmp_path / 'meetings'}
prompts_dir: {ROOT / 'prompts'}
participants:
  claude: {{adapter: dummy, role: chair}}
  gemini: {{adapter: dummy, role: researcher}}
""")
    s = new_session(load_config(p), source_config=p)

    def on_approval(engine, ev):
        engine.approve() if ev.data["topic"] == 1 else engine.reject("やり直し") if not getattr(engine, "_r", False) and setattr(engine, "_r", True) is None else engine.approve()

    await run_auto(s, on_approval)
    cfg = load_config(p)
    assert cfg.meeting.topics == [] and [d.title for d in cfg.meeting.done_topics] == ["議題A", "議題B"]
    assert (s.dir / "source_config").read_text() == str(p.resolve())
