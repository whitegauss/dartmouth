"""モデルの単価と、公開用の抽象プロファイル（規模帯・effort・相対コスト比）。

単価表は公開しない（実モデル名と実金額が入るため）。既定の置き場所は環境変数 DARTMOUTH_PRICING、
なければ個人用ディレクトリの pricing.yaml。形式（金額は 100万トークンあたりの USD）:

    models:
      <モデルID>:                # 会議の設定・dm stats に出るモデル名そのもの
        tier: Flagship          # 規模帯: Flagship / Lightweight など
        effort: high            # 推論 effort（任意）
        input: 3.0
        output: 15.0
        cache_read: 0.3         # 任意（省くと input の 10%）
        cache_creation: 3.75    # 任意（省くと input の 125%）

公開用の表記では、モデル名を「規模帯/effort」に、金額を相対コスト（いちばん安いモデルの
入力と出力の平均単価を 1.0 とした比）に置き換える。単価表にないモデルはエラーにする（伏せ損ねを防ぐ）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PER = 1_000_000


class UnknownModel(KeyError):
    pass


def pricing_path() -> Path:
    if os.environ.get("DARTMOUTH_PRICING"):
        return Path(os.environ["DARTMOUTH_PRICING"])
    from .paths import personal_dir

    return personal_dir() / "pricing.yaml"


def load_pricing(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    p = Path(path) if path else pricing_path()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    models = data.get("models") or {}
    if not isinstance(models, dict) or not models:
        raise ValueError(f"{p} に models: がありません")
    out = {}
    for mid, m in models.items():
        if not isinstance(m, dict) or "input" not in m or "output" not in m or "tier" not in m:
            raise ValueError(f"{p} の {mid}: tier・input・output が必要です")
        inp, outp = float(m["input"]), float(m["output"])
        out[str(mid)] = {"tier": str(m["tier"]), "effort": str(m.get("effort") or ""), "input": inp, "output": outp,
                         "cache_read": float(m.get("cache_read", inp * 0.1)),
                         "cache_creation": float(m.get("cache_creation", inp * 1.25))}
    return out


def _entry(pricing: dict[str, dict[str, Any]], model: str) -> dict[str, Any]:
    if model not in pricing:
        raise UnknownModel(f"単価表にないモデルです: {model}")
    return pricing[model]


def cost_usd(pricing: dict[str, dict[str, Any]], model: str, input: int = 0, output: int = 0, cache_read: int = 0,
             cache_creation: int = 0, thinking: int = 0) -> float:
    """使ったトークンの金額（USD）。thinking は出力の単価で数える。"""
    m = _entry(pricing, model)
    return (input * m["input"] + (output + thinking) * m["output"] + cache_read * m["cache_read"]
            + cache_creation * m["cache_creation"]) / PER


def unit_price(pricing: dict[str, dict[str, Any]]) -> float:
    """相対コストの基準: いちばん安いモデルの、入力と出力の平均単価（100万トークンあたり USD）。"""
    return min((m["input"] + m["output"]) / 2 for m in pricing.values())


def relative_cost(pricing: dict[str, dict[str, Any]], usd: float) -> float:
    """金額を相対コストにする（基準モデルで100万トークン使った額を 1.0 とする）。"""
    return usd / unit_price(pricing)


def profile(pricing: dict[str, dict[str, Any]], model: str) -> str:
    """公開用の表記。例: Flagship/high ×12.0（×は基準モデルとの単価比）。"""
    m = _entry(pricing, model)
    ratio = ((m["input"] + m["output"]) / 2) / unit_price(pricing)
    return f"{m['tier']}/{m['effort'] or '-'} ×{ratio:.1f}"


TOKEN_FIELDS = ("input", "output", "cache_read", "cache_creation", "thinking")


def row_cost(pricing: dict[str, dict[str, Any]], r: dict[str, Any]) -> float | None:
    """dm stats の1行の金額。トークン数が記録されていない行（古い会議・使用量を返さない経路）は None。"""
    vals = [r.get(k, 0) for k in TOKEN_FIELDS]
    if any(v in ("", None) for v in vals):
        _entry(pricing, r.get("model") or "")  # 単価表にないモデルは、使用量がなくてもエラーにする
        return None
    return cost_usd(pricing, r.get("model") or "", *vals)


def public_stat_rows(rows: list[dict[str, Any]], pricing: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """dm stats の行を公開用にする: モデル名を抽象プロファイルに、表示名を参加者IDに置き換え、相対コストを足す。"""
    out = []
    for r in rows:
        usd = row_cost(pricing, r)
        pid = r.get("public_id") or r.get("participant", "")  # 設定に抽象ID（public_id）があればそれを出す
        out.append({**r, "model": profile(pricing, r.get("model") or ""), "participant": pid, "display_name": pid,
                    "cost_rel": "" if usd is None else round(relative_cost(pricing, usd), 4)})
    return out


def private_stat_rows(rows: list[dict[str, Any]], pricing: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """dm stats の行に実金額（USD）を足す（手元で見る用）。使用量が記録されていない行は空欄。"""
    out = []
    for r in rows:
        usd = row_cost(pricing, r)
        out.append({**r, "cost_usd": "" if usd is None else round(usd, 4)})
    return out
