from __future__ import annotations

import io
import json
import urllib.error

import pytest

from dartmouth.adapters import gemini_api
from dartmouth.adapters.base import AdapterError, TurnContext
from dartmouth.adapters.gemini_api import GeminiAPIAdapter, to_gemini_schema
from dartmouth.storage.archive import classify_error
from dartmouth.storage.minutes import PROPOSAL_SCHEMA


def ctx(**kw):
    base = dict(prompt="議題について答えて", participant="g", role="researcher", round="independent", topic=1, meeting_id="m")
    base.update(kw)
    return TurnContext(**base)


class FakeResp:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_schema_conversion_keeps_supported_keys():
    s = to_gemini_schema({**PROPOSAL_SCHEMA, "$schema": "x", "additionalProperties": False})
    assert "$schema" not in s and "additionalProperties" not in s
    assert s["properties"]["open_questions"]["items"]["required"] == ["text"]


async def test_respond_sends_key_in_header_and_parses(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout):
        sent["url"], sent["headers"], sent["body"] = req.full_url, dict(req.headers), json.loads(req.data)
        return FakeResp({"candidates": [{"content": {"parts": [{"text": "考え中", "thought": True}, {"text": "結論です"}]}}],
                         "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 30, "totalTokenCount": 150},
                         "modelVersion": "gemini-x"})

    monkeypatch.setenv("GEMINI_API_KEY", "secret-key")
    monkeypatch.setattr(gemini_api.urllib.request, "urlopen", fake_urlopen)
    a = GeminiAPIAdapter("g", "researcher", 30, {"model": "gemini-3.5-flash-lite", "thinking_budget": 0})
    r = await a.respond(ctx())
    assert r.text == "結論です"  # 思考の部分は含めない
    assert r.tokens == 150 and r.meta["usage"]["thinking_tokens"] == 30
    assert "secret-key" not in sent["url"] and sent["headers"]["X-goog-api-key"] == "secret-key"  # キーは URL に出さない
    assert sent["url"].endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert sent["body"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


async def test_structured_output_for_chair(monkeypatch):
    def fake_urlopen(req, timeout):
        body = json.loads(req.data)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert "properties" in body["generationConfig"]["responseSchema"]
        return FakeResp({"candidates": [{"content": {"parts": [{"text": '{"decisions": ["A"], "summary": "s"}'}]}}]})

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(gemini_api.urllib.request, "urlopen", fake_urlopen)
    r = await GeminiAPIAdapter("g", "chair", 30, {"model": "m"}).respond(ctx(round="summary", response_schema=PROPOSAL_SCHEMA))
    assert r.structured == {"decisions": ["A"], "summary": "s"}


async def test_rate_limit_and_missing_key(monkeypatch):
    def fake_urlopen(req, timeout):
        err = {"error": {"code": 429, "message": "Quota exceeded", "details": [{"retryDelay": "37s"}]}}
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(json.dumps(err).encode()))

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(gemini_api.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(AdapterError) as e:
        await GeminiAPIAdapter("g", "researcher", 30, {"model": "m"}).respond(ctx())
    assert "回数上限（429）" in str(e.value) and "37s" in str(e.value)
    assert classify_error(str(e.value)) == "回数上限"
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(AdapterError, match="GEMINI_API_KEY"):
        await GeminiAPIAdapter("g", "researcher", 30, {"model": "m"}).respond(ctx())
