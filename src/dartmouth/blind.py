"""議事録の盲検化。モデル名・ペルソナ名・条件名を機械的に伏せ、残っていたら失敗にする。

    dm blind MEETING_DIR                 # MEETING_DIR/minutes.md を伏せて標準出力へ
    dm blind MEETING_DIR/digest.md -o out.md --term 追加の語

伏せたあとの文に識別子が1つでも残っていれば、何も書き出さずに終了コード1で止まる。
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import yaml

from .config import parse_persona

VERSION = "1"
# モデル・経路を示す語（大文字小文字は区別しない）
MODEL_TERMS = [
    "claude", "opus", "sonnet", "haiku", "anthropic",
    "gemini", "neon", "argon", "hydrogen", "flash", "agy",
    "chatgpt", "openai", "codex", "gpt", "gpt-oss", "o3", "o4-mini",
    "クロード", "オーパス", "ソネット", "ハイク", "ジェミニ", "ネオン", "アルゴン", "ハイドロジェン", "フラッシュ",
]
EFFORT_TERMS = ["high effort", "low effort", "medium effort", "effort", "xhigh", "high", "medium", "low",
                "高エフォート", "低エフォート", "中エフォート", "エフォート"]
_SEP = r"(?:\s*[/・,、と]\s*[A-D](?![A-Za-z]))*"  # 「条件A/B」「条件A・B」もまとめて伏せる
CONDITION_PATTERNS = [rf"条件\s*[A-D](?![A-Za-z]){_SEP}", r"(?<![A-Za-z])[A-D]\s*条件", r"condition\s*[A-D](?![A-Za-z])"]
MODEL_MASK = "[モデル]"
EFFORT_MASK = "[設定]"
COND_MASK = "[条件]"


def _ascii_word(term: str) -> str:
    """英数字の語は、前後が英数字でないところだけに当てる（"opus" が "opuses" の一部でも当たるのは許す）。"""
    esc = re.escape(term)
    if re.fullmatch(r"[A-Za-z0-9._-]+", term):
        return rf"(?<![A-Za-z0-9]){esc}"
    return esc


def collect_identities(meeting_dir: Path | None, personas_dir: Path | None) -> dict[str, str]:
    """伏せる名前 → 置き換え先。参加者（ペルソナ）は「参加者1」「参加者2」…に、議長は「議長」にする。"""
    names: dict[str, list[str]] = {}  # 参加者 id → 呼び名の一覧

    def add(pid: str, *labels: str | None) -> None:
        if pid in ("user", "system"):
            return
        names.setdefault(pid, [])
        for lab in (pid, *labels):
            if lab and lab not in names[pid]:
                names[pid].append(lab)

    if meeting_dir:
        st = meeting_dir / "status.json"
        if st.exists():
            data = json.loads(st.read_text(encoding="utf-8"))
            for pid, disp in (data.get("participants") or {}).items():
                add(pid, disp)
        cfgp = meeting_dir / "config.yaml"
        if cfgp.exists():
            raw = yaml.safe_load(cfgp.read_text(encoding="utf-8")) or {}
            for pid, p in (raw.get("participants") or {}).items():
                add(pid, (p or {}).get("display_name"))
            for pid in ((raw.get("seating") or {}).get("personas") or []):
                add(pid)
    if personas_dir and personas_dir.is_dir():
        for f in sorted(personas_dir.glob("*.md")):
            meta, _ = parse_persona(f)
            add(f.stem, meta.get("name"))
    out: dict[str, str] = {}
    n = 0
    for pid, labels in names.items():
        if pid == "chair":
            mask = "議長"
        else:
            n += 1
            mask = f"参加者{n}"
        for lab in labels:
            out.setdefault(lab, mask)
    out.pop("議長", None)  # 「議長」自体は条件を示さない
    return out


def blind_text(text: str, identities: dict[str, str], extra_terms: list[str] | None = None) -> tuple[str, list[str]]:
    """伏せた文と、伏せたあとに残った識別子の一覧を返す。全角英数字（Ｏｐｕｓ など）は先に半角にそろえる。"""
    repl: list[tuple[str, str]] = [(k, v) for k, v in identities.items()]
    repl += [(t, MODEL_MASK) for t in MODEL_TERMS + list(extra_terms or [])]
    repl += [(t, EFFORT_MASK) for t in EFFORT_TERMS]
    # 長い語から順に置き換える（「議長(Argon)」を「Argon」より先に）
    repl.sort(key=lambda kv: -len(kv[0]))
    out = unicodedata.normalize("NFKC", text)
    for pat in CONDITION_PATTERNS:
        out = re.sub(pat, COND_MASK, out, flags=re.I)
    for term, mask in repl:
        out = re.sub(_ascii_word(term), mask, out, flags=re.I)
    return out, find_residue(out, identities, extra_terms)


def find_residue(text: str, identities: dict[str, str], extra_terms: list[str] | None = None) -> list[str]:
    """残っている識別子（自己検査）。置き換え先の文字列は除いてから、部分一致で厳しめに探す。"""
    masks = {MODEL_MASK, EFFORT_MASK, COND_MASK, *identities.values()}
    scrub = unicodedata.normalize("NFKC", text)
    for m in sorted(masks, key=len, reverse=True):
        scrub = scrub.replace(m, " ")
    low = scrub.lower()
    found = []
    for term in [*identities, *MODEL_TERMS, *EFFORT_TERMS, *(extra_terms or [])]:
        t = term.lower()
        hit = re.search(_ascii_word(t), low) if re.fullmatch(r"[a-z0-9._-]+", t) else (t in low)
        if hit and term not in found:
            found.append(term)
    for pat in CONDITION_PATTERNS:
        m = re.search(pat, scrub, flags=re.I)
        if m:
            found.append(m.group(0))
    return found


def run(target: str, output: str | None, personas_dir: str | None, extra_terms: list[str]) -> int:
    p = Path(target)
    meeting_dir = p if p.is_dir() else p.parent
    src = p / "minutes.md" if p.is_dir() else p
    if not src.exists():
        print(f"{src} がありません")
        return 1
    pdir = Path(personas_dir) if personas_dir else Path("prompts/personas")
    ids = collect_identities(meeting_dir, pdir)
    out, residue = blind_text(src.read_text(encoding="utf-8"), ids, extra_terms)
    if residue:
        print(f"blind v{VERSION}: 識別子が残っています（書き出しを中止）: " + ", ".join(residue))
        return 1
    if output:
        Path(output).write_text(out, encoding="utf-8")
        print(f"blind v{VERSION}: {src} → {output}（伏せた名前 {len(ids)} 件）")
    else:
        print(out, end="")
    return 0
