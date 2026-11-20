"""ペルソナ定義（prompts/personas/*.md）の検査とトークン数の集計。

標準テンプレート（frontmatter に ``template: persona-v1``）のファイルは、
必須5ブロックの有無とトークン上限を検査し、違反があれば終了コード1を返す。
テンプレート未移行のファイルはトークン数の集計（基準値の算出用）だけに使う。

    dm lint-personas                 # prompts/personas を検査
    dm lint-personas --stats         # 全ファイルのトークン数と平均も出す
    dm lint-personas DIR --max-tokens 350
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import parse_persona

TEMPLATE = "persona-v1"
VERSION = "1"  # 検査規則の版（実験の管理シートに記録する）
DEFAULT_MAX_TOKENS = 350
# 必須ブロック（見出し ## の文言）。順番もこの通りであること
BLOCKS = ("問い", "判断基準", "必須検査", "触れない範囲", "出力")
OUTPUT_HEADINGS = ("【結論】", "【根拠】", "【未決】")


def estimate_tokens(text: str) -> int:
    """トークン数の概算。日本語などの非 ASCII は1字1トークン、ASCII は4字1トークンとみなす。"""
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    ascii_ = len(text) - non_ascii
    return non_ascii + (ascii_ + 3) // 4


def _headings(body: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^##\s+(.+?)\s*$", body, re.M)]


def _block(body: str, name: str) -> str:
    m = re.search(rf"^##\s+{re.escape(name)}\s*$(.*?)(?=^##\s|\Z)", body, re.M | re.S)
    return m.group(1).strip() if m else ""


def check_file(path: Path, max_tokens: int = DEFAULT_MAX_TOKENS) -> tuple[dict, list[str]]:
    """1ファイルを検査する。(情報, 違反の一覧) を返す。テンプレート未移行なら違反は空。"""
    meta, body = parse_persona(path)
    info = {"id": path.stem, "name": meta.get("name"), "tokens": estimate_tokens(body),
            "template": meta.get("template")}
    errs: list[str] = []
    if meta.get("template") != TEMPLATE:
        return info, errs
    for key in ("name", "role", "summary"):
        if not str(meta.get(key) or "").strip() or (key == "name" and meta.get("name") == path.stem):
            errs.append(f"frontmatter の {key} がない")
    heads = _headings(body)
    missing = [b for b in BLOCKS if b not in heads]
    if missing:
        errs.append("必須ブロックがない: " + ", ".join(f"## {b}" for b in missing))
    else:
        if [h for h in heads if h in BLOCKS] != list(BLOCKS):
            errs.append("ブロックの順番が違う（" + " → ".join(BLOCKS) + "）")
        extra = [h for h in heads if h not in BLOCKS]
        if extra:
            errs.append("テンプレートにない見出し: " + ", ".join(extra))
        for b in BLOCKS:
            if not _block(body, b):
                errs.append(f"## {b} が空（該当がなければ「該当なし」と書く）")
        out = _block(body, "出力")
        lacking = [h for h in OUTPUT_HEADINGS if h not in out]
        if lacking:
            errs.append("## 出力 に共通見出しがない: " + "".join(lacking))
    if info["tokens"] > max_tokens:
        errs.append(f"トークン数 {info['tokens']} が上限 {max_tokens} を超えている")
    return info, errs


def lint(dirs: list[Path], max_tokens: int = DEFAULT_MAX_TOKENS, stats: bool = False) -> int:
    files = sorted(f for d in dirs for f in (d.glob("*.md") if d.is_dir() else [d]))
    if not files:
        print("ペルソナのファイルがありません: " + ", ".join(map(str, dirs)))
        return 1
    bad = 0
    rows = []
    for f in files:
        info, errs = check_file(f, max_tokens)
        rows.append(info)
        for e in errs:
            print(f"{f}: {e}")
        bad += bool(errs)
    checked = [r for r in rows if r["template"] == TEMPLATE]
    if stats:
        print(f"{'id':<16}{'tokens':>7}  template")
        for r in rows:
            print(f"{r['id']:<16}{r['tokens']:>7}  {r['template'] or '-'}")
        legacy = [r["tokens"] for r in rows if r["template"] != TEMPLATE]
        if legacy:
            avg = sum(legacy) / len(legacy)
            print(f"未移行 {len(legacy)} 件の平均 {avg:.0f} トークン（×1.0〜1.25 = {avg:.0f}〜{avg * 1.25:.0f}）")
    print(f"lint_personas v{VERSION}: {len(checked)} 件を検査、違反 {bad} 件（上限 {max_tokens} トークン）")
    return 1 if bad else 0
