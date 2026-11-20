"""テスト用のダミーエージェント。本物のCLIやブラウザを使わずに会議を回せる。

options:
  replies:      発言のリスト（順に使い、尽きたら最後を繰り返す）。{round} {topic} {n} {name} を置換
  delay_s:      応答までの秒数。[最小, 最大] でランダム
  fail_rate:    0〜1。この確率で失敗する
  fail_mode:    error（例外）/ hang（応答しない＝タイムアウト）
  fail_calls:   失敗させる呼び出し番号（1始まり）のリスト
  fail_rounds:  このラウンドでは必ず失敗する（例: [critique]）
  seed:         乱数の種
議長（role: chair）が summary ラウンドで呼ばれたときは、決まった形式のJSONを返す。
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

from .base import Adapter, AdapterError, Reply, TurnContext


class DummyAdapter(Adapter):
    def __init__(self, name: str, role: str, timeout_s: float, options: dict[str, Any] | None = None):
        super().__init__(name, role, timeout_s, options)
        o = self.options
        self.replies: list[str] = list(o.get("replies") or [])
        d = o.get("delay_s", 0)
        self.delay = (float(d[0]), float(d[1])) if isinstance(d, (list, tuple)) else (float(d), float(d))
        self.fail_rate = float(o.get("fail_rate", 0))
        self.fail_mode = o.get("fail_mode", "error")
        self.fail_calls = set(o.get("fail_calls") or [])
        self.fail_rounds = set(o.get("fail_rounds") or [])
        self.bad_json = bool(o.get("bad_json", False))
        # 議長として、この議題で延長を申し出る（handover_missing なら最初は申し送りを書かない）
        self.extend_topics = set(o.get("extend_topics") or [])
        self.handover_missing = bool(o.get("handover_missing", False))
        self._handover_skipped: set = set()
        self.rng = random.Random(o.get("seed"))
        self.calls = 0
        self.prompts: list[TurnContext] = []  # テストで中身を確かめるために残す

    async def respond(self, ctx: TurnContext) -> Reply:
        self.calls += 1
        self.prompts.append(ctx)
        n = self.calls
        lo, hi = self.delay
        if hi > 0:
            await asyncio.sleep(self.rng.uniform(lo, hi))
        if n in self.fail_calls or ctx.round in self.fail_rounds or (self.fail_rate and self.rng.random() < self.fail_rate):
            if self.fail_mode == "hang":
                await asyncio.sleep(3600)
            raise AdapterError(f"dummy failure (call {n})")
        if ctx.round == "plan" and ctx.response_schema is not None:
            # 議長の席決め: options.seat_plan をそのまま返す（なければ空 → エンジンが自動で補う）
            return Reply(json.dumps({"seats": self.options.get("seat_plan") or []}, ensure_ascii=False), tokens=20)
        if ctx.round == "summary" and ctx.response_schema is not None:
            if self.bad_json and n % 2 == 1:
                return Reply("まとめです（JSONではありません）")
            data = {
                "agreements": [f"議題{ctx.topic}について大筋で合意"],
                "disagreements": ["細部の優先度"],
                "decisions": [f"議題{ctx.topic}の決定事項案（{self.name} 呼び出し{n}）"],
                "open_questions": [{"text": f"議題{ctx.topic}の残課題", "owner": "gemini"}],
                "todos": [{"text": f"議題{ctx.topic}の調査", "owner": "user"}],
                "summary": f"議題{ctx.topic}の要約。",
                "extension_requested": False, "extension_reason": "", "handover": "",
                # 席の採点: options.seat_score_map（ペルソナ → 点）。なければプロンプトに出てくる席に 3 点
                "seat_scores": [{"persona": k, "score": v, "note": "dummy"} for k, v in (self.options.get("seat_score_map") or {}).items()],
            }
            if ctx.topic in self.extend_topics and "extension_requested は必ず false" not in ctx.prompt:
                data["extension_requested"] = True
                data["extension_reason"] = f"議題{ctx.topic}の対立点が残っている"
                if self.handover_missing and ctx.topic not in self._handover_skipped:
                    self._handover_skipped.add(ctx.topic)
                else:
                    data["handover"] = f"## 最良案\n議題{ctx.topic}の案B\n## 残る論点\n費用\n## 問い\n- 月いくらまでなら出せるか"
            return Reply(json.dumps(data, ensure_ascii=False), tokens=50)
        if self.replies:
            tmpl = self.replies[min(n - 1, len(self.replies) - 1)]
        else:
            tmpl = "{name} の{round}での発言（議題{topic}、{n}回目）"
        text = tmpl.format(round=ctx.round, topic=ctx.topic, n=n, name=self.name)
        return Reply(text, tokens=len(text) // 2)
