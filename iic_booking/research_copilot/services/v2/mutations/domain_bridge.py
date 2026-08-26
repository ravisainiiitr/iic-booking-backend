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


def _authenticated_json_request(*, user, path: str, body: dict[str, Any]) -> Request:
    factory = APIRequestFactory()
    django_request = factory.post(path, body, format="json")
    force_authenticate(django_request, user=user)
    # Explicit parsers required: bare Request() rejects application/json.
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

    drf_request = _authenticated_json_request(
        user=user,
        path=f"/api/equipments/{equipment_id}/book/",
        body=safe_body,
    )
    response = _book_equipment_impl(drf_request, equipment_id)
    return _as_data(response)


def call_user_cancel_booking(*, user, booking_id: int, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    from iic_booking.equipment.api_views import user_cancel_booking

    drf_request = _authenticated_json_request(
        user=user,
        path=f"/api/bookings/{booking_id}/user-cancel/",
        body=body or {"refund": True, "notes": "Cancelled via Research Copilot"},
    )
    response = user_cancel_booking(drf_request, booking_id)
    return _as_data(response)


def call_user_reschedule_booking(
    *, user, booking_id: int, start_time: str, end_time: str
) -> tuple[int, dict[str, Any]]:
    from iic_booking.equipment.api_views import user_reschedule_booking

    drf_request = _authenticated_json_request(
        user=user,
        path=f"/api/bookings/{booking_id}/user-reschedule/",
        body={"start_time": start_time, "end_time": end_time},
    )
    response = user_reschedule_booking(drf_request, booking_id)
    return _as_data(response)
