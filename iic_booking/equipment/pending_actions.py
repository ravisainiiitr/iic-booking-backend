"""Items waiting for the signed-in user's attention (login popup, dashboard count)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

logger = logging.getLogger(__name__)

MAX_DETAILS = 3
RECENT_RESULTS_DAYS = 30


def _scoped(qs, field: str, equipment_ids):
    if equipment_ids is None:
        return qs
    if not equipment_ids:
        return qs.none()
    return qs.filter(**{f"{field}__in": equipment_ids})


def _booking_ref(booking) -> str:
    from iic_booking.communication.utils import booking_display_id_for_email

    return booking_display_id_for_email(booking) or str(booking.booking_id)


def _booking_line(booking) -> str:
    return f"{_booking_ref(booking)} — {booking.equipment.name}"


def _person(user) -> str:
    from iic_booking.communication.in_app import person_label

    return person_label(user)


class _Collector:
    def __init__(self, user):
        self.user = user
        self.items: list[dict[str, Any]] = []

    def add(self, key: str, label: str, qs_or_count, link: str, description: str, detail: Callable | None = None):
        count = qs_or_count if isinstance(qs_or_count, int) else qs_or_count.count()
        if not count:
            return
        item: dict[str, Any] = {"key": key, "label": label, "count": count, "link": link, "description": description}
        if detail is not None and not isinstance(qs_or_count, int):
            item["details"] = [detail(obj) for obj in qs_or_count[:MAX_DETAILS]]
        self.items.append(item)

    def safely(self, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            logger.exception("pending actions: %s failed user_id=%s", name, self.user.id)


def _personal_items(c: _Collector) -> None:
    from .models import (
        Booking,
        BookingDataShare,
        BookingStatus,
        IstemFbrStatus,
        StudentEquipmentNomination,
        StudentEquipmentNominationStatus,
        TAAssignment,
        TAAssignmentStatus,
    )

    user = c.user
    my_bookings = Booking.objects.filter(user=user).select_related("equipment")

    def wallet_join():
        from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

        c.add(
            "wallet_join_requests",
            "Wallet join requests",
            WalletJoinRequest.objects.filter(faculty=user, status=WalletJoinRequestStatus.PENDING)
            .select_related("student")
            .order_by("-pk"),
            "/student-management",
            "Students have asked to join your wallet. Approve or reject each request.",
            lambda r: f"{_person(r.student)} ({r.student.email}) wants to join your wallet",
        )

    def credit_clarification():
        from iic_booking.users.models.wallet_credit_facility import WalletCreditFacility, WalletCreditFacilityStatus

        c.add(
            "wallet_credit_clarification",
            "Wallet credit: clarification requested",
            WalletCreditFacility.objects.filter(user=user, status=WalletCreditFacilityStatus.CLARIFICATION).order_by("-pk"),
            "/wallet/credit-facility",
            "The administrator returned your wallet credit request for clarification.",
            lambda f: f"{f.public_reference} — ₹{f.requested_amount}",
        )

    def nominations():
        c.add(
            "nomination_resume",
            "Equipment nominations: upload resume",
            StudentEquipmentNomination.objects.filter(
                student=user, status=StudentEquipmentNominationStatus.PENDING, resume_submitted_at__isnull=True
            )
            .select_related("equipment", "supervisor")
            .order_by("-nominated_at"),
            "/my-nomination-requests",
            "Your supervisor nominated you to operate equipment. Upload your resume so it can be reviewed.",
            lambda n: f"{n.equipment.name} — nominated by {_person(n.supervisor)}",
        )

    def ta_duties():
        c.add(
            "ta_assignments",
            "TA duties to accept",
            TAAssignment.objects.filter(ta_student=user, status=TAAssignmentStatus.ALLOCATED)
            .select_related("booking__equipment")
            .order_by("-pk"),
            "/ta-assignments",
            "You have been allocated TA duty. Accept or decline each allocation.",
            lambda a: _booking_line(a.booking),
        )

    def payments():
        c.add(
            "bookings_pending_payment",
            "Bookings awaiting payment",
            my_bookings.filter(status=BookingStatus.PENDING_PAYMENT).order_by("-created_at"),
            "/my-bookings",
            "Complete payment to confirm these bookings.",
            _booking_line,
        )
        c.add(
            "bookings_disruption",
            "Disrupted bookings: your choice needed",
            my_bookings.filter(status=BookingStatus.DISRUPTION_PENDING).order_by("-created_at"),
            "/my-bookings",
            "These bookings were disrupted. Choose to reschedule or cancel.",
            _booking_line,
        )
        c.add(
            "istem_fbr_pending",
            "I-STEM FBR required",
            my_bookings.filter(
                status__in=(BookingStatus.BOOKED, BookingStatus.COMPLETED),
                istem_fbr_status__in=(IstemFbrStatus.PENDING_FBR, IstemFbrStatus.INVALID),
            ).order_by("-created_at"),
            "/my-bookings",
            "Submit (or correct) your I-STEM FBR number for these bookings.",
            lambda b: _booking_line(b)
            + (" (rejected — correction required)" if b.istem_fbr_status == IstemFbrStatus.INVALID else ""),
        )
        c.add(
            "ratings_due",
            "Bookings to rate",
            my_bookings.filter(
                status=BookingStatus.COMPLETED, rating__isnull=True, equipment__user_rating_enabled=True
            ).order_by("-completed_at"),
            "/my-bookings?pending_rating=1",
            "Rate your completed bookings to unlock their results.",
            _booking_line,
        )

    def new_results():
        from django.db.models import Q

        from .results_sharing_views import _bookings_with_results

        cutoff = timezone.now() - timedelta(days=RECENT_RESULTS_DAYS)
        qs = (
            _bookings_with_results(my_bookings)
            .filter(Q(completed_at__gte=cutoff) | Q(results_available_notified_at__gte=cutoff))
            .exclude(result_views__user=user)
            .order_by("-completed_at")
        )
        c.add(
            "new_results",
            "New results",
            qs,
            "/my-results",
            f"Results from the last {RECENT_RESULTS_DAYS} days that you have not downloaded yet.",
            _booking_line,
        )

    def shared_data():
        from .results_sharing_service import is_internal_iitr_user

        if not is_internal_iitr_user(user):
            return
        shares = (
            BookingDataShare.objects.filter(shared_with=user, revoked_at__isnull=True)
            .exclude(booking__result_views__user=user)
            .select_related("shared_by", "booking__equipment")
            .order_by("-created_at")
        )
        c.add(
            "shared_data_new",
            "Research data shared with you",
            shares,
            "/shared-data",
            "Colleagues shared booking results with you that you have not opened yet.",
            lambda s: f"{_person(s.shared_by)} shared {_booking_line(s.booking)}",
        )

    def shared_workspaces():
        from iic_booking.communication.models import CommunicationLog
        from iic_booking.my_research.models import ResearchWorkspaceMember
        from iic_booking.my_research.services import WORKSPACE_SHARED_TITLE

        active_ids = [
            str(pk)
            for pk in ResearchWorkspaceMember.objects.filter(user=user, revoked_at__isnull=True)
            .exclude(workspace__owner=user)
            .values_list("workspace_id", flat=True)
        ]
        if not active_ids:
            return
        c.add(
            "workspaces_shared",
            "Research workspaces shared with you",
            CommunicationLog.objects.filter(
                recipient=user,
                communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION,
                subject=WORKSPACE_SHARED_TITLE,
                metadata__research_workspace_id__in=active_ids,
            )
            .exclude(status=CommunicationLog.CommunicationStatus.READ)
            .order_by("-created_at"),
            "/my-research",
            "You were given access to research workspaces you have not opened yet.",
            lambda n: n.message or n.subject,
        )

    for name, fn in (
        ("wallet_join", wallet_join),
        ("credit_clarification", credit_clarification),
        ("nominations", nominations),
        ("ta_duties", ta_duties),
        ("payments", payments),
        ("new_results", new_results),
        ("shared_data", shared_data),
        ("shared_workspaces", shared_workspaces),
    ):
        c.safely(name, fn)


def _staff_items(c: _Collector) -> None:
    from .api_views import _get_equipment_ids_for_log_access, check_operator_permission, get_equipment_ids_managed_by_oic
    from .models import (
        Booking,
        EquipmentPublicationClaim,
        EquipmentPublicationClaimStatus,
        IstemFbrStatus,
        OperatorLeaveRequest,
        RepeatSampleRequest,
        RepeatSampleRequestStatus,
        StudentEquipmentNomination,
        StudentEquipmentNominationStatus,
        TADutyLog,
        TADutyLogStatus,
        UrgentBookingRequest,
        UrgentBookingRequestStatus,
    )

    user = c.user
    user_type = getattr(user, "user_type", None)
    if not check_operator_permission(user):
        return
    equipment_ids = _get_equipment_ids_for_log_access(user)
    oic_ids = None if user_type == UserType.ADMIN else list(get_equipment_ids_managed_by_oic(user.id))

    c.add(
        "repeat_sample_requests",
        "Repeat sample requests",
        _scoped(
            RepeatSampleRequest.objects.filter(status=RepeatSampleRequestStatus.PENDING), "booking__equipment_id", equipment_ids
        ),
        "/repeat-sample-requests",
        "Users have asked for a complimentary repeat sample. Approve or reject each request.",
    )
    if user_type == UserType.OPERATOR:
        return
    c.add(
        "urgent_requests",
        "Urgent booking requests",
        _scoped(UrgentBookingRequest.objects.filter(status=UrgentBookingRequestStatus.PENDING), "equipment_id", equipment_ids),
        "/urgent-requests",
        "Urgent (Type B) booking requests are waiting for your decision.",
    )

    def fbr():
        c.add(
            "istem_fbr_verification",
            "I-STEM FBR verification",
            _scoped(
                Booking.objects.filter(istem_fbr_status=IstemFbrStatus.PENDING_OIC).select_related("equipment", "user"),
                "equipment_id",
                oic_ids,
            ).order_by("-updated_at"),
            "/booking-management",
            "External users submitted I-STEM FBR numbers. Verify them on I-STEM and mark them in Booking Management.",
            lambda b: f"{_booking_line(b)} — FBR {b.istem_fbr_number} by {_person(b.user)}",
        )

    def nominations():
        c.add(
            "nominations_to_review",
            "Equipment operating nominations",
            _scoped(
                StudentEquipmentNomination.objects.filter(
                    status=StudentEquipmentNominationStatus.PENDING, resume_submitted_at__isnull=False
                ).select_related("student", "equipment"),
                "equipment_id",
                oic_ids,
            ).order_by("-resume_submitted_at"),
            "/ta-nominations-log",
            "Nominated students submitted their resumes. Approve or reject each nomination.",
            lambda n: f"{_person(n.student)} — {n.equipment.name}",
        )

    def duty_logs():
        c.add(
            "ta_duty_logs",
            "TA duty logs to verify",
            _scoped(
                TADutyLog.objects.filter(status=TADutyLogStatus.PENDING).select_related("student", "equipment"),
                "equipment_id",
                oic_ids,
            ).order_by("-created_at"),
            "/ta-assignments",
            "TA students logged duty hours that need verification.",
            lambda d: f"{_person(d.student)} — {d.equipment.name} on {d.duty_date}",
        )

    def leave():
        if user_type in (UserType.MANAGER, UserType.ADMIN) and getattr(user, "department_id", None):
            c.add(
                "leave_requests",
                "Lab Incharge leave requests",
                OperatorLeaveRequest.objects.filter(
                    status=OperatorLeaveRequest.Status.PENDING, operator__department_id=user.department_id
                ),
                "/oic-leave-management",
                "Leave requests from Lab Incharges in your department need approval.",
            )

    def publication_claims():
        from .publication_claim_views import _filter_review_queryset, _is_admin, _managed_equipment_ids

        if _is_admin(user) or (user_type == UserType.MANAGER and _managed_equipment_ids(user)):
            c.add(
                "publication_claims",
                "Publication claims",
                _filter_review_queryset(
                    user, EquipmentPublicationClaim.objects.filter(status=EquipmentPublicationClaimStatus.PENDING)
                ),
                "/publication-claims",
                "Publication claims are waiting for your review.",
            )

    for name, fn in (
        ("fbr", fbr),
        ("nominations", nominations),
        ("duty_logs", duty_logs),
        ("leave", leave),
        ("publication_claims", publication_claims),
    ):
        c.safely(name, fn)


def _admin_items(c: _Collector) -> None:
    if getattr(c.user, "user_type", None) != UserType.ADMIN:
        return

    def notices():
        from iic_booking.communication.models import Notice

        c.add(
            "notice_requests",
            "Notice board requests",
            Notice.objects.filter(approval_status=Notice.ApprovalStatus.PENDING),
            "/admin-settings/communication?tab=notices",
            "Notice board requests from Officers in charge need approval.",
        )

    def wallet():
        from iic_booking.users.models.wallet import (
            WalletRechargeRequest,
            WalletRechargeRequestStatus,
            WalletWithdrawalRequest,
            WalletWithdrawalRequestStatus,
        )
        from iic_booking.users.models.wallet_credit_facility import WalletCreditFacility, WalletCreditFacilityStatus

        c.add(
            "wallet_recharge_requests",
            "Wallet recharge requests",
            WalletRechargeRequest.objects.filter(status=WalletRechargeRequestStatus.PENDING, user_otp_verified=True)
            .select_related("user")
            .order_by("-pk"),
            "/admin-settings/wallet-recharge-requests",
            "Verified wallet recharge requests are awaiting approval.",
            lambda r: f"{_person(r.user)} — ₹{r.amount}",
        )
        c.add(
            "wallet_withdrawal_requests",
            "Wallet withdrawal requests",
            WalletWithdrawalRequest.objects.filter(status=WalletWithdrawalRequestStatus.PENDING)
            .select_related("user")
            .order_by("-pk"),
            "/admin-settings/wallet-withdrawal-requests",
            "External users asked to withdraw their wallet balance.",
            lambda r: f"{_person(r.user)} — ₹{r.amount}",
        )
        c.add(
            "wallet_credit_requests",
            "Wallet credit requests",
            WalletCreditFacility.objects.filter(
                status__in=(WalletCreditFacilityStatus.SUBMITTED, WalletCreditFacilityStatus.UNDER_REVIEW)
            )
            .select_related("user")
            .order_by("-pk"),
            "/admin/wallet-credit",
            "Wallet credit requests are waiting for review.",
            lambda f: f"{f.public_reference} — {_person(f.user)} — ₹{f.requested_amount}",
        )

    def equipment_additions():
        from .models import EquipmentAdditionRequest, EquipmentAdditionRequestStatus

        c.add(
            "equipment_addition_requests",
            "Equipment addition requests",
            EquipmentAdditionRequest.objects.filter(status=EquipmentAdditionRequestStatus.PENDING),
            "/admin/equipment-addition-requests",
            "New equipment addition requests are waiting for review.",
        )

    for name, fn in (("notices", notices), ("wallet", wallet), ("equipment_additions", equipment_additions)):
        c.safely(name, fn)


def collect_pending_actions(user) -> list[dict[str, Any]]:
    c = _Collector(user)
    c.safely("staff", lambda: _staff_items(c))
    c.safely("admin", lambda: _admin_items(c))
    c.safely("personal", lambda: _personal_items(c))
    return c.items


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def pending_actions(request):
    items = collect_pending_actions(request.user)
    return Response({"items": items, "total": sum(i["count"] for i in items)}, status=status.HTTP_200_OK)
