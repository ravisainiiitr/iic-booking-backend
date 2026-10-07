"""Booking endpoint to replace the STL / DXF files of a booked 3D print or laser cutting booking."""

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .fabrication import fabrication_parts_summary, is_fabrication_equipment, own_material_available
from .fabrication_reupload import (
    CHANGE_ID_KEY,
    STATE_KEY,
    ReuploadError,
    fabrication_state_snapshot,
    replace_booking_files,
)
from .models import Booking, BookingEventType, BookingStatus, EquipmentOperator, EquipmentProfileType

logger = logging.getLogger(__name__)


def user_can_replace_fabrication_files(user, booking) -> bool:
    from .api_views import _is_admin_user, _user_can_act_as_oic_for_equipment

    if not user or not getattr(user, "is_authenticated", False):
        return False
    if booking.user_id == user.id:
        return True
    if _is_admin_user(user) or _user_can_act_as_oic_for_equipment(user, booking.equipment):
        return True
    return EquipmentOperator.objects.filter(equipment_id=booking.equipment_id, operator=user).exists()


USER_CHANGE_AFTER_BOOKING_MESSAGE = (
    "Files cannot be changed after booking. If the lab finds a problem with your files, "
    "you will be asked to upload new ones."
)


def _staff_block_reason(booking, now) -> str | None:
    from .quota_utils import booking_first_slot_start

    first_start = booking_first_slot_start(booking)
    if first_start is None or now >= first_start:
        return "Files can only be replaced before the booked slot starts."
    return None


def _owner_block_reason(booking, now) -> str | None:
    """Booking users may replace files only while the lab's rejection is open (slot time does not matter)."""
    from .fabrication_workflow import is_rejection_active

    if not is_rejection_active(booking):
        return USER_CHANGE_AFTER_BOOKING_MESSAGE
    deadline = booking.fabrication_replace_deadline
    if deadline is not None and now >= deadline:
        return "The time to replace the files has ended."
    return None


def fabrication_reupload_block_reason(booking, now=None, user=None) -> str | None:
    """Why ``user`` cannot replace the files right now (None when allowed).

    Lab staff (admin, OIC, Lab Operator): while Booked, before the slot starts.
    Booking user: only while the booking is rejected as not feasible and before the replace deadline.
    Without a user, the lab staff rule is reported.
    """
    from .fabrication_workflow import user_is_fabrication_lab_staff

    if not is_fabrication_equipment(booking.equipment):
        return "Files can only be replaced on 3D print or laser cutting bookings."
    if booking.status != BookingStatus.BOOKED:
        return "Files can only be replaced while the booking is Booked."
    if booking.source_booking_id is not None:
        return "Files cannot be replaced on a repeat booking."
    now = now or timezone.now()
    if user is None:
        return _staff_block_reason(booking, now)
    reasons = []
    if user_is_fabrication_lab_staff(user, booking):
        reason = _staff_block_reason(booking, now)
        if reason is None:
            return None
        reasons.append(reason)
    if booking.user_id == getattr(user, "pk", None):
        reason = _owner_block_reason(booking, now)
        if reason is None:
            return None
        reasons.append(reason)
    return reasons[0] if reasons else "You don't have permission to change the files of this booking."


def _change_rows(booking):
    rows = []
    for change in booking.fabrication_file_changes.select_related("changed_by").all()[:50]:
        by = change.changed_by
        rows.append(
            {
                "id": change.pk,
                "changed_at": change.changed_at.isoformat() if change.changed_at else None,
                "changed_by_name": (getattr(by, "name", "") or getattr(by, "email", "")) if by else "",
                "previous_files": change.previous_files,
                "new_files": change.new_files,
                "previous_own_material": change.previous_own_material,
                "new_own_material": change.new_own_material,
                "charge_before": str(change.charge_before) if change.charge_before is not None else None,
                "charge_after": str(change.charge_after) if change.charge_after is not None else None,
                "reverted_at": change.reverted_at.isoformat() if change.reverted_at else None,
            }
        )
    return rows


