"""会議ごとの予算（呼び出し回数・ラウンド数・発言の文字数）。"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import BudgetConfig


@dataclass
class Budget:
    cfg: BudgetConfig
    calls_used: int = 0
    rounds_used: int = 0
    tokens_used: int = 0

    @property
    def calls_left(self) -> int:
        return max(0, self.cfg.max_calls - self.calls_used)

    @property
    def rounds_left(self) -> int:
        return max(0, self.cfg.max_rounds - self.rounds_used)

    def can_call(self, n: int = 1) -> bool:
        return self.calls_used + n <= self.cfg.max_calls

    def can_start_round(self, n_calls: int) -> bool:
        """議論ラウンドは、全員分の呼び出しが予算内に収まるときだけ始める。"""
        return self.rounds_left > 0 and self.can_call(n_calls)

    def can_afford(self, calls: int) -> tuple[bool, str]:
        """calls 回の呼び出しを、呼び出し回数とトークン（上限があれば）の残りでまかなえるか。"""
        if not self.can_call(calls):
            return False, f"呼び出しの残り {self.calls_left} 回では足りません（見込み {calls} 回）"
        if self.cfg.max_tokens:
            avg = self.tokens_used / self.calls_used if self.calls_used else 0
            need = int(avg * calls)
            left = self.cfg.max_tokens - self.tokens_used
            if need > left:
                return False, f"トークンの残り {left} では足りません（見込み {need}）"
        return True, ""

    def truncate(self, text: str) -> tuple[str, bool]:
        limit = self.cfg.max_chars_per_message
        if len(text) <= limit:
            return text, False
        return text[:limit] + "\n…（文字数上限で省略）", True

    def describe(self) -> str:
        tok = f"、トークン {self.tokens_used}/{self.cfg.max_tokens}" if self.cfg.max_tokens else f"、トークン {self.tokens_used}"
        return (
            f"呼び出し 残り {self.calls_left}/{self.cfg.max_calls}回{tok}、"
            f"ラウンド 残り {self.rounds_left}/{self.cfg.max_rounds}、"
            f"発言上限 {self.cfg.max_chars_per_message}字"
        )
