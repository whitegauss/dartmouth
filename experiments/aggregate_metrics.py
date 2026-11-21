#!/usr/bin/env python3
"""Dartmouth実験 本番12セッションの質の定量指標集計。

データ:
  - sessions-data/<session_id>/review.json (discussion全文 + chair集約 summary)
  - sessions.csv (実測: トークン, format_error 等)

指標の定義 (データの実構造に基づく):
  形式:
    statements        : review.json の discussion 配列の要素数 (= 発言数)
    truncated         : 発言テキストに文字列 "[FORMAT_FALLBACK]" を含む発言数。
                        このマーカーは上限 (818字) で切断された発言の末尾に付く。
    truncation_rate   : truncated / statements
    summary_truncated : chair 集約 (summary) 自体が [FORMAT_FALLBACK] で終わるか
  内容 (chair 集約 summary のパース):
    セクション見出しはセッションにより3形式ある:
      (a) "■決定事項案" / "■ToDo" の行見出し (末尾に条件文が付く場合あり)
      (b) "**決定事項案**" / "**ToDo**" の太字見出し
      (c) "決定事項案：" "ToDo：" で始まるインライン文 (本文中に ①②… で列挙)
    decisions : 決定事項案ブロック内の項目数。
                リスト形式では行頭の "1." "2." … の行数、
                インライン形式では丸数字 ①〜⑳ の出現数。
    todos     : ToDoブロック内の項目数。
                リスト形式では行頭 "- " "・" "1." 等の行数、
                インライン形式では丸数字の出現数。
    undecided : 【未決】ブロック内の項目数 (行頭 "- " "・" の行数)。
    numeric_sentence_ratio (具体性の代理指標):
                summary 全体を見出し行を除いて文に分割 (区切り: "。" と改行) し、
                半角/全角数字を含むか、数量表現 (数字+単位: 回/件/分/秒/時間/日/
                %/％/倍/KB/MB/円/ドル/人日/行/項目/点/割) を含む文の割合。
  過程:
    ref_mentions : discussion の各発言について、発言者以外の席への名前付き言及回数。
                   席の表記はセッションにより英語/日本語が混在するため別名で数える:
                   manager=マネージャー, engineer=エンジニア, critic=批判, quota=利用枠。
                   言及 = "@<別名>" の出現、または "<別名>案" "<別名>の" の出現。
                   全発言分の合計。
    ref_generic  : 名前を挙げない汎用言及 "他席" "各席" の出現数 (別掲)。
    ref_statements : 名前付き言及を1回以上含む発言の数。
  効率:
    total_tokens : sessions.csv の実測値
    output_per_100k_tokens : (decisions + todos) / total_tokens * 100000
"""
import csv
import json
import re
import sys
from pathlib import Path
from statistics import mean

BASE = Path(__file__).parent
DATA = BASE / "sessions-data"
LOCAL_B = BASE / "topic1-b-check"  # bb49bc05 の先行取得分

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
NUM_RE = re.compile(r"[0-9０-９]")
QTY_RE = re.compile(
    r"[0-9０-９一二三四五六七八九十百千]+\s*"
    r"(回|件|分|秒|時間|日|%|％|倍|KB|MB|GB|円|ドル|人日|行|項目|点|割|席|名|つ|個|文字|トークン)"
)
FALLBACK = "[FORMAT_FALLBACK]"