def _state_payload(request, booking):
    block = fabrication_reupload_block_reason(booking, user=request.user)
    can_act = user_can_replace_fabrication_files(request.user, booking)
    return {
        "profile_type": booking.equipment.profile_type,
        "can_replace": can_act and block is None,
        "blocked_reason": block if can_act else "You don't have permission to change the files of this booking.",
        "own_material": bool(booking.own_material),
        "own_material_available": own_material_available(booking.equipment),
        "own_material_fixed_charge": (
            str(booking.equipment.own_material_fixed_charge)
            if booking.equipment.own_material_fixed_charge is not None
            else None
        ),
        "parts": fabrication_parts_summary(booking),
        "changes": _change_rows(booking),
    }


def _new_print_minutes(booking):
    from .fabrication import active_print_analyses_for_booking, booking_job_quantity, inject_print_parts

    analyses = active_print_analyses_for_booking(booking)
    return int(inject_print_parts({}, analyses, booking_job_quantity(booking)).get("C") or 0)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def booking_fabrication_files(request, booking_id):
    """GET: current parts and change history. POST: replace files / edit parts / own-material choice.

    POST body (all optional, at least one change required):
      { "print_analysis_id" | "print_analysis_batch_id" | "laser_cut_batch_id": "<uuid>",
        "part_updates": [{"analysis_id": "<uuid>", "part_name": "...", "quantity": 2,
                          "material_id": 3, "units": "mm"}],
        "own_material": true }
    """
    from .api_views import (
        BookingSerializer,
        _is_admin_user,
        _recalculate_booking_charge_and_adjust_wallet,
        _user_can_act_as_oic_for_equipment,
        check_operator_permission,
    )
    from .booking_events import create_booking_event
    from .fabrication_workflow import after_files_replaced
    from .input_edit_payment_window import (
        expire_unpaid_input_edit,
        has_payment_window,
        snapshot_booking_charge_state,
    )
    from .input_edit_refund_window import instant_refund_window
    from .print_3d_notifications import REASON_FILES_UPDATED, dispatch_fabrication_file_email

    booking = (
        Booking.objects.select_related("equipment", "charge_profile", "user")
        .filter(booking_id=booking_id)
        .first()
    )
    if booking is None:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    can_view = user_can_replace_fabrication_files(request.user, booking) or check_operator_permission(request.user)
    if not can_view:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response(_state_payload(request, booking))

    if not user_can_replace_fabrication_files(request.user, booking):
        return Response(
            {"error": "You don't have permission to change the files of this booking."},
            status=status.HTTP_403_FORBIDDEN,
        )

    expire_unpaid_input_edit(booking)
    block = fabrication_reupload_block_reason(booking, user=request.user)
    if block:
        return Response({"error": block}, status=status.HTTP_400_BAD_REQUEST)
    if has_payment_window(booking):
        return Response(
            {"error": "Pay or cancel the pending extra charge on this booking before changing the files."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    data = request.data or {}
    is_charge_manager = _user_can_act_as_oic_for_equipment(request.user, booking.equipment) or _is_admin_user(
        request.user
    )
    acting_as_booking_user = request.user.pk == booking.user_id and not is_charge_manager
    is_staff_editor = check_operator_permission(request.user)

    try:
        with transaction.atomic():
            booking = Booking.objects.select_for_update().select_related("equipment", "charge_profile", "user").get(
                pk=booking.pk
            )
            locked_block = fabrication_reupload_block_reason(booking, user=request.user)
            if locked_block:
                raise ReuploadError(locked_block)
            payment_window_snapshot = snapshot_booking_charge_state(booking) if acting_as_booking_user else None
            if payment_window_snapshot is not None:
                payment_window_snapshot[STATE_KEY] = fabrication_state_snapshot(booking)
            instant_refund_allowed = acting_as_booking_user and instant_refund_window(booking)[0]

            change = replace_booking_files(
                booking,
                request.user,
                print_analysis_id=data.get("print_analysis_id") or None,
                print_analysis_batch_id=data.get("print_analysis_batch_id") or None,
                laser_cut_batch_id=data.get("laser_cut_batch_id") or None,
                part_updates=data.get("part_updates"),
                own_material=data.get("own_material") if "own_material" in data else None,
            )
            if payment_window_snapshot is not None:
                payment_window_snapshot[CHANGE_ID_KEY] = [change.pk]

            if booking.equipment.profile_type == EquipmentProfileType.PRINT_3D and not is_staff_editor:
                from .quota_breakdown import quota_failure_fields
                from .quota_utils import booking_quota_reference_datetime, evaluate_booking_minutes_change

                decision = evaluate_booking_minutes_change(booking, _new_print_minutes(booking))
                if not decision.allowed:
                    raise _QuotaRejected(
                        {
                            "error": decision.error,
                            **quota_failure_fields(
                                decision,
                                equipment=booking.equipment,
                                subject=booking.user,
                                booking_date=booking_quota_reference_datetime(booking),
                                booking_id=booking.pk,
                            ),
                        }
                    )

            summary = _recalculate_booking_charge_and_adjust_wallet(
                request,
                booking,
                payment_window_snapshot=payment_window_snapshot,
                instant_refund_allowed=instant_refund_allowed,
            )
            booking.refresh_from_db()
            change.charge_after = booking.total_charge
            change.save(update_fields=["charge_after"])

            what = "Design files replaced" if change.files_replaced else "Fabrication details updated"
            create_booking_event(
                booking=booking,
                event_type=BookingEventType.COMMENT,
                created_by=request.user,
                comment=(
                    f"{what}: {len(change.new_files)} part(s)"
                    + ("; user brings own material" if booking.own_material else "")
                    + f". Charge ₹{change.charge_before} → ₹{change.charge_after}."
                ),
                metadata={
                    "fabrication_file_change_id": change.pk,
                    "previous_files": change.previous_files,
                    "new_files": change.new_files,
                },
                send_notification=False,
            )
            if not after_files_replaced(booking, request.user):
                dispatch_fabrication_file_email(booking, reason=REASON_FILES_UPDATED)
    except ReuploadError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except _QuotaRejected as exc:
        return Response(exc.payload, status=status.HTTP_400_BAD_REQUEST)

    message = "Files updated. Charges recalculated."
    if summary.get("extra_amount"):
        message = f"Files updated. The new charge is higher: pay ₹{summary['extra_amount']} to keep the change."
    elif summary.get("refund_status") == "refunded":
        message = f"Files updated. The new charge is lower, so ₹{summary['refund_amount']} has been refunded to the wallet."
    elif summary.get("refund_status") == "awaiting_oic_confirmation":
        message = (
            f"Files updated. The refund of ₹{summary['refund_amount']} will reach the wallet after the "
            "Officer In Charge approves it."
        )
    return Response(
        {
            "message": message,
            "booking": BookingSerializer(booking, context={"request": request}).data,
            "charge_recalculation_summary": summary,
            "fabrication": _state_payload(request, booking),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_fabrication_reject(request, booking_id):
    """Lab staff: reject a booked 3D print / laser cutting job as not feasible. Body: {"reason": "..."}"""
    from .api_views import BookingSerializer
    from .fabrication_workflow import FabricationWorkflowError, reject_fabrication_booking

    try:
        booking = reject_fabrication_booking(booking_id, request.user, (request.data or {}).get("reason"))
    except FabricationWorkflowError as exc:
        return Response({"error": str(exc)}, status=exc.status_code)
    booking = Booking.objects.select_related("equipment", "charge_profile", "user").get(pk=booking.pk)
    return Response(
        {
            "message": "Booking rejected. The user has been emailed and can upload new files until "
            f"{_deadline_display(booking)}.",
            "booking": BookingSerializer(booking, context={"request": request}).data,
        },
        status=status.HTTP_200_OK,
    )


def _deadline_display(booking) -> str:
    from iic_booking.communication.email_branding import format_email_datetime

    return format_email_datetime(booking.fabrication_replace_deadline)


class _QuotaRejected(Exception):
    def __init__(self, payload):
        super().__init__(payload.get("error"))
        self.payload = payload
