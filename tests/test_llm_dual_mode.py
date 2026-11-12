"""LLM 双模驱动测试（v3.2）：客户 API + 本地 Agent 模型，全程 mock，零外网。"""
from __future__ import annotations

import json

import pytest

from trading_system.llm.client import (LocalAgentClient, OpenAICompatClient,
                                       default_client)
from trading_system.redline import LLMUnavailable


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _chat_payload(text: str):
    return {"choices": [{"message": {"content": text}}]}


# ---------------------------------------------------------------- API 模式
def test_openai_compat_success(monkeypatch):
    seen = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        seen["url"] = url
        seen["auth"] = headers.get("Authorization")
        seen["payload"] = json.loads(data)
        seen["model"] = seen["payload"]["model"]
        return _Resp(200, _chat_payload('{"score": 7, "reason": "ok"}'))

    monkeypatch.setattr("requests.post", fake_post)
    c = OpenAICompatClient(base_url="https://api.deepseek.com/v1",
                           api_key="sk-x", model="deepseek-chat")
    out = c.complete_json(system="s", user="u", schema_hint={"score": 0})
    assert out["score"] == 7
    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-x"
    assert seen["model"] == "deepseek-flash"
    assert seen["payload"]["response_format"] == {"type": "json_object"}
    assert seen["payload"]["thinking"] == {"type": "disabled"}
    assert seen["payload"]["max_tokens"] > 300_000


def test_openai_compat_unconfigured_raises(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(LLMUnavailable, match="未配置"):
        OpenAICompatClient()


def test_openai_compat_http_error(monkeypatch):
    monkeypatch.setattr("requests.post",
                        lambda *a, **k: _Resp(401, text="unauthorized"))
    c = OpenAICompatClient(base_url="https://x/v1", api_key="bad", model="m",
                           max_retries=0)
    with pytest.raises(LLMUnavailable, match="401"):
        c.complete_json(system="s", user="u", schema_hint={})


def test_openai_compat_non_json_output(monkeypatch):
    monkeypatch.setattr("requests.post",
                        lambda *a, **k: _Resp(200, _chat_payload("我觉得会涨")))
    c = OpenAICompatClient(base_url="https://x/v1", api_key="k", model="m",
                           max_retries=0)
    with pytest.raises(LLMUnavailable):
        c.complete_json(system="s", user="u", schema_hint={})


# ---------------------------------------------------------------- 本地探测
def test_local_detect_explicit_url(monkeypatch):
    monkeypatch.setenv("LLM_LOCAL_URL", "http://localhost:9999/v1")
    monkeypatch.setenv("LLM_LOCAL_MODEL", "my-local-model")
    LocalAgentClient._cache = None
    c = LocalAgentClient.detect()
    assert c.base_url == "http://localhost:9999/v1"
    assert c.model == "my-local-model"
    assert c.name == "local"


def test_local_detect_ollama(monkeypatch):
    monkeypatch.delenv("LLM_LOCAL_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    LocalAgentClient._cache = None

    def fake_get(url, timeout=None):
        if "11434" in url:
            return _Resp(200, {"models": [{"name": "qwen3:32b"}]})
        raise ConnectionError("down")

    monkeypatch.setattr("requests.get", fake_get)
    c = LocalAgentClient.detect()
    assert c.base_url == "http://localhost:11434/v1"
    assert c.model == "qwen3:32b"


def test_local_detect_all_down_raises(monkeypatch):
    monkeypatch.delenv("LLM_LOCAL_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    LocalAgentClient._cache = None
    monkeypatch.setattr("requests.get",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError()))
    with pytest.raises(LLMUnavailable, match="探测失败"):
        LocalAgentClient.detect()


# ---------------------------------------------------------------- default_client 选择链
def test_default_client_backend_api(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "api")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    c = default_client()
    assert isinstance(c, OpenAICompatClient) and c.model == "deepseek-flash"


def test_default_client_backend_local(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "local")
    monkeypatch.setenv("LLM_LOCAL_URL", "http://localhost:9999/v1")
    LocalAgentClient._cache = None
    c = default_client()
    assert c.name == "local"


def test_default_client_auto_falls_to_api(monkeypatch):
    """auto：已配置 LLM_BASE_URL 时直接选 DeepSeek API，不探测 Kimi。"""
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    import sys
    monkeypatch.setitem(sys.modules, "agent_gw", None)  # 模拟 SDK 不存在
    c = default_client()
    assert isinstance(c, OpenAICompatClient) and c.model == "deepseek-flash"


def test_default_client_auto_never_falls_back_to_kimi(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_LOCAL_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    LocalAgentClient._cache = None
    monkeypatch.setattr("requests.get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError()))
    c = default_client()
    assert c.__class__.__name__ == "DisabledLLMClient"
    with pytest.raises(LLMUnavailable):
        c.complete_json(system="s", user="u", schema_hint={})


def test_redline_no_rule_fallback():
    """红线静态审查：client.py 不得出现规则回退语义（关键词打分等）。"""
    import inspect
    import trading_system.llm.client as m
    src = inspect.getsource(m)
    for banned in ("keyword_score", "fallback_to_rules",
                   "rule_based_sentiment", "def _rule_fallback"):
        assert banned not in src