def load_sessions_csv():
    rows = {}
    with open(BASE / "sessions.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["phase"] == "main" and r["status"] == "COMPLETED" and r["session_id"]:
                rows[r["session_id"]] = r
    return rows


def load_review(sid):
    p = DATA / sid / "review.json"
    if not p.exists() and sid == "bb49bc0588304d1185ae1d7f73bf7152":
        p = LOCAL_B / "review.json"
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def split_sections(summary):
    text = summary.replace(FALLBACK, "")
    lines = text.split("\n")
    bounds = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if re.match(r"^■\s*決定事項案", s):
            bounds.append(("decisions", i, ""))
        elif re.match(r"^\*\*決定事項案\*\*", s):
            bounds.append(("decisions", i, ""))
        elif re.match(r"^決定事項案[：:]", s):
            bounds.append(("decisions", i, re.sub(r"^決定事項案[：:]", "", s)))
        elif re.match(r"^■\s*ToDo", s):
            bounds.append(("todos", i, ""))
        elif re.match(r"^\*\*ToDo\*\*", s):
            bounds.append(("todos", i, ""))
        elif re.match(r"^ToDo[：:]", s):
            bounds.append(("todos", i, re.sub(r"^ToDo[：:]", "", s)))
        elif s.startswith("【未決】"):
            bounds.append(("undecided", i, ""))
        elif s.startswith("【根拠】") or s.startswith("【結論】"):
            bounds.append(("other", i, ""))
    sections = {}
    for j, (kind, idx, inline) in enumerate(bounds):
        end = bounds[j + 1][1] if j + 1 < len(bounds) else len(lines)
        chunk = ([inline] if inline else []) + lines[idx + 1:end]
        if kind != "other":
            sections.setdefault(kind, []).extend(chunk)
    return {k: "\n".join(v) for k, v in sections.items()}


def count_items(block):
    if not block.strip():
        return 0
    circled = sum(block.count(c) for c in CIRCLED)
    list_items = 0
    for ln in block.split("\n"):
        s = ln.strip()
        if re.match(r"^(\d+)[\.．、]", s) or s.startswith("- ") or s.startswith("・") or s == "-":
            list_items += 1
    return max(circled, list_items) if circled else list_items


def numeric_ratio(summary):
    body_lines = []
    for ln in summary.replace(FALLBACK, "").split("\n"):
        s = ln.strip()
        if re.match(r"^(■|【|\*\*決定事項案\*\*|\*\*ToDo\*\*)", s):
            continue
        body_lines.append(ln)
    text = "\n".join(body_lines)
    sentences = [s.strip() for s in re.split(r"[。\n]", text) if len(s.strip()) >= 4]
    if not sentences:
        return 0.0, 0, 0
    hit = sum(1 for s in sentences if NUM_RE.search(s) or QTY_RE.search(s))
    return hit / len(sentences), hit, len(sentences)


ALIASES = {
    "manager": ["manager", "マネージャー"],
    "engineer": ["engineer", "エンジニア"],
    "critic": ["critic", "批判"],
    "quota": ["quota", "利用枠"],
}


def ref_counts(discussion):
    seats = sorted({seat for seat, _ in discussion})
    total = 0
    stmts = 0
    generic = 0
    for seat, text in discussion:
        n = 0
        for other in seats:
            if other == seat:
                continue
            for alias in ALIASES.get(other, [other]):
                n += text.count("@" + alias)
                n += len(re.findall(re.escape(alias) + r"(案|の)", text))
        total += n
        if n > 0:
            stmts += 1
        generic += text.count("他席") + text.count("各席")
    return total, stmts, generic


def main():
    meta = load_sessions_csv()
    out_rows = []
    for sid, m in sorted(meta.items(), key=lambda kv: int(kv[1]["order"])):
        rev = load_review(sid)
        discussion = rev["discussion"]
        summary = rev["summary"]
        statements = len(discussion)
        truncated = sum(1 for _, t in discussion if FALLBACK in t)
        sections = split_sections(summary)
        decisions = count_items(sections.get("decisions", ""))
        todos = count_items(sections.get("todos", ""))
        undecided = count_items(sections.get("undecided", ""))
        ratio, n_hit, n_sent = numeric_ratio(summary)
        refs, ref_stmts, ref_generic = ref_counts(discussion)
        total_tokens = int(m["total_tokens"])
        out_rows.append({
            "session_id": sid,
            "topic": m["topic"],
            "condition": m["condition"],
            "statements": statements,
            "truncated": truncated,
            "truncation_rate": round(truncated / statements, 3) if statements else "",
            "format_error_csv": m["format_error"],
            "summary_truncated": int(summary.rstrip().endswith(FALLBACK)),
            "decisions": decisions,
            "todos": todos,
            "undecided": undecided,
            "numeric_sentences": n_hit,
            "total_sentences": n_sent,
            "numeric_sentence_ratio": round(ratio, 3),
            "ref_mentions": refs,
            "ref_statements": ref_stmts,
            "ref_generic": ref_generic,
            "calls": m["calls"],
            "total_tokens": total_tokens,
            "output_per_100k_tokens": round((decisions + todos) / total_tokens * 100000, 2),
        })
    cols = list(out_rows[0].keys())
    with open(BASE / "metrics.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)
    for r in out_rows:
        print(r)
    print("\n=== 条件別平均 ===")
    for cond in "ABCD":
        rs = [r for r in out_rows if r["condition"] == cond]
        print(cond,
              "切断率", round(mean(r["truncation_rate"] for r in rs), 3),
              "| 決定+ToDo", round(mean(r["decisions"] + r["todos"] for r in rs), 2),
              "| 具体性", round(mean(r["numeric_sentence_ratio"] for r in rs), 3),
              "| 言及合計", round(mean(r["ref_mentions"] for r in rs), 1),
              "| tokens", int(mean(r["total_tokens"] for r in rs)),
              "| 効率", round(mean(r["output_per_100k_tokens"] for r in rs), 2))


if __name__ == "__main__":
    sys.exit(main())
