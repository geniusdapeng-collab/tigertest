"""LLM 客户端抽象 — 所有 LLM 驱动环节的唯一入口。

实现（双模驱动，v3.2）：
  - KimiGatewayClient：agent-gw SDK 调 Kimi chat_completion（原有路径）。
  - OpenAICompatClient：客户自有 API——任何 OpenAI 兼容端点
    （DeepSeek/Kimi/Qwen/GLM/Moonshot/自部署 vLLM 等），
    环境变量 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL。
  - LocalAgentClient：本地 AI Coding Agent 主力模型——自动探测本地
    模型端点（LLM_LOCAL_URL → Ollama :11434 → LM Studio :1234 →
    OPENAI_BASE_URL），开发者本地运行零配置接入。
  - default_client()：LLM_BACKEND=kimi|api|local|auto（默认 auto：
    DeepSeek API → local 依次探测，均不可用则 LLMUnavailable；Kimi 只接受显式选择）。

红线 2：本模块没有、也不允许有任何"规则回退"实现（例如关键词打分、
正则情感分析）。LLM 不可用的唯一出路是抛 LLMUnavailable。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..redline import LLMUnavailable
from .model_budget import TokenBudgetError, plan_request

log = logging.getLogger("llm.client")

DEFAULT_MODEL = "deepseek-flash"


class LLMClient(Protocol):
    def complete_json(self, *, system: str, user: str, schema_hint: dict,
                      max_tokens: int | None = None, temperature: float = 0.2) -> dict:
        """请求 LLM 输出 JSON（按 schema_hint 约定结构），解析后返回 dict。
        任何失败（网络/权限/解析）都必须抛 LLMUnavailable。"""
        ...


class DisabledLLMClient:
    """Trusted public-paper boundary: no SDK import, endpoint probe or request."""

    def __init__(self, execution=None):
        self.execution = execution
        self.attempts = 0

    def describe(self) -> dict:
        return {"mode": "disabled", "reason": "trusted public paper policy",
                "attempts": self.attempts, "remote_requests": 0}

    def complete_json(self, *, system: str, user: str, schema_hint: dict,
                      max_tokens: int | None = None, temperature: float = 0.2) -> dict:
        self.attempts += 1
        def refuse():
            raise LLMUnavailable("Model invocation disabled by trusted public paper policy")
        if self.execution is not None:
            return self.execution.call_io(
                "model.disabled.complete_json",
                {"system": system, "user": user, "schema_hint": schema_hint,
                 "max_tokens": max_tokens, "temperature": temperature,
                 "policy": "disabled"}, refuse)
        return refuse()


class KimiGatewayClient:
    """agent-gw chat_completion 实现。"""

    def __init__(self, model: str = DEFAULT_MODEL, timeout: float = 45.0,
                 max_retries: int = 2):
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = None

    def _gw(self):
        if self._client is None:
            try:
                from agent_gw import AgentGwClient
            except ImportError as e:
                raise LLMUnavailable(f"agent-gw SDK 未安装: {e}")
            try:
                self._client = AgentGwClient(timeout=self.timeout)
            except Exception as e:  # 无配置文件 / 无 key
                raise LLMUnavailable(f"agent-gw 初始化失败: {e}")
        return self._client

    def complete_json(self, *, system: str, user: str, schema_hint: dict,
                      max_tokens: int | None = None, temperature: float = 0.2) -> dict:
        prompt = (
            f"{user}\n\n"
            "严格只输出 JSON（不要 markdown 代码块、不要解释），结构遵循："
            f"{json.dumps(schema_hint, ensure_ascii=False)}"
        )
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._gw().chat_completion(
                    model=self.model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": prompt}],
                    max_tokens=max_tokens if max_tokens is not None else 1200,
                    temperature=temperature,
                )
                text = self._extract_text(resp)
                return self._parse_json(text)
            except LLMUnavailable:
                raise
            except Exception as e:
                last_err = e
                log.info("LLM 第 %d 次尝试失败: %s", attempt + 1, e)
        raise LLMUnavailable(f"chat_completion 失败: {last_err}")

    @staticmethod
    def _extract_text(resp: Any) -> str:
        try:
            choices = resp.get("choices") if isinstance(resp, dict) else None
            if choices is None and hasattr(resp, "choices"):
                choices = resp.choices
            ch0 = choices[0]
            msg = ch0.get("message") if isinstance(ch0, dict) else ch0.message
            content = msg.get("content") if isinstance(msg, dict) else msg.content
            if isinstance(content, list):  # 分段 content
                content = "".join(seg.get("text", "") if isinstance(seg, dict)
                                  else getattr(seg, "text", "") for seg in content)
            if not content:
                raise ValueError("空 content")
            return str(content)
        except Exception as e:
            raise LLMUnavailable(f"响应结构无法解析: {e}")

    @staticmethod
    def _parse_json(text: str) -> dict:
        t = text.strip()
        t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
        try:
            obj = json.loads(t)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", t, flags=re.DOTALL)
            if not m:
                raise LLMUnavailable("LLM 输出非 JSON")
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError as e:
                raise LLMUnavailable(f"LLM JSON 解析失败: {e}")
        if not isinstance(obj, dict):
            raise LLMUnavailable("LLM 输出不是 JSON 对象")
        return obj


# ---------------------------------------------------------------- OpenAI 兼容 API
class OpenAICompatClient:
    """客户自有 API：任何 OpenAI 兼容 /chat/completions 端点。

    环境变量：
      LLM_BASE_URL  如 https://api.deepseek.com/v1 或 http://localhost:11434/v1
      LLM_API_KEY   端点密钥（本地端点可填任意非空值，如 ollama）
      LLM_MODEL     模型名；DeepSeek 官方端点固定归一为 deepseek-flash
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float = 60.0,
                 max_retries: int = 2, name: str = "api"):
        raw_base = (base_url or os.environ.get("LLM_BASE_URL") or "").rstrip("/")
        self.base_url = re.sub(r"/anthropic$", "", raw_base, flags=re.IGNORECASE)
        self.api_key = api_key or os.environ.get("LLM_API_KEY") or ""
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        if self._deepseek_endpoint():
            self.model = DEFAULT_MODEL
        self.timeout = timeout
        self.max_retries = max_retries
        self.name = name
        if not self.base_url or not self.model:
            raise LLMUnavailable(
                "OpenAI 兼容端点未配置（需要 LLM_BASE_URL 与 LLM_MODEL）")

    def _deepseek_endpoint(self) -> bool:
        try:
            return urlsplit(self.base_url).hostname == "api.deepseek.com"
        except ValueError:
            return False

    def describe(self) -> str:
        return f"openai-compat:{self.model}@{self.base_url}"

    def complete_json(self, *, system: str, user: str, schema_hint: dict,
                      max_tokens: int | None = None, temperature: float = 0.2) -> dict:
        import requests
        prompt = (
            f"{user}\n\n"
            "严格只输出 JSON（不要 markdown 代码块、不要解释），结构遵循："
            f"{json.dumps(schema_hint, ensure_ascii=False)}"
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": prompt}],
                   "temperature": temperature, "stream": False,
                   "response_format": {"type": "json_object"}}
        if self._deepseek_endpoint():
            payload["thinking"] = {"type": "disabled"}
            try:
                payload, _, _ = plan_request(payload)
            except TokenBudgetError as error:
                raise LLMUnavailable(f"模型预算失败: {error.code}") from None
        else:
            payload["max_tokens"] = max_tokens if max_tokens is not None else 1200
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                r = requests.post(
                    f"{self.base_url}/chat/completions",
                    data=json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode(),
                    headers=headers, timeout=self.timeout)
                if r.status_code != 200:
                    raise LLMUnavailable(f"端点返回 {r.status_code}")
                text = KimiGatewayClient._extract_text(r.json())
                return KimiGatewayClient._parse_json(text)
            except LLMUnavailable:
                raise
            except Exception as e:
                last_err = e
                log.info("LLM(api) 第 %d 次尝试失败: %s", attempt + 1, e)
        raise LLMUnavailable(f"openai-compat 调用失败: {last_err}")


