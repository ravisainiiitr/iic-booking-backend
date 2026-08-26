"""
Bridge Copilot mutations to existing portal booking APIs (no engine rewrite).

Calls DRF view implementations with an authenticated synthetic request so
authorization, pricing, wallet debit, cancel, and reschedule stay in domain code.
"""

from __future__ import annotations

from typing import Any

from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory, force_authenticate


def _as_data(response) -> tuple[int, dict[str, Any]]:
    status_code = int(getattr(response, "status_code", 500) or 500)
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return status_code, data
    return status_code, {"error": str(data) if data is not None else "unknown_error"}


def _django_json_post(*, user, path: str, body: dict[str, Any]):
    """Authenticated Django HttpRequest (for @api_view entrypoints)."""
    factory = APIRequestFactory()
    request = factory.post(path, body, format="json")
    force_authenticate(request, user=user)
    return request


def _drf_json_post(*, user, path: str, body: dict[str, Any]) -> Request:
    """
    Authenticated DRF Request with JSONParser.

    Used for internal helpers like `_book_equipment_impl` that expect `.data`.
    """
    django_request = _django_json_post(user=user, path=path, body=body)
    drf_request = Request(django_request, parsers=[JSONParser()])
    drf_request.user = user
    return drf_request


def call_book_equipment(*, user, equipment_id: int, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST /api/equipments/<pk>/book/ equivalent via _book_equipment_impl."""
    from iic_booking.equipment.api_views import _book_equipment_impl

    # Never accept foreign user_id from Copilot / LLM.
    safe_body = dict(body or {})
    for banned in ("user_id", "user", "email", "owner", "owner_id", "target_user", "wallet_owner_id"):
        safe_body.pop(banned, None)

    drf_request = _drf_json_post(
        user=user,
        path=f"/api/equipments/{equipment_id}/book/",
        body=safe_body,
    )
    response = _book_equipment_impl(drf_request, equipment_id)
    return _as_data(response)


def call_user_cancel_booking(*, user, booking_id: int, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    from iic_booking.equipment.api_views import user_cancel_booking

    # @api_view must receive Django HttpRequest (not an already-wrapped DRF Request).
    django_request = _django_json_post(
        user=user,
        path=f"/api/bookings/{booking_id}/user-cancel/",
        body=body or {"refund": True, "notes": "Cancelled via Research Copilot"},
    )
    response = user_cancel_booking(django_request, booking_id)
    return _as_data(response)


def call_user_reschedule_booking(
    *, user, booking_id: int, start_time: str, end_time: str
) -> tuple[int, dict[str, Any]]:
    from iic_booking.equipment.api_views import user_reschedule_booking

    django_request = _django_json_post(
        user=user,
        path=f"/api/bookings/{booking_id}/user-reschedule/",
        body={"start_time": start_time, "end_time": end_time},
    )
    response = user_reschedule_booking(django_request, booking_id)
    return _as_data(response)


def _django_get(*, user, path: str):
    factory = APIRequestFactory()
    request = factory.get(path)
    force_authenticate(request, user=user)
    return request


def _strip_identity(body: dict[str, Any] | None) -> dict[str, Any]:
    safe = dict(body or {})
    for banned in ("user_id", "user", "email", "owner", "owner_id", "target_user", "wallet_owner_id", "faculty_id"):
        safe.pop(banned, None)
    return safe


def call_get_wallet(*, user) -> tuple[int, dict[str, Any]]:
    from iic_booking.users.api.wallet_views import get_wallet

    response = get_wallet(_django_get(user=user, path="/api/wallet/"))
    return _as_data(response)


def call_get_wallet_transactions(*, user) -> tuple[int, dict[str, Any]]:
    from iic_booking.users.api.wallet_views import get_wallet_transactions

    response = get_wallet_transactions(_django_get(user=user, path="/api/wallet/transactions/"))
    return _as_data(response)


def call_wallet_credit_summary(*, user) -> tuple[int, dict[str, Any]]:
    from iic_booking.users.api.wallet_credit_facility_v2_views import wallet_credit_v2_summary

    response = wallet_credit_v2_summary(_django_get(user=user, path="/api/wallet/credit-requests/summary/"))
    return _as_data(response)


def call_wallet_credit_create(*, user, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from iic_booking.users.api.wallet_credit_facility_v2_views import wallet_credit_v2_list_or_create

    django_request = _django_json_post(
        user=user,
        path="/api/wallet/credit-requests/",
        body=_strip_identity(body),
    )
    response = wallet_credit_v2_list_or_create(django_request)
    return _as_data(response)


def call_razorpay_wallet_recharge_create_order(*, user, amount: str, department_id: int | None = None) -> tuple[int, dict[str, Any]]:
    """Initiate online wallet recharge via existing payments module (does not settle payment)."""
    from iic_booking.payments.views import razorpay_create_order

    body: dict[str, Any] = {"purpose": "WALLET_RECHARGE", "amount": str(amount)}
    if department_id is not None:
        body["department_id"] = int(department_id)
    django_request = _django_json_post(
        user=user,
        path="/api/payments/razorpay/create-order/",
        body=_strip_identity(body),
    )
    response = razorpay_create_order(django_request)
    return _as_data(response)
