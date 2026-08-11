"""Provider-agnostic LLM gateway — OpenAI adapter first (AI.1)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    finish_reason: str = ""


class LLMGateway:
    def complete(self, messages: list[dict], *, max_tokens: int = 800) -> LLMResult | None:
        raise NotImplementedError

    def stream(self, messages: list[dict], *, max_tokens: int = 800) -> Iterator[str]:
        result = self.complete(messages, max_tokens=max_tokens)
        if result and result.text:
            yield result.text


class OpenAIGateway(LLMGateway):
    def __init__(self, api_key: str, model: str, *, timeout_seconds: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def _client(self):
        from openai import OpenAI

        return OpenAI(api_key=self.api_key, timeout=self.timeout_seconds)

    def complete(self, messages: list[dict], *, max_tokens: int = 800) -> LLMResult | None:
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
            return LLMResult(text=content, model=self.model, provider="openai", finish_reason=reason)
        except Exception:
            logger.warning("Research Copilot LLM complete failed", exc_info=True)
            return None

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


class FallbackGateway(LLMGateway):
    """Deterministic guidance when no API key / LLM unavailable."""

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


def get_gateway() -> LLMGateway:
    api_key = (getattr(settings, "OPENAI_API_KEY", None) or "").strip()
    model = (
        getattr(settings, "RESEARCH_COPILOT_MODEL", None)
        or getattr(settings, "OPENAI_CHAT_MODEL", None)
        or "gpt-4o-mini"
    )
    timeout = float(getattr(settings, "RESEARCH_COPILOT_LLM_TIMEOUT_SECONDS", 30) or 30)
    if api_key:
        return OpenAIGateway(api_key=api_key, model=model, timeout_seconds=timeout)
    return FallbackGateway()


def default_max_tokens() -> int:
    return int(getattr(settings, "RESEARCH_COPILOT_MAX_TOKENS", 800) or 800)
