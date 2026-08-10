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
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def complete(self, messages: list[dict], *, max_tokens: int = 800) -> LLMResult | None:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=self.api_key)
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
            from openai import OpenAI

            client = OpenAI(api_key=self.api_key)
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
                "For wallet balance, recharge, and credit facility, open the **Wallet** section. "
                "I cannot read live balances until tool calling (AI.3) is enabled. "
                "Tell me what you need (balance, recharge, grant, refund) and I will guide the steps."
            )
        elif any(k in lower for k in ("book", "slot", "fesem", "afm", "tem")):
            reply = (
                "To book equipment: open **Equipments**, pick the instrument, review slots, and confirm. "
                "Describe your sample and measurement need if you want advisory guidance "
                "(e.g. elemental mapping → FESEM+EDS). I will not invent availability."
            )
        else:
            reply = (
                "I am **IIC Research Copilot**. Ask about booking, equipment selection, sample status, "
                "wallet, Remote Analysis, DSA (department admins), or documentation. "
                "Live data actions require upcoming tool integrations — I will not fabricate results."
            )
        return LLMResult(text=reply, model="fallback", provider="local")


def get_gateway() -> LLMGateway:
    api_key = (getattr(settings, "OPENAI_API_KEY", None) or "").strip()
    model = (
        getattr(settings, "RESEARCH_COPILOT_MODEL", None)
        or getattr(settings, "OPENAI_CHAT_MODEL", None)
        or "gpt-4o-mini"
    )
    if api_key:
        return OpenAIGateway(api_key=api_key, model=model)
    return FallbackGateway()
