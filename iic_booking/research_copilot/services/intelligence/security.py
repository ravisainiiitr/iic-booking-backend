"""
Prompt-injection and privilege-escalation screening for Copilot messages.

Copilot never gains permissions from message text: every tool runs as the signed-in user and every
id is re-checked against that user. This screen only makes the refusal explicit and auditable
instead of letting such a message reach the language model.
"""

from __future__ import annotations

import re

_PATTERNS = [
    r"\bignore\s+(?:all\s+|any\s+|your\s+|the\s+)?(?:previous|prior|above|earlier|system)?\s*(?:instructions|rules|prompts?|guidelines|restrictions)\b",
    r"\bdisregard\s+(?:all\s+|your\s+|the\s+)?(?:previous|prior|above|system)?\s*(?:instructions|rules|prompts?|guidelines)\b",
    r"\bforget\s+(?:all\s+|your\s+)?(?:previous\s+)?(?:instructions|rules|guidelines)\b",
    r"\b(?:act|behave|pretend|respond)\s+as\s+(?:an?\s+)?(?:admin|administrator|superuser|root|oic|operator|system|developer)\b",
    r"\byou\s+are\s+now\s+(?:an?\s+)?(?:admin|administrator|superuser|root|dan|developer|unrestricted)\b",
    r"\b(?:enable|enter|switch\s+to)\s+(?:developer|admin|god|debug|jailbreak)\s+mode\b",
    r"\b(?:reveal|show|print|display|repeat)\s+(?:me\s+)?(?:your|the)\s+(?:system\s+prompt|hidden\s+instructions|instructions|prompt)\b",
    r"\bsystem\s+prompt\b",
    r"\b(?:bypass|override|disable|skip)\s+(?:the\s+)?(?:security|permissions?|authori[sz]ation|confirmation|validation|rules)\b",
    r"\bwithout\s+(?:asking\s+(?:for\s+)?)?confirmation\b",
    r"\b(?:another|other)\s+user'?s?\s+(?:booking|bookings|wallet|results?|data)\b",
    r"\b(?:all|every)\s+users?'?\s+(?:bookings|wallets?|results|data|balances?)\b",
    r"\b(?:run|execute)\s+(?:this\s+)?(?:sql|python|shell|django|query|command)\b",
    r"\b(?:drop|truncate|delete\s+from|update\s+\w+\s+set)\b.*\b(?:table|users|bookings|wallet)\b",
    r"\bgrant\s+me\s+(?:admin|access|permission|credit)\b",
    r"\b(?:add|credit|transfer)\s+(?:money|funds|balance|rs\.?|inr|\u20b9)\s*\d*\s*(?:to|into)\s+my\s+wallet\s+(?:for\s+free|without)\b",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in _PATTERNS]

REFUSAL = (
    "I can only act with your own account's permissions, and I can't change my rules, reveal internal "
    "instructions or access other users' data. I can help you find equipment, check your bookings, "
    "estimate costs or answer portal questions."
)


def detect_injection(text: str) -> str | None:
    """Return the matched pattern label when the message tries to override rules or escalate privileges."""
    for pat in _COMPILED:
        if pat.search(text or ""):
            return pat.pattern[:80]
    return None
