"""公開前の固定監査（4層12項目。うち10項目を機械判定し、残り2項目は手動サインオフ）。標準ライブラリだけで動く。

調べるのは「公開ミラーに実際に置かれる形」のツリー:
作業リポジトリの中身から .github/workflows を除き、.github/public/ の中身（見本の設定・合成サンプル）をルートに置いたもの。
項目の一覧と意味は docs/release-checklist.md、手動の項目は docs/release-signoff.md。

    python src/dartmouth/release_audit.py                         # HEAD から公開用ツリーを組んで調べる
    python src/dartmouth/release_audit.py --tag release-v0.1.0 --strict   # CI（sync-public）と同じ
    python src/dartmouth/release_audit.py --dir mirror            # 組み上がったツリーを調べる

機械判定の項目が1つでも ✗ なら終了コード1。当たった文字列そのものは出さない（CI のログは公開されるため）。
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:  # パッケージとしてでも、ファイルを直接実行しても読めるように
    from . import leakscan
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import leakscan  # type: ignore[no-redef]

VERSION = "1"
OVERLAY = ".github/public"
TAG_RE = re.compile(r"^release-v\d+\.\d+\.\d+$")
FORBIDDEN_FILES = ("config.yaml", "agents.yaml", "*.local.yaml", "pricing.yaml", "configs/experiment.yaml",
                   "leak_terms.txt", ".env", "*.env", "ledger.sqlite*", "*.bundle")
REQUIRED_IGNORES = ("config.yaml", "agents.yaml", "*.local.yaml", "exports/", "local/", "meetings/*")
MODEL_RE = re.compile(r"\b(?:claude|opus|sonnet|haiku|fable|gemini|argon|neon|hydrogen|flash|gpt|o\d)(?:[-.\w]*)", re.I)
SAMPLE_DIR = "data/sample"


@dataclass
class Item:
    key: str
    layer: str
    title: str
    auto: bool
    ok: bool | None = None  # None は手動（または対象外）
    note: str = ""


def build_publish_tree(dest: Path, ref: str = "HEAD") -> Path:
    """作業リポジトリの ref から、公開ミラーに置く形のツリーを dest に作る。"""
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "archive", ref], check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    return arrange_overlay(dest)


def build_from_worktree(dest: Path) -> Path:
    """コミット前の確認用: 作業ツリーのファイル（追跡中＋未追跡で .gitignore されていないもの）から組む。"""
    names = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], check=True, capture_output=True,
                           text=True).stdout.splitlines()
    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True,
                               text=True).stdout.strip())
    for n in names:
        src = root / n
        if src.is_file():
            (dest / n).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / n)
    return arrange_overlay(dest)


def arrange_overlay(tree: Path) -> Path:
    """.github/public/ の中身をルートへ移し、.github/workflows を消す（sync-public と同じ並べ替え）。"""
    ov = tree / OVERLAY
    if ov.is_dir():
        for p in ov.iterdir():
            target = tree / p.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(p), str(target))
        shutil.rmtree(ov)
    shutil.rmtree(tree / ".github" / "workflows", ignore_errors=True)
    if (tree / ".github").is_dir() and not any((tree / ".github").iterdir()):
        (tree / ".github").rmdir()
    return tree


def _files(tree: Path) -> list[Path]:
    return sorted(p for p in tree.rglob("*") if p.is_file() and ".git" not in p.relative_to(tree).parts)


def _texts(tree: Path) -> list[tuple[str, str]]:
    out = []
    for p in _files(tree):
        if p.suffix.lower() in leakscan.SKIP_SUFFIXES:
            continue
        try:
            out.append((str(p.relative_to(tree)), p.read_text(encoding="utf-8")))
        except UnicodeDecodeError:
            continue
    return out


def audit(tree: Path, tag: str | None, terms_text: str, strict: bool, allow_missing: bool = False) -> list[Item]:
    rels = [str(p.relative_to(tree)) for p in _files(tree)]
    texts = _texts(tree)
    terms = leakscan.parse_terms(terms_text)
    items: list[Item] = []

    # ---- 1. メタデータ
    if tag is None:
        items.append(Item("M1", "メタデータ", "タグ名が release-vX.Y.Z の形", True, None, "タグなし（CI ではタグ名を渡す）"))
    else:
        items.append(Item("M1", "メタデータ", "タグ名が release-vX.Y.Z の形", True, bool(TAG_RE.match(tag))))
    name_hits = [r for r in rels for _, pat in leakscan.PATTERNS + terms if pat.search(r)]
    items.append(Item("M2", "メタデータ", "ファイル名・ディレクトリ名に個人の情報・固有語がない", True, not name_hits,
                      f"{len(name_hits)} 件" if name_hits else ""))
    items.append(Item("M3", "メタデータ", "公開ミラーのコミット名義がプロジェクト共通名義になっている", False, None,
                      "手動（サインオフ 3）"))

    # ---- 2. 名前（固有語）
    gen = [h for path, text in texts for h in leakscan.scan_text(path, text, leakscan.PATTERNS)[0]]
    items.append(Item("N1", "名前", "汎用パターン（パス・メール・鍵・トークンなど）が0件", True, not gen,
                      f"{len(gen)} 件" if gen else ""))
    if not terms:
        ok = None if (not strict or allow_missing) else False
        items.append(Item("N2", "名前", "固有語リストのヒットが0件", True, ok,
                          "固有語リストなし" + ("（--strict のため ✗）" if ok is False else "（警告のみ）")))
    else:
        th = [h for path, text in texts for h in leakscan.scan_text(path, text, terms)[0]]
        items.append(Item("N2", "名前", "固有語リストのヒットが0件", True, not th, f"{len(th)} 件" if th else ""))
    ignored = sum(leakscan.scan_text(path, text, leakscan.PATTERNS + terms)[1] for path, text in texts)
    items.append(Item("N3", "名前", f"leakscan:ignore が上限（{leakscan.DEFAULT_MAX_IGNORES} 件）以内", True,
                      ignored <= leakscan.DEFAULT_MAX_IGNORES, f"{ignored} 件"))

    # ---- 3. 構成
    bad = [r for r in rels if any(fnmatch.fnmatch(r, g) or fnmatch.fnmatch(Path(r).name, g) for g in FORBIDDEN_FILES)]
    items.append(Item("C1", "構成", "個人設定ファイル（config.yaml・agents.yaml・*.local.yaml・pricing.yaml など）がない", True,
                      not bad, ", ".join(bad[:5])))
    gi = (tree / ".gitignore").read_text(encoding="utf-8").splitlines() if (tree / ".gitignore").exists() else []
    missing = [g for g in REQUIRED_IGNORES if g not in gi]
    items.append(Item("C2", "構成", ".gitignore に個人用の除外が全部ある", True, not missing, ", ".join(missing)))
    examples = [r for r in rels if r.endswith((".example.yaml", ".example.yml"))]
    leaked = []
    for r in examples:
        for ln in (tree / r).read_text(encoding="utf-8").splitlines():
            for m in re.finditer(r"\bmodel:\s*[\"']?([^\s,}\"']+)", ln):  # ブロック形式でも {model: ...} でも
                if MODEL_RE.search(m.group(1)):
                    leaked.append(r)
    items.append(Item("C3", "構成", "見本の設定（*.example.yaml）に実モデル名がない", True, bool(examples) and not leaked,
                      "見本の設定がない" if not examples else (", ".join(sorted(set(leaked))) or f"{len(examples)} 件を確認")))

    # ---- 4. 実データ
    records = [r for r in rels if (r.endswith("transcript.jsonl") and not r.startswith(SAMPLE_DIR + "/"))
               or r.endswith((".sqlite", ".db")) or r.startswith("exports/")
               or (r.startswith("meetings/") and r != "meetings/.gitkeep")]
    items.append(Item("D1", "実データ", "会議記録・台帳・書き出し（サンプル以外）がない", True, not records, ", ".join(records[:5])))
    sample = tree / SAMPLE_DIR
    subdirs = sorted(p.name for p in sample.iterdir() if p.is_dir()) if sample.is_dir() else []
    ok = subdirs == ["abnormal", "normal"] and all((sample / s / "transcript.jsonl").exists() for s in subdirs)
    items.append(Item("D2", "実データ", "サンプルは合成の2件（normal・abnormal）だけ", True, ok, ", ".join(subdirs) or "なし"))
    items.append(Item("D3", "実データ", "CI（leakscan generic / strict と、この監査）が全部合格している", False, None,
                      "手動（サインオフ 1）"))
    return items


def report(items: list[Item], out=None) -> int:
    out = out or sys.stdout
    auto = [i for i in items if i.auto]
    for i in items:
        mark = "—" if i.ok is None else ("✓" if i.ok else "✗")
        print(f"{mark} {i.key} [{i.layer}] {i.title}{'（手動）' if not i.auto else ''}{'  ' + i.note if i.note else ''}", file=out)
    failed = [i for i in auto if i.ok is False]
    print(f"release_audit v{VERSION}: {'NG' if failed else 'OK'}（機械判定 {len(auto)} 項目・合格 "
          f"{sum(1 for i in auto if i.ok)}・不合格 {len(failed)}・手動 {len(items) - len(auto)} 項目）", file=out)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="release_audit", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", help="組み上がった公開用ツリー（省くと --ref から組む）")
    ap.add_argument("--arrange", action="store_true", help="--dir のツリーを公開の形に並べ替える（.github/public をルートへ、workflows を外す）")
    ap.add_argument("--ref", default="HEAD", help="公開用ツリーを組む元（既定 HEAD）")
    ap.add_argument("--worktree", action="store_true", help="コミット前の作業ツリーから組む（ローカルの確認用）")
    ap.add_argument("--tag", help="リリースのタグ名（M1 で形を確かめる）")
    ap.add_argument("--terms", help="固有語リストのファイル（省くと環境変数 LEAKSCAN_TERMS）")
    ap.add_argument("--strict", action="store_true", help="固有語リストがなければ ✗ にする（CI）")
    ap.add_argument("--allow-missing-secrets", action="store_true", help="--strict でも固有語リストがなければ警告だけ（ローカル用）")
    a = ap.parse_args(argv)
    terms = leakscan.load_terms(a.terms)
    if a.dir and a.arrange:
        arrange_overlay(Path(a.dir))
    if a.dir:
        return report(audit(Path(a.dir), a.tag, terms, a.strict, a.allow_missing_secrets))
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "tree"
        tree = build_from_worktree(dest) if a.worktree else build_publish_tree(dest, a.ref)
        return report(audit(tree, a.tag, terms, a.strict, a.allow_missing_secrets))


if __name__ == "__main__":
    sys.exit(main())
