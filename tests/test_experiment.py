import asyncio
import csv
import json
from pathlib import Path

import pytest
import yaml

from dartmouth.adapters import AdapterError, Reply
from experiments.runner import DryAdapter, Experiment, main, run_session, sanitize, MODEL_PATTERN
from experiments.validate_output import FALLBACK, validate_output, write_review

CONFIG = Path(__file__).parent / "fixtures/experiment.yaml"
VALID = "【結論】案を採用\n【根拠】検証済み\n【未決】期限"


@pytest.mark.parametrize(
    "error, topic, expected_status, expected_reason, retries, switched",
    [
        ("quota exhausted", "1", "COMPLETED", "", 0, True),
        ("HTTP 429", "1", "ABORTED", "http_429", 1, False),
        ("quota exhausted", "2", "ABORTED", "restricted_fallback", 0, False),
    ],
)
def test_dry_run_safety(tmp_path, error, topic, expected_status, expected_reason, retries, switched):
    class Mock(DryAdapter):
        def __init__(self, name, adapter, role, timeout, options):
            super().__init__(name, adapter, role, timeout, options)
            self.adapter = adapter

        async def respond(self, ctx):
            if ctx.participant == "manager" and self.adapter == "antigravity_cli":
                raise AdapterError(error + " 非公開のエラー本文")
            return await super().respond(ctx)

    settings = Experiment.model_validate(yaml.safe_load(CONFIG.read_text()))
    row = asyncio.run(run_session(settings, topic, "A", "pilot", 1, "roles_standard", tmp_path, Mock))
    assert row["status"] == expected_status
    assert expected_reason in row["notes"]
    assert row["retries"] == retries
    records = [json.loads(line) for line in (tmp_path / row["log_ref"]).read_text().splitlines()]
    assert any(record["switch_count"] for record in records) == switched
    assert all(set(record) == {"seat_id", "model", "switch_reason", "switch_count", "prompt_sha256"} for record in records)
    assert all(len(record["prompt_sha256"]) == 64 for record in records)
    assert "非公開のエラー本文" not in str(row) + str(records)


def test_token_limit(tmp_path, caplog):
    class LargeReply(DryAdapter):
        async def respond(self, ctx):
            return Reply(VALID, tokens=240_000)

    settings = Experiment.model_validate(yaml.safe_load(CONFIG.read_text()))
    row = asyncio.run(run_session(settings, "1", "B", "pilot", 1, "roles_standard", tmp_path, LargeReply))
    assert row["status"] == "ABORTED"
    assert "token_limit" in row["notes"]
    assert "360000" in caplog.text
    assert 2 <= row["calls"] <= 4


def test_cli_pilot_and_review(tmp_path):
    args = ["--config", str(CONFIG), "--dry-run", "--artifacts", str(tmp_path)]
    main(args + ["--pilot", "--roles", "roles_reversed"])
    with (tmp_path / "sessions.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert [(row["topic"], row["condition"], row["roles"], row["calls"]) for row in rows] == [
        ("1", "A", "roles_reversed", "7"), ("1", "C", "roles_reversed", "7"),
    ]
    main(args)
    review = tmp_path / "review.md"
    write_review(tmp_path / "sessions.csv", tmp_path, review)
    assert review.read_text().count("採用可能な決定事項または ToDo が1件以上") == 5


def test_retry_retains_original_and_caps_total(tmp_path):
    from experiments.runner import CSV_FIELDS

    args = ["--config", str(CONFIG), "--dry-run", "--artifacts", str(tmp_path)]
    with (tmp_path / "sessions.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for session_id in ("one", "two", "three"):
            writer.writerow(dict(session_id=session_id, status="ABORTED", topic="1", condition="A",
                                 phase="pilot", roles="roles_standard"))
    main(args + ["--retry-session", "one"])
    with pytest.raises(SystemExit):
        main(args + ["--retry-session", "one"])
    main(args + ["--retry-session", "two"])
    with pytest.raises(SystemExit):
        main(args + ["--retry-session", "three"])
    with (tmp_path / "sessions.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert all(row["status"] == "ABORTED" for row in rows[:3])
    assert [row["retry_of"] for row in rows[3:]] == ["one", "two"]
    assert len({row["session_id"] for row in rows}) == 5


def test_validation_and_redaction():
    assert validate_output(VALID) == (VALID, False)
    assert validate_output("前置き\n" + VALID) == (VALID, True)
    assert validate_output("[同意] @seat D-001") == ("[同意] @seat D-001", False)
    for invalid in ("[同意] @seat D-1", "【未決】なし", VALID + "\n禁止", VALID + "長" * 400):
        assert validate_output(invalid, forbidden=("禁止",))[0].endswith(FALLBACK)
    assert sanitize("sk-" + "x" * 24, MODEL_PATTERN) == "[REDACTED]"


def test_fallback_cuts_at_configured_limit():
    from experiments.validate_output import FALLBACK, validate_output

    long = "【結論】" + "あ" * 900 + "\n【根拠】い\n【未決】う"
    out, bad = validate_output(long, max_chars=800)
    assert bad and out.endswith(FALLBACK) and len(out) == 800 + 1 + len(FALLBACK)
    out, bad = validate_output(long, max_chars=400)
    assert len(out) == 400 + 1 + len(FALLBACK)
