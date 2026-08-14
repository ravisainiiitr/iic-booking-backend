"""Provider-agnostic LLM gateway (AI.1) + Ollama provider (AI.17).

Copilot orchestration talks only to LLMGateway. Provider selection is
configuration-driven (COPILOT_LLM_PROVIDER). Portal tools / knowledge /
authorization remain outside this module.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    finish_reason: str = ""
    latency_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error_category: str = ""


@dataclass
class ProviderHealth:
    provider: str
    model: str
    status: str  # available | unavailable | misconfigured | fallback
    detail: str = ""
    latency_ms: int = 0
    extras: dict[str, Any] = field(default_factory=dict)

    def as_public_dict(self) -> dict[str, Any]:
        """Safe for staff/admin diagnostics — no secrets, no internal hostnames."""
        return {
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            **{k: v for k, v in self.extras.items() if k not in {"base_url", "api_key"}},
        }


class LLMGateway:
    provider_name = "abstract"

    def complete(self, messages: list[dict], *, max_tokens: int = 800) -> LLMResult | None:
        raise NotImplementedError

    def generate(self, messages: list[dict], *, max_tokens: int = 800) -> LLMResult | None:
        """Preferred alias for complete() (AI.17 provider surface)."""
        return self.complete(messages, max_tokens=max_tokens)

    def stream(self, messages: list[dict], *, max_tokens: int = 800) -> Iterator[str]:
        result = self.complete(messages, max_tokens=max_tokens)
        if result and result.text:
            yield result.text

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.provider_name,
            model=getattr(self, "model", "") or "",
            status="available",
        )

    def model_available(self) -> bool:
        health = self.health()
        return health.status in {"available", "fallback"}


class FakeInferenceProvider(LLMGateway):
    """
    Deterministic provider for unit tests (AI.17).

    Does not call network. Configure via get_gateway() when
    COPILOT_PROVIDER / COPILOT_LLM_PROVIDER = fake.
    """

    provider_name = "fake"
    model = "fake-test"

    def __init__(self, *, reply: str | None = None):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict], *, max_tokens: int = 800) -> LLMResult | None:
        self.calls.append(list(messages))
        text = self.reply
        if text is None:
            user_text = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    user_text = (m.get("content") or "").strip()
                    break
            text = f"[fake] {user_text[:200]}" if user_text else "[fake] ok"
        return LLMResult(text=text, model=self.model, provider="fake", finish_reason="stop", latency_ms=1)

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider="fake", model=self.model, status="available", detail="test double")


class OpenAIGateway(LLMGateway):
    provider_name = "openai"

    def __init__(self, api_key: str, model: str, *, timeout_seconds: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def _client(self):
        from openai import OpenAI

        return OpenAI(api_key=self.api_key, timeout=self.timeout_seconds)

    def complete(self, messages: list[dict], *, max_tokens: int = 800) -> LLMResult | None:
        started = time.monotonic()
        try:
            client = self._client()
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.3,
            )
            content = (response.choices[0].message.content or "").strip()
            reason = getattr(response.choices[0], "finish_reason", "") or ""
            usage = getattr(response, "usage", None)
            latency_ms = int((time.monotonic() - started) * 1000)
            return LLMResult(
                text=content,
                model=self.model,
                provider="openai",
                finish_reason=reason,
                latency_ms=latency_ms,
                prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
                completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            )
        except Exception as exc:
            logger.warning("Research Copilot LLM complete failed (%s)", type(exc).__name__, exc_info=True)
            return LLMResult(
                text="",
                model=self.model,
                provider="openai",
                latency_ms=int((time.monotonic() - started) * 1000),
                error_category=_error_category(exc),
            )

    def stream(self, messages: list[dict], *, max_tokens: int = 800) -> Iterator[str]:
        try:
            client = self._client()
            stream = client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.3,
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and getattr(delta, "content", None):
                    yield delta.content
        except Exception:
            logger.warning("Research Copilot LLM stream failed; falling back", exc_info=True)
            result = self.complete(messages, max_tokens=max_tokens)
            if result and result.text:
                yield result.text

    def health(self) -> ProviderHealth:
        if not (self.api_key or "").strip():
            return ProviderHealth(
                provider="openai",
                model=self.model,
                status="misconfigured",
                detail="OPENAI_API_KEY missing",
            )
        return ProviderHealth(provider="openai", model=self.model, status="available", detail="configured")


class OllamaGateway(LLMGateway):
    """HTTP client for a separately running Ollama service (AI.17)."""

    provider_name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float = 60.0,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def _post_json(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}

    def _get_json(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=min(self.timeout_seconds, 10.0)) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}

    def complete(self, messages: list[dict], *, max_tokens: int = 800) -> LLMResult | None:
        started = time.monotonic()
        try:
            # Prefer OpenAI-compatible chat endpoint (Ollama ≥0.1.x)
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "max_tokens": max_tokens,
                "temperature": 0.3,
                "options": {
                    "num_predict": max_tokens,
                    "temperature": 0.3,
                },
            }
            try:
                data = self._post_json("/v1/chat/completions", payload)
                choices = data.get("choices") or []
                if choices:
                    msg = choices[0].get("message") or {}
                    content = (msg.get("content") or "").strip()
                    usage = data.get("usage") or {}
                    return LLMResult(
                        text=content,
                        model=data.get("model") or self.model,
                        provider="ollama",
                        finish_reason=(choices[0].get("finish_reason") or ""),
                        latency_ms=int((time.monotonic() - started) * 1000),
                        prompt_tokens=usage.get("prompt_tokens"),
                        completion_tokens=usage.get("completion_tokens"),
                    )
            except urllib.error.HTTPError as http_exc:
                if http_exc.code not in (404, 405):
                    raise
                # Fall through to native /api/chat

            native = self._post_json(
                "/api/chat",
                {
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": {
                        "num_predict": max_tokens,
                        "temperature": 0.3,
                    },
                },
            )
            message = native.get("message") or {}
            content = (message.get("content") or "").strip()
            return LLMResult(
                text=content,
                model=native.get("model") or self.model,
                provider="ollama",
                finish_reason="stop" if native.get("done") else "",
                latency_ms=int((time.monotonic() - started) * 1000),
                prompt_tokens=native.get("prompt_eval_count"),
                completion_tokens=native.get("eval_count"),
            )
        except Exception as exc:
            logger.warning("Research Copilot Ollama complete failed (%s)", type(exc).__name__, exc_info=True)
            return LLMResult(
                text="",
                model=self.model,
                provider="ollama",
                latency_ms=int((time.monotonic() - started) * 1000),
                error_category=_error_category(exc),
            )

    def stream(self, messages: list[dict], *, max_tokens: int = 800) -> Iterator[str]:
        # Keep stream simple: non-streaming complete then yield (avoids partial tool misuse).
        result = self.complete(messages, max_tokens=max_tokens)
        if result and result.text:
            yield result.text

    def health(self) -> ProviderHealth:
        if not self.base_url or not self.model:
            return ProviderHealth(
                provider="ollama",
                model=self.model or "",
                status="misconfigured",
                detail="OLLAMA_BASE_URL or model missing",
            )
        started = time.monotonic()
        try:
            tags = self._get_json("/api/tags")
            models = [m.get("name") for m in (tags.get("models") or []) if m.get("name")]
            latency_ms = int((time.monotonic() - started) * 1000)
            model_ok = any(
                self.model == name or name.startswith(f"{self.model}:") or name.startswith(self.model)
                for name in models
            )
            if not model_ok and models:
                return ProviderHealth(
                    provider="ollama",
                    model=self.model,
                    status="unavailable",
                    detail="configured model not pulled",
                    latency_ms=latency_ms,
                    extras={"models_pulled_count": len(models)},
                )
            if not models:
                return ProviderHealth(
                    provider="ollama",
                    model=self.model,
                    status="unavailable",
                    detail="no models pulled",
                    latency_ms=latency_ms,
                )
            return ProviderHealth(
                provider="ollama",
                model=self.model,
                status="available",
                detail="reachable",
                latency_ms=latency_ms,
                extras={"models_pulled_count": len(models)},
            )
        except Exception as exc:
            return ProviderHealth(
                provider="ollama",
                model=self.model,
                status="unavailable",
                detail=_error_category(exc),
                latency_ms=int((time.monotonic() - started) * 1000),
            )


class FallbackGateway(LLMGateway):
    """Deterministic guidance when LLM provider unavailable / misconfigured."""

    provider_name = "fallback"
    model = "fallback"

    def complete(self, messages: list[dict], *, max_tokens: int = 800) -> LLMResult | None:
        user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_text = (m.get("content") or "").strip()
                break
        lower = user_text.lower()
        if any(k in lower for k in ("ticket", "human", "support", "talk to")):
            reply = (
                "I can help you open a support ticket with the laboratory team. "
                "Please use **Tickets** in the portal menu, or say you want to escalate.\n"
                "ESCALATE_HUMAN"
            )
        elif any(k in lower for k in ("wallet", "recharge", "balance")):
            reply = (
                "For wallet balance and recharge, open the **Wallet** section. "
                "When Copilot tools are available I will summarize your accessible balance from portal data. "
                "I will not invent balances or perform wallet mutations."
            )
        elif any(k in lower for k in ("book", "slot", "fesem", "afm", "tem")):
            reply = (
                "To book equipment: open **Equipments**, pick the instrument, review slots, and confirm. "
                "Describe your sample and measurement need if you want advisory guidance "
                "(e.g. elemental mapping → FESEM+EDS). I will not invent availability — "
                "use Review & Confirm in the portal before any booking is created."
            )
        else:
            reply = (
                "I am **IIC Research Copilot**. Ask about booking, equipment selection, sample status, "
                "results, wallet, Remote Analysis software, or documentation. "
                "Live answers use portal data and institute documents when available — I will not fabricate results."
            )
        return LLMResult(text=reply, model="fallback", provider="local")

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider="fallback",
            model="fallback",
            status="fallback",
            detail="deterministic local replies only",
        )


def configured_provider_name() -> str:
    """
    Resolve provider from COPILOT_PROVIDER (preferred) or COPILOT_LLM_PROVIDER.

    Default: ollama — production inference must not require OPENAI_API_KEY.
    """
    raw = (
        getattr(settings, "COPILOT_PROVIDER", None)
        or getattr(settings, "COPILOT_LLM_PROVIDER", None)
        or "ollama"
    )
    raw = str(raw).strip().lower()
    if raw in {"ollama", "openai", "fallback", "auto", "fake"}:
        return raw
    return "ollama"


def ollama_model_name() -> str:
    return (
        (getattr(settings, "OLLAMA_MODEL", None) or "").strip()
        or (getattr(settings, "RESEARCH_COPILOT_MODEL", None) or "").strip()
        or "llama3.2:3b"
    )


def openai_model_name() -> str:
    return (
        (getattr(settings, "RESEARCH_COPILOT_MODEL", None) or "").strip()
        or (getattr(settings, "OPENAI_CHAT_MODEL", None) or "").strip()
        or "gpt-4o-mini"
    )


def llm_timeout_seconds() -> float:
    # Ollama local inference can be slower than cloud APIs.
    default = 60 if configured_provider_name() == "ollama" else 30
    return float(getattr(settings, "RESEARCH_COPILOT_LLM_TIMEOUT_SECONDS", default) or default)


def get_gateway() -> LLMGateway:
    """
    Select LLM provider.

    - ollama (default): does NOT require OPENAI_API_KEY
    - openai: requires OPENAI_API_KEY; otherwise FallbackGateway
    - fallback: always deterministic
    - fake: FakeInferenceProvider (unit tests only)
    - auto: openai if key present else ollama if base URL set else fallback
    """
    provider = configured_provider_name()
    timeout = llm_timeout_seconds()
    api_key = (getattr(settings, "OPENAI_API_KEY", None) or "").strip()
    ollama_base = (getattr(settings, "OLLAMA_BASE_URL", None) or "http://127.0.0.1:11434").strip()

    if provider == "fallback":
        return FallbackGateway()

    if provider == "fake":
        return FakeInferenceProvider()

    if provider == "openai":
        if not api_key:
            logger.warning("COPILOT_PROVIDER=openai but OPENAI_API_KEY missing; using fallback")
            return FallbackGateway()
        return OpenAIGateway(api_key=api_key, model=openai_model_name(), timeout_seconds=timeout)

    if provider == "auto":
        if api_key:
            return OpenAIGateway(api_key=api_key, model=openai_model_name(), timeout_seconds=timeout)
        if ollama_base:
            return OllamaGateway(base_url=ollama_base, model=ollama_model_name(), timeout_seconds=timeout)
        return FallbackGateway()

    # default: ollama
    return OllamaGateway(base_url=ollama_base, model=ollama_model_name(), timeout_seconds=timeout)


def provider_health() -> ProviderHealth:
    """Probe configured provider without exposing secrets."""
    gateway = get_gateway()
    try:
        return gateway.health()
    except Exception as exc:
        return ProviderHealth(
            provider=configured_provider_name(),
            model="",
            status="unavailable",
            detail=_error_category(exc),
        )


def default_max_tokens() -> int:
    # AI.21.2: 800 completion tokens on CPU llama3.2:1b routinely approaches the
    # 60s provider timeout. Keep answers concise; raise via env only if measured.
    return int(getattr(settings, "RESEARCH_COPILOT_MAX_TOKENS", 160) or 160)


def _error_category(exc: BaseException) -> str:
    name = type(exc).__name__
    msg = str(exc).lower()
    if "timeout" in name.lower() or "timed out" in msg:
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return "invalid_model_or_path"
        if exc.code == 429:
            return "rate_limited"
        if exc.code >= 500:
            return "provider_5xx"
        return f"http_{exc.code}"
    if isinstance(exc, urllib.error.URLError) or "connection" in msg:
        return "network"
    return name or "error"
