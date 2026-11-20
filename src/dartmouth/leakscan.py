"""公開リポジトリに個人の情報が混ざっていないかを調べる（標準ライブラリだけで動く。CI でもそのまま使える）。

2段で調べる。
- 汎用パターン（このファイルに書いてあり、公開してよいもの）: ホームディレクトリのパス、メールアドレス、
  API キーやトークンの形、秘密鍵、Tailscale のアドレスなど
- 固有語リスト（公開しないもの）: 個人の名前・用途・機材名など。環境変数 LEAKSCAN_TERMS（GitHub Secrets から渡す）か
  --terms のファイルから読む。1行1語、`re:` で始まる行は正規表現、`#` で始まる行はコメント。
  英数字だけの語は単語境界（\\b）付きで、大文字小文字を区別せずに探す（argon が "argonaut" に当たらないように）

ヒットしたら終了コード1。出力には場所と規則の番号だけを出し、**当たった文字列そのものは出さない**
（CI のログは公開されるため）。誤検知は、その行に `leakscan:ignore` と書けば外せるが、リポジトリ全体で
--max-ignores（既定5）件を超えたら失敗にする。ファイル単位の除外は .leakscan-ignore（1行1 glob）に書く。

    python src/dartmouth/leakscan.py                     # 追跡しているファイル全部（汎用パターン＋あれば固有語）
    python src/dartmouth/leakscan.py --staged            # コミットしようとしている内容（pre-commit 用）
    python src/dartmouth/leakscan.py --strict            # 固有語リストがないと失敗（CI 用。--require-terms と同じ）
    python src/dartmouth/leakscan.py --strict --allow-missing-secrets   # ローカルで固有語リストがないときは警告だけ
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

VERSION = "1"
IGNORE_MARK = "leakscan:ignore"
DEFAULT_MAX_IGNORES = 5

# 汎用パターン: (規則名, 正規表現)。公開してよい形だけを書く
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("home-path", re.compile(r"(?:/home/|/Users/)(?!runner\b|user\b|me\b|you\b|<|\$|\{)[A-Za-z0-9._-]+")),
    ("windows-home", re.compile(r"[A-Za-z]:\\\\?Users\\\\?(?!Public\b|<)[A-Za-z0-9._-]+")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net)\b|anthropic\.com\b|users\.noreply\.github\.com\b|github\.com\b)"
                         r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")),
    ("api-key", re.compile(r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}|gh[pousr]_[A-Za-z0-9]{30,}"
                           r"|xox[abpr]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_-]{20,})")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}")),
    ("tailscale-ip", re.compile(r"\b(?!100\.64\.0\.0\b)100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")),
    ("discord-webhook", re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+")),
]

# 調べないファイル（バイナリ・生成物）
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".woff", ".woff2", ".sqlite", ".lock"}


@dataclass
class Hit:
    path: str
    line: int
    rule: str


def parse_terms(text: str) -> list[tuple[str, re.Pattern[str]]]:
    """固有語リストを (規則名, 正規表現) にする。規則名は terms#行番号（語そのものは出さない）。"""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        t = raw.strip()
        if not t or t.startswith("#"):
            continue
        if t.startswith("re:"):
            pat = re.compile(t[3:])
        elif re.fullmatch(r"[A-Za-z0-9._-]+", t):
            pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])", re.IGNORECASE)
        else:
            pat = re.compile(re.escape(t), re.IGNORECASE)
        out.append((f"terms#{n}", pat))
    return out


def scan_text(path: str, text: str, rules: list[tuple[str, re.Pattern[str]]]) -> tuple[list[Hit], int]:
    """1ファイル分。(ヒット, leakscan:ignore で外した件数) を返す。"""
    hits, ignored = [], 0
    for n, line in enumerate(text.splitlines(), 1):
        found = [name for name, pat in rules if pat.search(line)]
        if not found:
            continue
        if IGNORE_MARK in line:
            ignored += 1
            continue
        hits += [Hit(path, n, name) for name in found]
    return hits, ignored


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def _ignored_globs(root: Path) -> list[str]:
    f = root / ".leakscan-ignore"
    if not f.exists():
        return []
    return [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]


def targets(staged: bool) -> list[tuple[str, str]]:
    """(パス, 中身) の一覧。staged なら index の中身（コミットされる内容）を読む。"""
    root = Path(_git("rev-parse", "--show-toplevel").strip())
    globs = _ignored_globs(root)
    names = (_git("diff", "--cached", "--name-only", "--diff-filter=ACMR") if staged else _git("ls-files")).splitlines()
    out = []
    for name in names:
        if Path(name).suffix.lower() in SKIP_SUFFIXES or any(fnmatch.fnmatch(name, g) for g in globs):
            continue
        try:
            text = _git("show", f":{name}") if staged else (root / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, subprocess.CalledProcessError):
            continue
        out.append((name, text))
    return out


def load_terms(path: str | None) -> str:
    if path:
        p = Path(path).expanduser()
        return p.read_text(encoding="utf-8") if p.exists() else ""
    return os.environ.get("LEAKSCAN_TERMS", "")


def run(files: list[tuple[str, str]], terms_text: str, require_terms: bool, max_ignores: int, out=None,
        allow_missing: bool = False) -> int:
    out = out or sys.stdout
    terms = parse_terms(terms_text)
    if require_terms and not terms:
        if not allow_missing:
            print("leakscan: 固有語リストがありません（LEAKSCAN_TERMS か --terms）。--strict のため失敗にします", file=out)
            return 2
        # ローカル開発だけの逃げ道（Q-003）。CI では使わない（leakscan.yml は失敗で止める）
        print("leakscan: 警告: 固有語リストがないため、汎用パターンだけで調べます（--allow-missing-secrets）", file=out)
    rules = PATTERNS + terms
    hits, ignored = [], 0
    for path, text in files:
        h, ig = scan_text(path, text, rules)
        hits += h
        ignored += ig
    for h in hits:
        print(f"{h.path}:{h.line}: {h.rule}", file=out)
    over = ignored > max_ignores
    if over:
        print(f"leakscan: {IGNORE_MARK} が {ignored} 件あり、上限 {max_ignores} 件を超えています", file=out)
    status = "NG" if hits or over else "OK"
    print(f"leakscan v{VERSION}: {status}（{len(files)} ファイル・汎用 {len(PATTERNS)} 規則・固有語 {len(terms)} 語・"
          f"ヒット {len(hits)} 件・除外 {ignored} 件）", file=out)
    return 1 if hits or over else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="leakscan", description=__doc__.split("\n\n")[0])
    ap.add_argument("--staged", action="store_true", help="コミットしようとしている内容だけを調べる（pre-commit 用）")
    ap.add_argument("--terms", help="固有語リストのファイル（省くと環境変数 LEAKSCAN_TERMS）")
    ap.add_argument("--require-terms", "--strict", dest="require_terms", action="store_true",
                    help="固有語リストがなければ失敗にする（CI の strict 検査）")
    ap.add_argument("--allow-missing-secrets", action="store_true",
                    help="--strict でも固有語リストがなければ警告だけで続ける（ローカル開発用。CI では使わない）")
    ap.add_argument("--max-ignores", type=int, default=DEFAULT_MAX_IGNORES, help=f"{IGNORE_MARK} の上限（既定 {DEFAULT_MAX_IGNORES}）")
    a = ap.parse_args(argv)
    return run(targets(a.staged), load_terms(a.terms), a.require_terms, a.max_ignores, allow_missing=a.allow_missing_secrets)


if __name__ == "__main__":
    sys.exit(main())
