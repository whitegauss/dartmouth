"""ローカル LLM（Ollama）を道具として使う。考えさせない仕事（資料の要約・圧縮）だけを任せて、利用枠を節約する。

Ollama を入れていなければ使わない（設定の knowledge.local_llm を空にしておく）。
"""

from __future__ import annotations

import json
import urllib.request


class LocalLLM:
    def __init__(self, model: str, url: str = "http://127.0.0.1:11434", timeout_s: float = 120):
        self.model, self.url, self.timeout_s = model, url.rstrip("/"), timeout_s

    def generate(self, prompt: str) -> str:
        body = json.dumps({"model": self.model, "prompt": prompt, "stream": False, "options": {"temperature": 0}}).encode()
        req = urllib.request.Request(f"{self.url}/api/generate", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read()).get("response", "").strip()

    def compress(self, text: str, focus: str, max_chars: int = 400) -> str:
        """抜粋を、議題に関係する事実だけに縮める。出典の行番号は呼び出し側で付ける。"""
        prompt = (f"次の抜粋から、「{focus}」に関係する事実だけを、日本語で{max_chars}字以内の箇条書きにしてください。"
                  "推測や意見は書かず、固有名詞・数値・設定名はそのまま残してください。関係がなければ「関係なし」とだけ書いてください。\n\n"
                  f"--- 抜粋 ---\n{text}")
        return self.generate(prompt)
