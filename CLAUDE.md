# Notes for coding agents

- Run tests with `scripts/t` (optionally with pytest args, e.g. `scripts/t tests/test_web.py -k watch`).
  It prints one line when everything passes and only the failing tests' short tracebacks otherwise.
  Do not run plain `pytest` and read the full output; it wastes tokens.
- Check the Web UI scripts with `for f in src/dartmouth/ui/web_static/js/*.js; do node --check "$f" || echo "$f"; done` after editing them.
