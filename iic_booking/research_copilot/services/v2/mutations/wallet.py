"""
Phase C wallet / financial mutation wrappers.

prepare_* builds confirmation proposals (no money movement).
execute_* calls existing portal domain APIs and is gated by COPILOT_WALLET_* flags.

Copilot never:
- marks Razorpay payment successful from chat
- approves wallet credit
- writes SubWallet balances directly
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings

from iic_booking.research_copilot.services.v2.mutations import domain_bridge
from iic_booking.research_copilot.services.v2.mutations import idempotency as idem
from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store


def _flag(name: str) -> bool:
    return bool(getattr(settings, name, False))


def _proposals_allowed() -> bool:
    return _flag("COPILOT_FINANCIAL_PROPOSALS") or _flag("COPILOT_WALLET_RECHARGE") or _flag("COPILOT_WALLET_CREDIT")


def _safe_error(code: str, message: str, **extra) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message, **extra}


def _audit(*, user, action: str, detail: dict[str, Any], message: str = "") -> None:
    try:
        from iic_booking.research_copilot.models import AuditAction
        from iic_booking.research_copilot.services import audit as audit_svc

        audit_svc.write_audit(
            action=AuditAction.TOOL_EXECUTED,
            message=message or action,
            user=user,
            detail={k: v for k, v in detail.items() if k not in {"confirmation_token", "token", "razorpay_key", "key"}},
        )
    except Exception:  # noqa: BLE001
        pass


def parse_inr_amount(text: str = "", explicit: Any = None) -> Decimal | None:
    if explicit is not None and str(explicit).strip() != "":
        try:
            return Decimal(str(explicit).replace(",", "").replace("₹", "").strip())
        except (InvalidOperation, ValueError, TypeError):
            return None
    raw = text or ""
    m = re.search(r"(?:₹|rs\.?\s*|inr\s*)?\s*([\d,]{2,}(?:\.\d{1,2})?)", raw, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d{3,7}(?:\.\d{1,2})?)\b", raw)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _wallet_snapshot(user) -> dict[str, Any]:
    try:
        from iic_booking.research_copilot.services import tools as tools_svc

        result = tools_svc._get_wallet(arguments={}, user=user)
        data = (result or {}).get("data") or {}
        return {
            "balance": data.get("balance"),
            "sub_wallets": data.get("sub_wallets") or [],
            "currency": data.get("currency") or "INR",
        }
    except Exception:  # noqa: BLE001
        return {"balance": None, "sub_wallets": [], "currency": "INR"}


def _default_department_id(user, snap: dict[str, Any] | None = None) -> int | None:
    snap = snap or {}
    subs = snap.get("sub_wallets") or []
    if subs:
        try:
            return int(subs[0].get("department_id"))
        except (TypeError, ValueError):
            pass
    dept = getattr(user, "department_id", None)
    return int(dept) if dept else None


def prepare_wallet_recharge(*, user, amount=None, text: str = "", department_id: int | None = None) -> dict[str, Any]:
    if user is None or not getattr(user, "is_authenticated", False):
        return _safe_error("AUTH_REQUIRED", "Sign in to prepare a wallet recharge.")

    amt = parse_inr_amount(text, amount)
    snap = _wallet_snapshot(user)
    if amt is None or amt <= 0:
        return {
            "ok": True,
            "action": "WALLET_RECHARGE",
            "status": "NEEDS_AMOUNT",
            "confirmation_required": False,
            "wallet_balance": snap.get("balance"),
            "message": "How much would you like to recharge? Example: “Recharge ₹5000”.",
            "portal_href": "/wallet",
            "suggested_actions": [
                {"id": "open_wallet", "label": "Open Wallet", "href": "/wallet", "enabled": True},
            ],
        }

    if amt > Decimal("100000"):
        return _safe_error("AMOUNT_TOO_LARGE", "Maximum single recharge is ₹1,00,000 via the portal payment flow.")

    dept_id = department_id or _default_department_id(user, snap)
    try:
        expected = Decimal(str(snap.get("balance") or 0)) + amt
    except (InvalidOperation, TypeError, ValueError):
        expected = None

    payload = {
        "amount": str(amt),
        "department_id": dept_id,
        "currency": "INR",
        "wallet_balance_before": snap.get("balance"),
        "expected_balance_after": str(expected) if expected is not None else None,
        "payment_method": "RAZORPAY",
        "source": "COPILOT",
        "note": "Estimate only until Razorpay checkout completes and the portal webhook settles the credit.",
    }
    record = prop_store.create_proposal(user=user, action="WALLET_RECHARGE", payload=payload)
    executable = _flag("COPILOT_WALLET_RECHARGE")
    _audit(
        user=user,
        action="prepare_wallet_recharge",
        detail={"proposal_id": record["proposal_id"], "amount": str(amt), "ok": True},
    )
    return {
        "ok": True,
        "action": "WALLET_RECHARGE",
        "status": "READY_FOR_CONFIRMATION",
        "proposal_id": record["proposal_id"],
        "confirmation_token": record["confirmation_token"],
        "confirmation_required": True,
        "requires_confirmation": True,
        "executable": executable,
        "expires_at": record["expires_at"],
        "amount": str(amt),
        "wallet_balance": snap.get("balance"),
        "expected_balance_after": payload["expected_balance_after"],
        "department_id": dept_id,
        "message": (
            f"Recharge proposal: ₹{amt}. Current wallet ≈ ₹{snap.get('balance')}. "
            "Confirm to start the portal Razorpay payment. Copilot will not mark payment as successful from chat."
            + ("" if executable else " Execute is currently disabled (flag OFF).")
        ),
        "portal_href": "/wallet",
    }


def execute_wallet_recharge(
    *,
    user,
    proposal_id: str,
    confirmation_token: str,
    idempotency_key: str = "",
) -> dict[str, Any]:
    if not _flag("COPILOT_WALLET_RECHARGE"):
        return _safe_error(
            "COPILOT_WALLET_RECHARGE_DISABLED",
            "Wallet recharge via Copilot is disabled. Use the Wallet page, or enable COPILOT_WALLET_RECHARGE after controlled financial E2E.",
            proposal_id=proposal_id,
        )

    key = idempotency_key or idem.make_idempotency_key(user=user, action="WALLET_RECHARGE", proposal_id=proposal_id)
    cached = idem.get_cached_result(user=user, idempotency_key=key)
    if cached:
        return {**cached, "idempotent_replay": True}

    prop, err = prop_store.validate_proposal_for_user(
        user=user, proposal_id=proposal_id, confirmation_token=confirmation_token, expected_action="WALLET_RECHARGE"
    )
    if err:
        return _safe_error(err, "Invalid or expired recharge confirmation.")

    payload = prop.get("payload") or {}
    amount = payload.get("amount")
    department_id = payload.get("department_id")
    status_code, data = domain_bridge.call_razorpay_wallet_recharge_create_order(
        user=user, amount=str(amount), department_id=int(department_id) if department_id else None
    )
    ok = 200 <= status_code < 300
    result = {
        "ok": ok,
        "action": "WALLET_RECHARGE",
        "status_code": status_code,
        "proposal_id": proposal_id,
        "idempotency_key": key,
        "data": data if ok else None,
        "error": None if ok else (data.get("code") or data.get("error") or "RECHARGE_INIT_FAILED"),
        "message": (
            "Payment order created. Complete Razorpay checkout in the portal. Balance updates only after gateway settlement."
            if ok
            else (data.get("error") or data.get("message") or "Could not start recharge payment.")
        ),
        "portal_href": "/wallet",
        "source": "COPILOT",
    }
    if ok:
        prop_store.invalidate_proposal(proposal_id)
        idem.store_result(user=user, idempotency_key=key, result=result)
    _audit(
        user=user,
        action="execute_wallet_recharge",
        detail={
            "ok": ok,
            "proposal_id": proposal_id,
            "idempotency_key": key,
            "amount": amount,
            "order_id": (data or {}).get("order_id") or (data or {}).get("razorpay_order_id"),
            "error": result.get("error"),
        },
        message="wallet_recharge_init",
    )
    return result


def prepare_wallet_credit(*, user, amount=None, text: str = "", purpose: str = "") -> dict[str, Any]:
    if user is None or not getattr(user, "is_authenticated", False):
        return _safe_error("AUTH_REQUIRED", "Sign in to request wallet credit.")

    amt = parse_inr_amount(text, amount)
    snap = _wallet_snapshot(user)

    # Deterministic eligibility / outstanding via domain summary when available
    status_code, summary = domain_bridge.call_wallet_credit_summary(user=user)
    eligibility = (summary or {}).get("eligibility") if isinstance(summary, dict) else None
    outstanding = None
    if isinstance(summary, dict):
        outstanding = summary.get("outstanding_amount") or summary.get("outstanding") or (summary.get("active") or {}).get("outstanding_amount")

    if amt is None or amt <= 0:
        return {
            "ok": True,
            "action": "WALLET_CREDIT",
            "status": "NEEDS_AMOUNT",
            "confirmation_required": False,
            "wallet_balance": snap.get("balance"),
            "credit_summary": summary if status_code < 400 else None,
            "message": "How much credit do you need? Example: “Request ₹20000 wallet credit”. Main Admin must approve.",
            "portal_href": "/wallet/credit-facility",
        }

    purpose_text = (purpose or "").strip()
    if not purpose_text:
        m = re.search(r"(?:for|because|purpose)[:\s]+(.+)$", text or "", flags=re.IGNORECASE)
        purpose_text = (m.group(1).strip() if m else "") or "Requested via Research Copilot"

    payload = {
        "requested_amount": str(amt),
        "purpose": purpose_text[:500],
        "department_id": _default_department_id(user, snap),
        "wallet_balance_before": snap.get("balance"),
        "outstanding_credit": outstanding,
        "eligibility": eligibility,
        "source": "COPILOT",
    }
    record = prop_store.create_proposal(user=user, action="WALLET_CREDIT", payload=payload)
    executable = _flag("COPILOT_WALLET_CREDIT")
    _audit(user=user, action="prepare_wallet_credit", detail={"proposal_id": record["proposal_id"], "amount": str(amt), "ok": True})
    return {
        "ok": True,
        "action": "WALLET_CREDIT",
        "status": "READY_FOR_CONFIRMATION",
        "proposal_id": record["proposal_id"],
        "confirmation_token": record["confirmation_token"],
        "confirmation_required": True,
        "requires_confirmation": True,
        "executable": executable,
        "expires_at": record["expires_at"],
        "requested_amount": str(amt),
        "purpose": purpose_text,
        "wallet_balance": snap.get("balance"),
        "outstanding_credit": outstanding,
        "message": (
            f"You are requesting ₹{amt} wallet credit. Current wallet ≈ ₹{snap.get('balance')}. "
            f"Outstanding credit: {outstanding if outstanding is not None else 'see portal'}. "
            "Confirm only to submit a PENDING request. Copilot cannot approve credit."
            + ("" if executable else " Execute is currently disabled (flag OFF).")
        ),
        "portal_href": "/wallet/credit-facility",
    }


def execute_wallet_credit_request(
    *,
    user,
    proposal_id: str,
    confirmation_token: str,
    idempotency_key: str = "",
) -> dict[str, Any]:
    if not _flag("COPILOT_WALLET_CREDIT"):
        return _safe_error(
            "COPILOT_WALLET_CREDIT_DISABLED",
            "Wallet credit requests via Copilot are disabled. Use Credit Facility page, or enable COPILOT_WALLET_CREDIT after E2E.",
            proposal_id=proposal_id,
        )

    key = idempotency_key or idem.make_idempotency_key(user=user, action="WALLET_CREDIT", proposal_id=proposal_id)
    cached = idem.get_cached_result(user=user, idempotency_key=key)
    if cached:
        return {**cached, "idempotent_replay": True}

    prop, err = prop_store.validate_proposal_for_user(
        user=user, proposal_id=proposal_id, confirmation_token=confirmation_token, expected_action="WALLET_CREDIT"
    )
    if err:
        return _safe_error(err, "Invalid or expired credit confirmation.")

    payload = prop.get("payload") or {}
    body = {
        "requested_amount": payload.get("requested_amount"),
        "purpose": payload.get("purpose") or "Requested via Research Copilot",
        "department_id": payload.get("department_id"),
    }
    status_code, data = domain_bridge.call_wallet_credit_create(user=user, body=body)
    ok = 200 <= status_code < 300
    result = {
        "ok": ok,
        "action": "WALLET_CREDIT",
        "status_code": status_code,
        "proposal_id": proposal_id,
        "idempotency_key": key,
        "data": data if ok else None,
        "error": None if ok else (data.get("code") or data.get("error") or "CREDIT_REQUEST_FAILED"),
        "message": (
            "Credit request submitted and is PENDING Main Administrator approval."
            if ok
            else (data.get("error") or data.get("message") or "Could not submit credit request.")
        ),
        "portal_href": "/wallet/credit-facility",
        "source": "COPILOT",
    }
    if ok:
        prop_store.invalidate_proposal(proposal_id)
        idem.store_result(user=user, idempotency_key=key, result=result)
    _audit(
        user=user,
        action="execute_wallet_credit",
        detail={
            "ok": ok,
            "proposal_id": proposal_id,
            "idempotency_key": key,
            "requested_amount": body.get("requested_amount"),
            "facility_id": (data or {}).get("id") or (data or {}).get("facility_id"),
            "error": result.get("error"),
        },
        message="wallet_credit_request",
    )
    return result


# Back-compat aliases used by older scaffold imports
def execute_wallet_credit(*, user, proposal_id: str, confirmation_token: str, idempotency_key: str = ""):
    return execute_wallet_credit_request(
        user=user, proposal_id=proposal_id, confirmation_token=confirmation_token, idempotency_key=idempotency_key
    )
