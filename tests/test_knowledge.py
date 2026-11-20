from __future__ import annotations

import os
import time

from dartmouth.engine import events as E
from dartmouth.knowledge.chunker import chunk_text
from dartmouth.knowledge.embedders import HashEmbedder
from dartmouth.knowledge.index import KnowledgeBase

from .conftest import make_config, run_auto


def _docs(tmp_path):
    d = tmp_path / "docs"; d.mkdir(exist_ok=True)
    if (d / "storage.md").exists():
        return _sources(tmp_path)
    (d / "storage.md").write_text("# ストレージ\nSSD は 2TB にする。容量の見積もりは月 4GB の増加。\n\n# ネットワーク\nTailscale で中継する。\n")
    (d / "quota.md").write_text("# 利用枠\nClaude の 5時間枠に達すると呼び出しが失敗する。\n")
    code = tmp_path / "code"; code.mkdir(exist_ok=True)
    (code / "flow.py").write_text("import os\n\n\ndef tools_rounds_default():\n    return ['independent', 'interjection']\n\n\nclass Engine:\n    pass\n")
    (tmp_path / "secret.md").write_text("# 健康\n血圧は135/88\n")
    return _sources(tmp_path)


def _sources(tmp_path):
    d, code = tmp_path / "docs", tmp_path / "code"
    return [{"path": str(d), "glob": "**/*.md", "label": "docs"},
            {"path": str(code), "glob": "**/*.py", "label": "src"},
            {"path": str(tmp_path), "glob": "secret.md", "label": "private", "private": True}]


def test_chunker_keeps_line_numbers():
    chunks = chunk_text("a\n\ndef f():\n    return 1\n\nclass C:\n    pass\n", ".py")
    starts = [c.start for c in chunks]
    assert 3 in starts and 6 in starts and all(c.end >= c.start for c in chunks)
    md = chunk_text("# A\nx\n# B\ny\n", ".md")
    assert [(c.start, c.end) for c in md] == [(1, 2), (3, 4)]


def test_index_search_and_incremental_update(tmp_path):
    kb = KnowledgeBase(tmp_path / "idx", HashEmbedder())
    stats = kb.update(_docs(tmp_path))
    assert stats["embedded"] == stats["chunks"] > 0
    hits = kb.search("SSD の容量", 2)
    assert hits[0].path == "docs/storage.md" and hits[0].start == 1
    assert kb.search("tools_rounds の既定値", 1)[0].path == "src/flow.py"
    # 非公開の資料は public_only には渡さない
    assert all(not c.private for c in kb.search("血圧", 10, allow_private=False))
    assert any(c.private for c in kb.search("血圧", 10, allow_private=True))
    # 変わっていなければ埋め込み直さない。変わったファイルだけ埋め込み直す
    assert kb.update(_docs(tmp_path))["embedded"] == 0
    t = time.time() + 5
    os.utime(tmp_path / "docs" / "quota.md", (t, t))
    s3 = kb.update(_docs(tmp_path))
    assert 0 < s3["embedded"] < s3["chunks"]
    # 保存した索引を読み直せる
    kb2 = KnowledgeBase(tmp_path / "idx", HashEmbedder()).load()
    assert len(kb2.chunks) == len(kb.chunks) and kb2.get(kb.chunks[0].id)


async def test_meeting_gets_materials_and_handles_requests(tmp_path, new_session):
    cfg = make_config(tmp_path, {
        "chair": {"adapter": "dummy", "role": "chair"},
        "claude": {"adapter": "dummy", "role": "engineer", "replies": ["案です\n【資料請求】利用枠 5時間"]},
        "critic": {"adapter": "dummy", "role": "third_opinion", "share_scope": "public_only"},
    }, topics=[{"title": "ストレージの容量", "premise": "SSD"}], intro=False, chair_participates=False, agent_tools=False)
    from dartmouth.config import KnowledgeConfig

    cfg.knowledge = KnowledgeConfig.model_validate({"sources": _docs(tmp_path), "embedder": {"backend": "hash"},
                                                     "index_dir": str(tmp_path / "idx"), "top_k": 2, "request_k": 1})
    s = new_session(cfg)
    assert s.engine.kb is not None
    d = await run_auto(s)
    recs = d.records()
    first = next(r for r in recs if r.get("event") == "kb_materials")
    assert any("storage.md" in i for i in first["ids"])
    look = [r for r in recs if r.get("event") == "kb_lookup"]
    assert look and look[0]["by"] == "claude" and any("quota.md" in i for i in look[0]["ids"])
    # 独立回答から関連資料が渡り、資料請求の結果は次の発言者から見える
    indep = [c for c in s.adapters["critic"].prompts if c.round == "independent"][0].prompt
    assert "# 関連資料" in indep and "docs/storage.md:1-" in indep and "【資料請求】" in indep
    crit = [c for c in s.adapters["critic"].prompts if c.round == "critique"][-1].prompt
    assert "docs/quota.md" in crit
    # 非公開の資料は public_only の参加者に渡らない
    assert all("血圧" not in c.prompt for c in s.adapters["critic"].prompts)


def test_agent_tools_false_strips_tools_and_cwd(tmp_path):
    from dartmouth.config import Config
    from dartmouth.session import build_adapters

    cfg = Config.model_validate({"meeting": {"agent_tools": False}, "participants": {
        "a": {"adapter": "claude_code", "role": "chair", "cwd": "/code"}}})
    a = build_adapters(cfg)["a"]
    assert a.options.get("tools_rounds") == [] and "cwd" not in a.options
