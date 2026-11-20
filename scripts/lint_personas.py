#!/usr/bin/env python3
"""CI entry point for the four preregistered experiment personas."""

import argparse
from pathlib import Path
import sys

import yaml

from dartmouth.persona_lint import DEFAULT_MAX_TOKENS, TEMPLATE, check_file

PERSONAS = ("manager", "engineer", "critic", "quota")
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "prompts" / "personas"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args()

    failed = False
    for persona in PERSONAS:
        path = args.directory / f"{persona}.md"
        try:
            info, errors = check_file(path, DEFAULT_MAX_TOKENS)
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors = [str(error)]
        else:
            if info["template"] != TEMPLATE:
                errors.append(f"template: {TEMPLATE} が必要です")
        for error in errors:
            print(f"{path}: {error}", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
