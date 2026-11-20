import json
from pathlib import Path

from dartmouth.blind import blind_text, collect_identities, run as run_blind
from dartmouth.persona_lint import check_file, estimate_tokens, lint

ROOT = Path(__file__).resolve().parents[1]
GOOD = """---
name: テスト役
role: member
summary: テスト用
template: persona-v1
---
## 問い
問い。

## 判断基準
- 基準

## 必須検査
該当なし

## 触れない範囲
実装

## 出力
【結論】【根拠】【未決】の3つで書く。
"""


def test_repo_personas_pass_lint():
    # 実験で使う4名は標準テンプレートに移行済みで、検査に通る
    assert lint([ROOT / "prompts" / "personas"]) == 0
    for pid in ("manager", "engineer", "critic", "quota"):
        info, errs = check_file(ROOT / "prompts" / "personas" / f"{pid}.md")
        assert info["template"] == "persona-v1" and errs == []


def test_lint_detects_missing_block_and_token_overflow(tmp_path):
    (tmp_path / "ok.md").write_text(GOOD)
    assert lint([tmp_path]) == 0
    (tmp_path / "bad.md").write_text(GOOD.replace("## 触れない範囲\n実装\n\n", ""))
    _, errs = check_file(tmp_path / "bad.md")
    assert any("触れない範囲" in e for e in errs)
    assert lint([tmp_path]) == 1
    (tmp_path / "bad.md").unlink()
    (tmp_path / "long.md").write_text(GOOD.replace("問い。", "あ" * 400))
    _, errs = check_file(tmp_path / "long.md")
    assert any("上限" in e for e in errs)
    # 未移行（template なし）のファイルは集計だけで、違反にしない
    (tmp_path / "long.md").write_text("昔のペルソナ" * 100)
    assert lint([tmp_path]) == 0
    assert estimate_tokens("あいう") == 3 and estimate_tokens("abcdefgh") == 2


def test_blind_masks_models_personas_and_conditions(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    (d / "status.json").write_text(json.dumps({"participants": {"chair": "議長(Argon)", "manager": "マネージャー", "critic": "批判役"}}))
    text = "- [T-001] 条件Aは Opus と gemini-3.8-flash、条件 B は高エフォート。条件A/B共通で Sonnet High。（担当: manager）批判役の指摘。議長(Argon)がまとめた\n"
    (d / "minutes.md").write_text(text)
    ids = collect_identities(d, None)
    out, residue = blind_text(text, ids)
    assert residue == []
    for w in ("Opus", "flash", "gemini", "manager", "批判役", "Argon", "条件A", "条件 B", "高エフォート", "/B", "High"):
        assert w not in out
    assert "参加者1" in out and "参加者2" in out and "議長" in out and "[条件]" in out
    o = tmp_path / "out.md"
    assert run_blind(str(d), str(o), str(tmp_path / "none"), []) == 0 and o.read_text() == out


def test_blind_normalizes_fullwidth_and_stops_when_identifier_remains(tmp_path, monkeypatch):
    import dartmouth.blind as b

    out, residue = blind_text("Ｏｐｕｓ と ＧＰＴ", {})
    assert residue == [] and "Opus" not in out and "GPT" not in out
    assert b.find_residue("参加者1 と Sonnet", {"critic": "参加者1"}) == ["sonnet"]
    # 自己検査で識別子が残れば、何も書き出さずに 1 で止まる
    d = tmp_path / "m"
    d.mkdir()
    (d / "minutes.md").write_text("Sonnet の案\n")
    monkeypatch.setattr(b, "blind_text", lambda t, ids, extra=None: (t, b.find_residue(t, ids, extra)))
    o = tmp_path / "out.md"
    assert run_blind(str(d), str(o), None, []) == 1 and not o.exists()


# ---------------------------------------------------------------- leakscan（公開前の個人情報の検査）

def test_leakscan_generic_patterns_positive_and_negative():
    from dartmouth.leakscan import PATTERNS, scan_text

    # 検出される見本は、このファイル自体が検査に引っかからないよう分けて組み立てる
    H, U = "/ho" + "me/", "ali" + "ce"
    bad = {"home-path": f"見て {H}{U}/notes.md", "email": f"連絡は {U}@" + "mail.co.jp まで",
           "api-key": 'KEY = "sk-ant-' + "a" * 30 + '"', "private-key": "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
           "bearer": "Authorization: Bearer " + "x" * 30, "tailscale-ip": "接続先 100." + "70.1.2",
           "windows-home": "C:" + "\\Users\\" + U + "\\Desktop"}
    for rule, line in bad.items():
        hits, _ = scan_text("f", line, PATTERNS)
        assert [h.rule for h in hits] == [rule], (rule, line)
    good = ["/home/runner/work", "/home/<user>/x", "noreply@anthropic.com", "a@example.com", "git@github.com:o/r.git", "100.64.0.0/10",
            "sk-short", "Bearer token", "192.168.1.5"]
    for line in good:
        assert scan_text("f", line, PATTERNS)[0] == [], line


def test_leakscan_terms_word_boundary_ignore_and_no_echo(capsys):
    from dartmouth.leakscan import parse_terms, run

    terms = "# コメント\nargon\nほげプラン\nre:demo-[a-z]+\n"
    rules = parse_terms(terms)
    assert [r[0] for r in rules] == ["terms#2", "terms#3", "terms#4"]
    files = [("a.md", "Argon が議長\nargonaut は別\nほげプランの相談\ndemo-storage を作る\n議長は ARGON  leakscan:ignore\n")]
    assert run(files, terms, require_terms=True, max_ignores=5) == 1
    out = capsys.readouterr().out
    assert "a.md:1: terms#2" in out and "a.md:2:" not in out and "a.md:3: terms#3" in out and "a.md:4: terms#4" in out
    assert "a.md:5" not in out and "除外 1 件" in out
    assert "argon" not in out.lower() and "ほげプラン" not in out  # 当たった語そのものはログに出さない
    # 固有語リストがないのに必須なら失敗（Secrets 不在で検査を飛ばさない）
    assert run([("b.md", "ok")], "", require_terms=True, max_ignores=5) == 2
    # leakscan:ignore の上限超過も失敗
    assert run([("c.md", "x leakscan:ignore\n" * 3)], "x\n", require_terms=False, max_ignores=2) == 1
    assert run([("d.md", "clean")], "argon\n", require_terms=True, max_ignores=5) == 0


def test_repository_has_no_generic_leaks():
    from dartmouth.leakscan import run, targets

    assert run(targets(False), "", require_terms=False, max_ignores=5) == 0
