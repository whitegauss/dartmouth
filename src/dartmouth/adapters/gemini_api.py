"""Gemini API（Google AI Studio の API キー）アダプタ。追加の依存なし（標準ライブラリの HTTP）。

- API キーは環境変数（既定 GEMINI_API_KEY、options.api_key_env で変更）から読む。ログ・記録には書かない
- 議長のまとめ（response_schema あり）は、responseMimeType=application/json と responseSchema で形を強制する
- 回数の上限（429）は、上限に当たったことが分かるエラーにする（エンジンが再試行し、dm stats で「回数上限」と数える）

注意: 無料枠（unpaid）では、送った内容が Google のサービス改善に使われ、人が読むことがある（欧州・スイス・英国以外）。
非公開の資料を渡さないよう、無料枠で使う参加者は share_scope: public_only にする。

options: model（必須）, api_key_env, temperature, thinking_budget, base_url
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

from .base import Adapter, AdapterError, Reply, TurnContext

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_SCHEMA_KEYS = {"type", "properties", "required", "items", "enum", "description", "nullable", "format"}


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema を、Gemini の responseSchema が受け付ける部分だけにする。"""
    out = {k: v for k, v in schema.items() if k in _SCHEMA_KEYS}
    if "properties" in out:
        out["properties"] = {k: to_gemini_schema(v) for k, v in out["properties"].items()}
    if "items" in out:
        out["items"] = to_gemini_schema(out["items"])
    return out


def api_key(env_name: str = "GEMINI_API_KEY") -> str:
    key = os.environ.get(env_name, "").strip()
    if not key:
        raise AdapterError(f"環境変数 {env_name} に Gemini の API キーがありません（Google AI Studio で発行して設定してください）")
    return key


def _post(url: str, body: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            err = json.loads(e.read()).get("error", {})
            detail = err.get("message", "")
            if e.code == 429:
                retry = next((d.get("retryDelay") for d in err.get("details", []) if d.get("retryDelay")), None)
                raise AdapterError(f"Gemini API の回数上限（429）に当たりました{f'（{retry} 後に再試行できます）' if retry else ''}: {detail[:200]}") from e
        except (ValueError, AttributeError):
            pass
        raise AdapterError(f"Gemini API がエラーを返しました（HTTP {e.code}）: {detail[:300]}") from e
    except urllib.error.URLError as e:
        raise AdapterError(f"Gemini API に接続できません: {e.reason}") from e


def list_models(key: str, base_url: str = BASE_URL) -> list[dict[str, Any]]:
    req = urllib.request.Request(f"{base_url}/models?pageSize=200", headers={"x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return [m for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]


class GeminiAPIAdapter(Adapter):
    async def respond(self, ctx: TurnContext) -> Reply:
        model = self.options.get("model")
        if not model:
            raise AdapterError("gemini_api には model の指定が要ります（dm models gemini で一覧）")
        key = api_key(self.options.get("api_key_env", "GEMINI_API_KEY"))
        gen: dict[str, Any] = {}
        if "temperature" in self.options:
            gen["temperature"] = float(self.options["temperature"])
        if "thinking_budget" in self.options:
            gen["thinkingConfig"] = {"thinkingBudget": int(self.options["thinking_budget"])}
        if ctx.response_schema is not None:
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = to_gemini_schema(ctx.response_schema)
        body: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": ctx.prompt}]}]}
        if gen:
            body["generationConfig"] = gen
        url = f"{self.options.get('base_url', BASE_URL)}/models/{model}:generateContent"
        # 標準ライブラリの HTTP は同期なので別スレッドで（タイムアウトはエンジン側でも強制される）
        data = await asyncio.to_thread(_post, url, body, key, self.timeout_s)
        return self.parse(data, ctx)

    def parse(self, data: dict[str, Any], ctx: TurnContext) -> Reply:
        cands = data.get("candidates") or []
        if not cands:
            fb = (data.get("promptFeedback") or {}).get("blockReason")
            raise AdapterError(f"Gemini API が回答を返しませんでした{f'（{fb}）' if fb else ''}")
        c = cands[0]
        text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []) if not p.get("thought"))
        if not text.strip():
            raise AdapterError(f"Gemini API の回答が空でした（finishReason: {c.get('finishReason')}）")
        structured = None
        if ctx.response_schema is not None:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError:
                structured = None
        u = data.get("usageMetadata") or {}
        usage = {"input_tokens": u.get("promptTokenCount"), "output_tokens": u.get("candidatesTokenCount"),
                 "thinking_tokens": u.get("thoughtsTokenCount"), "cache_read_tokens": u.get("cachedContentTokenCount")}
        return Reply(text, tokens=u.get("totalTokenCount"), structured=structured,
                     meta={"turns": 1, "usage": {k: v for k, v in usage.items() if v is not None},
                           "model_version": data.get("modelVersion")})
