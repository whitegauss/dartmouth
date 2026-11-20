import pytest

from dartmouth.profiles import UnknownModel, cost_usd, load_pricing, profile, public_stat_rows, relative_cost


@pytest.fixture
def pricing(tmp_path):
    p = tmp_path / "pricing.yaml"
    p.write_text("models:\n"
                 "  big-model: {tier: Flagship, effort: high, input: 10, output: 50}\n"
                 "  small-model: {tier: Lightweight, input: 1, output: 3}\n", encoding="utf-8")
    return load_pricing(p)


def test_cost_and_relative_cost(pricing):
    assert cost_usd(pricing, "big-model", input=1_000_000, output=100_000) == pytest.approx(15.0)
    # cache の単価は省略時 input の 10% / 125%
    assert cost_usd(pricing, "small-model", cache_read=1_000_000, cache_creation=1_000_000) == pytest.approx(0.1 + 1.25)
    assert relative_cost(pricing, 2.0) == pytest.approx(1.0)  # 基準: 安いモデルの (1+3)/2 = 2 USD
    assert profile(pricing, "big-model") == "Flagship/high ×15.0" and profile(pricing, "small-model") == "Lightweight/- ×1.0"


def test_public_rows_hide_model_and_money_and_fail_closed(pricing):
    rows = [{"meeting": "m", "participant": "critic", "display_name": "批判役(本名)", "model": "big-model",
             "input": 1000, "output": 1000, "cache_read": 0, "cache_creation": 0, "thinking": 0}]
    pub = public_stat_rows(rows, pricing)[0]
    assert pub["model"] == "Flagship/high ×15.0" and pub["display_name"] == "critic" and "cost_usd" not in pub
    assert pub["cost_rel"] == pytest.approx(round(0.06 / 2, 4))
    with pytest.raises(UnknownModel):
        public_stat_rows([{**rows[0], "model": "secret-model"}], pricing)


def test_load_pricing_requires_fields(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("models:\n  x: {input: 1}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_pricing(p)


def test_rows_without_usage_get_blank_cost(pricing):
    from dartmouth.profiles import private_stat_rows

    row = {"meeting": "m", "participant": "a", "model": "small-model", "input": "", "output": "", "cache_read": "",
           "cache_creation": "", "thinking": ""}
    assert private_stat_rows([row], pricing)[0]["cost_usd"] == "" and public_stat_rows([row], pricing)[0]["cost_rel"] == ""


def test_public_rows_use_public_id(pricing):
    row = {"meeting": "m", "participant": "critic", "public_id": "seat-H1", "display_name": "x", "model": "big-model",
           "input": 1, "output": 1, "cache_read": 0, "cache_creation": 0, "thinking": 0}
    pub = public_stat_rows([row], pricing)[0]
    assert pub["participant"] == "seat-H1" and pub["display_name"] == "seat-H1"