# ---------------------------------------------------------------- 本地 Agent 模型
class LocalAgentClient(OpenAICompatClient):
    """本地 AI Coding Agent 主力模型：自动探测本地模型端点。

    探测顺序（全部快速超时，绝不阻塞管线）：
      1) LLM_LOCAL_URL 显式指定（含可选 LLM_LOCAL_MODEL）
      2) Ollama   http://localhost:11434 （/api/tags 取首个模型）
      3) LM Studio http://localhost:1234 （/v1/models 取首个模型）
      4) OPENAI_BASE_URL 环境变量（部分 Agent 运行时会注入）
    探测结果 5 分钟缓存；全部失败 → LLMUnavailable（红线透传）。
    """

    _cache: tuple[float, "LocalAgentClient"] | None = None

    def __init__(self, timeout: float = 60.0):
        # 请使用 LocalAgentClient.detect()（自动探测）；直接实例化无意义。
        raise LLMUnavailable("LocalAgentClient 请使用 .detect() 自动探测实例化")

    @classmethod
    def detect(cls) -> "LocalAgentClient":
        import requests
        now = __import__("time").time()
        if cls._cache and now - cls._cache[0] < 300:
            return cls._cache[1]

        # 1) 显式指定
        url = os.environ.get("LLM_LOCAL_URL")
        if url:
            c = cls._build(url.rstrip("/"),
                           os.environ.get("LLM_LOCAL_MODEL") or "local")
            cls._cache = (now, c)
            return c

        # 2) Ollama
        try:
            r = requests.get("http://localhost:11434/api/tags", timeout=1.5)
            models = (r.json().get("models") or []) if r.status_code == 200 else []
            if models:
                c = cls._build("http://localhost:11434/v1",
                               models[0].get("name") or models[0].get("model"))
                cls._cache = (now, c)
                return c
        except Exception:
            pass

        # 3) LM Studio
        try:
            r = requests.get("http://localhost:1234/v1/models", timeout=1.5)
            data = (r.json().get("data") or []) if r.status_code == 200 else []
            if data:
                c = cls._build("http://localhost:1234/v1",
                               data[0].get("id"))
                cls._cache = (now, c)
                return c
        except Exception:
            pass

        # 4) OPENAI_BASE_URL
        base = os.environ.get("OPENAI_BASE_URL")
        if base and os.environ.get("OPENAI_MODEL"):
            c = cls._build(base.rstrip("/"), os.environ["OPENAI_MODEL"],
                           os.environ.get("OPENAI_API_KEY"))
            cls._cache = (now, c)
            return c

        raise LLMUnavailable("本地模型端点探测失败（Ollama/LM Studio/OPENAI_BASE_URL 均不可用）")

    @classmethod
    def _build(cls, base_url: str, model: str, api_key: str | None = None) -> "LocalAgentClient":
        c = cls.__new__(cls)
        OpenAICompatClient.__init__(c, base_url=base_url,
                                    api_key=api_key or "ollama",
                                    model=model, name="local")
        return c


def default_client() -> LLMClient:
    """生产默认客户端（双模驱动）。调用方必须捕获 LLMUnavailable → 红线透传。

    LLM_BACKEND=kimi|api|local|auto（默认 auto：DeepSeek API → local）。
    Kimi 只在显式设置 LLM_BACKEND=kimi 时使用。
    """
    backend = (os.environ.get("LLM_BACKEND") or "auto").lower()
    if backend == "kimi":
        return KimiGatewayClient()
    if backend == "api":
        return OpenAICompatClient()
    if backend == "local":
        return LocalAgentClient.detect()
    # auto：DeepSeek/OpenAI-compatible API（已配置）→ local（可探测）。
    # Kimi SDK 不再作为隐式默认，避免历史 Kimi 配置被自动实际调用。
    if os.environ.get("LLM_BASE_URL"):
        return OpenAICompatClient()
    try:
        return LocalAgentClient.detect()
    except LLMUnavailable:
        pass
    # Legacy demo/research callers still need a client object so deterministic
    # stages can run and semantic stages disclose passthrough honestly. Never
    # instantiate Kimi merely because no new DeepSeek configuration exists.
    return DisabledLLMClient()
