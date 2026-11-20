"""実験用の応答検査と抜取レビューの生成。"""

import argparse
import csv
import json
import re
from pathlib import Path

from dartmouth.persona_lint import OUTPUT_HEADINGS
from dartmouth.persona_output import normalize_reply

HEADINGS = re.compile("|".join(map(re.escape, OUTPUT_HEADINGS)))
AGREEMENT = re.compile(r"\[同意\] @[A-Za-z0-9_-]+ D-[0-9]{3}")
FALLBACK = "[FORMAT_FALLBACK]"


def validate_output(text: str, max_chars: int = 400, forbidden: tuple[str, ...] = ()) -> tuple[str, bool]:
    text = text.strip()
    if AGREEMENT.fullmatch(text) and len(text) <= 30 and not any(word in text for word in forbidden):
        return text, False

    normalized, repaired = normalize_reply(text)
    matches = list(HEADINGS.finditer(text))
    ends = [match.start() for match in matches[1:]] + [len(text)]
    lines = [line.strip() for line in text.splitlines() if "[同意]" in line]
    invalid = (
        len(text) > max_chars
        or any(word in text for word in forbidden)
        or any(len(line) > 30 or not AGREEMENT.fullmatch(line) for line in lines)
    )
    if not repaired and not invalid:
        return normalized, False
    if (
        tuple(match.group() for match in matches) == OUTPUT_HEADINGS
        and all(text[match.end():end].strip() for match, end in zip(matches, ends))
    ):
        extracted, _ = normalize_reply(text[matches[0].start():])
        extracted = extracted.strip()
        if (
            len(extracted) <= max_chars
            and not any(word in extracted for word in forbidden)
            and all(
                AGREEMENT.fullmatch(line.strip()) and len(line.strip()) <= 30
                for line in extracted.splitlines() if "[同意]" in line
            )
        ):
            return extracted, True
    # 復元できないときは字数の上限で切る（D-013 の「先頭400字」は上限400字のときの決め方。上限を変えたら上限で切る）
    return text[:max_chars] + "\n" + FALLBACK, True


def write_review(csv_path: Path, artifacts: Path, destination: Path) -> None:
    with csv_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    samples = {}
    for row in rows:
        session = row["session_id"]
        if (
            row["status"] == "COMPLETED" and row["phase"] == "main"
            and row["condition"] not in samples
            and re.fullmatch(r"[A-Za-z0-9_-]+", session)
        ):
            artifact = artifacts / session / "review.json"
            if artifact.is_file():
                samples[row["condition"]] = (session, json.loads(artifact.read_text(encoding="utf-8")))
    sections = ["# 抜取レビュー", "採用可能な決定事項または ToDo が1件以上、かつ重大な見落とし・規約違反が0件なら合格。"]
    for condition in "ABCD":
        sample = samples.get(condition)
        if sample is None:
            sections.append(f"## 条件 {condition}\n\n対象セッションなし")
            continue
        session, review = sample
        discussion = "\n\n".join(f"@{seat}\n\n{text}" for seat, text in review.get("discussion", []))
        sections.append(
            f"## 条件 {condition}（{session}）\n\n{review['summary']}\n\n"
            "- [ ] 採用可能な決定事項または ToDo が1件以上\n"
            f"- [ ] 重大な見落とし・規約違反が0件\n\n<details>\n<summary>議論の記録</summary>\n\n{discussion}\n\n</details>"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n\n".join(sections) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=Path("docs/experiment-sessions.csv"))
    parser.add_argument("--artifacts", type=Path, default=Path("local/experiment"))
    parser.add_argument("--output", type=Path, default=Path("local/experiment/review.md"))
    args = parser.parse_args()
    write_review(args.csv, args.artifacts, args.output)


if __name__ == "__main__":
    main()
