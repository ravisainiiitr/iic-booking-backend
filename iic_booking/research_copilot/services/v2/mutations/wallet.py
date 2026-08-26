"""
Phase C wallet mutation wrappers (DISABLED by default).

Never credit/debit wallets from Copilot without COPILOT_WALLET_* flags,
confirmation token, and idempotency key. Prefer deep-link prepare cards.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings


def _flag(name: str) -> bool:
    return bool(getattr(settings, name, False))


def prepare_wallet_recharge(*, user, amount=None) -> dict[str, Any]:
    return {
        "ok": True,
        "phase": "prepare",
        "requires_confirmation": True,
        "executable": _flag("COPILOT_WALLET_RECHARGE"),
        "message": "Phase A: open Wallet to recharge. Mutation execute is disabled.",
        "href": "/wallet",
    }


def execute_wallet_recharge(*, user, confirmation_token: str, idempotency_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not _flag("COPILOT_WALLET_RECHARGE"):
        return {"ok": False, "error": "COPILOT_WALLET_RECHARGE_DISABLED", "message": "Wallet recharge via Copilot is disabled."}
    raise NotImplementedError("Phase C not enabled")


def execute_wallet_credit_request(*, user, confirmation_token: str, idempotency_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not _flag("COPILOT_WALLET_CREDIT"):
        return {"ok": False, "error": "COPILOT_WALLET_CREDIT_DISABLED", "message": "Wallet credit via Copilot is disabled."}
    raise NotImplementedError("Phase C not enabled")
