"""
Bridge Copilot mutations to existing portal booking APIs (no engine rewrite).

Calls DRF view implementations with an authenticated synthetic request so
authorization, pricing, wallet debit, cancel, and reschedule stay in domain code.
"""

from __future__ import annotations

from typing import Any

from rest_framework.test import APIRequestFactory, force_authenticate


def _as_data(response) -> tuple[int, dict[str, Any]]:
    status_code = int(getattr(response, "status_code", 500) or 500)
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return status_code, data
    return status_code, {"error": str(data) if data is not None else "unknown_error"}


def call_book_equipment(*, user, equipment_id: int, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST /api/equipments/<pk>/book/ equivalent via _book_equipment_impl."""
    from iic_booking.equipment.api_views import _book_equipment_impl

    # Never accept foreign user_id from Copilot / LLM.
    safe_body = dict(body or {})
    for banned in ("user_id", "user", "email", "owner", "owner_id", "target_user", "wallet_owner_id"):
        safe_body.pop(banned, None)

    factory = APIRequestFactory()
    request = factory.post(f"/api/equipments/{equipment_id}/book/", safe_body, format="json")
    force_authenticate(request, user=user)
    # DRF views expect .data / .user on Request wrapper
    from rest_framework.request import Request

    drf_request = Request(request)
    drf_request.user = user
    response = _book_equipment_impl(drf_request, equipment_id)
    return _as_data(response)


def call_user_cancel_booking(*, user, booking_id: int, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    from iic_booking.equipment.api_views import user_cancel_booking

    factory = APIRequestFactory()
    request = factory.post(
        f"/api/bookings/{booking_id}/user-cancel/",
        body or {"refund": True, "notes": "Cancelled via Research Copilot"},
        format="json",
    )
    force_authenticate(request, user=user)
    from rest_framework.request import Request

    drf_request = Request(request)
    drf_request.user = user
    response = user_cancel_booking(drf_request, booking_id)
    return _as_data(response)


def call_user_reschedule_booking(
    *, user, booking_id: int, start_time: str, end_time: str
) -> tuple[int, dict[str, Any]]:
    from iic_booking.equipment.api_views import user_reschedule_booking

    factory = APIRequestFactory()
    request = factory.post(
        f"/api/bookings/{booking_id}/user-reschedule/",
        {"start_time": start_time, "end_time": end_time},
        format="json",
    )
    force_authenticate(request, user=user)
    from rest_framework.request import Request

    drf_request = Request(request)
    drf_request.user = user
    response = user_reschedule_booking(drf_request, booking_id)
    return _as_data(response)
