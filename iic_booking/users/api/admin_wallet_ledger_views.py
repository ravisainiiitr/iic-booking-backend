"""Wallet ledger API (Main Administrator only): owners, transactions, manual credit / debit."""

from __future__ import annotations

from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from iic_booking.users import admin_wallet_ledger as svc
from iic_booking.users.mobile_sessions import client_ip


class IsMainAdmin(permissions.BasePermission):
    message = "Only the Main Administrator can view and adjust the wallet ledger."

    def has_permission(self, request, view):
        return svc.is_main_admin(request.user)


def _error(exc: svc.LedgerError) -> Response:
    return Response({"error": exc.message, "code": exc.code, **exc.extra}, status=exc.status)


@api_view(["GET"])
@permission_classes([IsMainAdmin])
def wallet_ledger_owners(request):
    data = svc.list_owners(request.query_params)
    if request.query_params.get("with_options"):
        data["options"] = svc.filter_options()
    return Response(data)


@api_view(["GET"])
@permission_classes([IsMainAdmin])
def wallet_ledger_options(request):
    return Response(svc.filter_options())


@api_view(["GET"])
@permission_classes([IsMainAdmin])
def wallet_ledger_owner_detail(request, owner_id: int):
    try:
        return Response(svc.owner_detail(owner_id))
    except svc.LedgerError as exc:
        return _error(exc)


@api_view(["GET"])
@permission_classes([IsMainAdmin])
def wallet_ledger_transactions(request):
    return Response(svc.list_transactions(request.query_params))


@api_view(["GET"])
@permission_classes([IsMainAdmin])
def wallet_ledger_linked_students(request):
    from iic_booking.users.admin_wallet_students import linked_students

    try:
        return Response(linked_students(request.query_params))
    except svc.LedgerError as exc:
        return _error(exc)


@api_view(["POST"])
@permission_classes([IsMainAdmin])
def wallet_ledger_adjustment_preview(request):
    data = request.data if isinstance(request.data, dict) else {}
    try:
        return Response(svc.preview_adjustment(data))
    except svc.LedgerError as exc:
        return _error(exc)


@api_view(["POST"])
@permission_classes([IsMainAdmin])
def wallet_ledger_adjustment_create(request):
    data = request.data if isinstance(request.data, dict) else {}
    try:
        record, created = svc.perform_adjustment(
            actor=request.user,
            data=data,
            ip=client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except svc.LedgerError as exc:
        return _error(exc)
    payload = svc.serialize_adjustment(record)
    payload["replayed"] = not created
    return Response(payload, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
