"""Validation and text fallback for persona-v1 discussion replies."""

import re

from .persona_lint import OUTPUT_HEADINGS

VERSION = "1"
_HEADING = re.compile("|".join(map(re.escape, OUTPUT_HEADINGS)))


def normalize_reply(text: str) -> tuple[str, bool]:
    """Return the text for the chair and whether its format required repair."""
    matches = list(_HEADING.finditer(text))
    prefix = text[:matches[0].start()].strip() if matches else text.strip()
    ends = [match.start() for match in matches[1:]] + [len(text)]
    segments = [
        text[match.end():end].strip()
        for match, end in zip(matches, ends)
    ]
    if not prefix and tuple(match.group() for match in matches) == OUTPUT_HEADINGS and all(segments):
        return text, False

    sections: dict[str, list[str]] = {heading: [] for heading in OUTPUT_HEADINGS}
    if prefix:
        sections[OUTPUT_HEADINGS[0]].append(prefix)
    for match, segment in zip(matches, segments):
        if segment:
            sections[match.group()].append(segment)
    return "\n".join(
        f"{heading}{' '.join(parts) or '（記載なし）'}" for heading, parts in sections.items()
    ), True
