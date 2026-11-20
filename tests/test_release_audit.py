from pathlib import Path

from dartmouth.release_audit import arrange_overlay, audit

GITIGNORE = "config.yaml\nagents.yaml\n*.local.yaml\nexports/\nlocal/\nmeetings/*\n"


def _tree(tmp_path: Path) -> Path:
    t = tmp_path / "tree"
    (t / ".github" / "workflows").mkdir(parents=True)
    (t / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    ov = t / ".github" / "public"
    for k in ("normal", "abnormal"):
        (ov / "data" / "sample" / k).mkdir(parents=True)
        (ov / "data" / "sample" / k / "transcript.jsonl").write_text("{}\n")
    (ov / "config.example.yaml").write_text("participants:\n  chair-a: {adapter: dummy, model: dummy}\n")
    (t / ".gitignore").write_text(GITIGNORE)
    (t / "README.md").write_text("hello\n")
    return arrange_overlay(t)


def _bad(items):
    return {i.key for i in items if i.auto and i.ok is False}


def test_clean_tree_passes_and_overlay_is_moved(tmp_path):
    t = _tree(tmp_path)
    assert (t / "config.example.yaml").exists() and (t / "data" / "sample" / "normal").is_dir()
    assert not (t / ".github").exists()  # workflows も public も消える
    items = audit(t, "release-v0.1.0", "secretword\n", strict=True)
    assert _bad(items) == set() and len(items) == 12 and sum(1 for i in items if i.auto) >= 9


def test_each_layer_detects_problems(tmp_path):
    t = _tree(tmp_path)
    (t / "config.yaml").write_text("x: 1\n")                                         # C1
    (t / "config.example.yaml").write_text("participants:\n  a: {model: " + "gemini-4-x}\n")  # C3
    (t / ".gitignore").write_text("config.yaml\n")                                   # C2
    (t / "meetings").mkdir()
    (t / "meetings" / "m1").mkdir()
    (t / "meetings" / "m1" / "transcript.jsonl").write_text("{}\n")                 # D1
    (t / "notes-secretword.md").write_text("mail " + "someone@" + "mail.co.jp\n")    # M2・N1
    (t / "doc.md").write_text("the secretword is here\n")                            # N2
    import shutil
    shutil.rmtree(t / "data" / "sample" / "abnormal")                                # D2
    bad = _bad(audit(t, "v1", "secretword\n", strict=True))                          # M1
    assert bad == {"M1", "M2", "N1", "N2", "C1", "C2", "C3", "D1", "D2"}


def test_strict_requires_terms_unless_allowed(tmp_path):
    t = _tree(tmp_path)
    assert "N2" in _bad(audit(t, None, "", strict=True))
    assert "N2" not in _bad(audit(t, None, "", strict=True, allow_missing=True))
    assert "N2" not in _bad(audit(t, None, "", strict=False))
