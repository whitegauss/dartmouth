from dartmouth.persona_output import normalize_reply


def test_valid_format_is_unchanged():
    text = "【結論】採用\n【根拠】計測結果\n【未決】なし"
    assert normalize_reply(text) == (text, False)


def test_fallback_preserves_unstructured_text():
    assert normalize_reply("根拠のある説明") == (
        "【結論】根拠のある説明\n【根拠】（記載なし）\n【未決】（記載なし）", True
    )


def test_reordered_duplicate_and_empty_blocks_are_repaired():
    assert normalize_reply("補足\n【未決】後で調査\n【結論】A\n【結論】B\n【根拠】") == (
        "【結論】補足 A B\n【根拠】（記載なし）\n【未決】後で調査", True
    )
