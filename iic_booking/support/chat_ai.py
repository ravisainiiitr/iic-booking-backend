"""
AI-powered chat for the virtual assistant.
Uses OpenAI API when OPENAI_API_KEY is set. Returns (reply_text, should_escalate).
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Instruct the model to output this when the user should get a support ticket
ESCALATE_MARKER = "ESCALATE_TICKET"

SYSTEM_PROMPT = """You are a helpful, friendly assistant for an equipment booking portal. Users can book equipment, manage slots, use their wallet, and submit urgent requests.

Answer briefly and in a clear, conversational way. Only use information about this portal: booking equipment from the dashboard or Equipments page, viewing slots on each equipment page, wallet and recharge in the Wallet section, cancellations from My Bookings, and urgent requests (no slot / reviewer urgent) from the equipment page or My Urgent Requests.

If the user asks to speak to a human, wants a refund, has a complaint, needs something you cannot help with, or the query is clearly beyond simple how-to or general info, reply with exactly this line on its own (no other text before or after):
ESCALATE_TICKET

Otherwise give a short, helpful answer in one or two sentences. Do not make up features or URLs."""


def get_ai_reply(user_message: str) -> tuple[str | None, bool]:
    """
    Call OpenAI chat and return (reply_text, should_escalate).
    If API key is missing or the call fails, returns (None, False) and the caller can fall back to FAQ.
    """
    api_key = getattr(settings, "OPENAI_API_KEY", None) or ""
    if not api_key.strip():
        return None, False

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key.strip())
        response = client.chat.completions.create(
            model=settings.OPENAI_CHAT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            max_tokens=300,
            temperature=0.3,
        )
        content = (response.choices[0].message.content or "").strip()
        if ESCALATE_MARKER in content or content == ESCALATE_MARKER:
            return None, True
        return content or None, False
    except Exception as e:
        logger.warning("Chat AI request failed: %s", e, exc_info=True)
        return None, False
