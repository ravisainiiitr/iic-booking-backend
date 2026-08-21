"""Student expiry and +6 calendar month extension workflow."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from iic_booking.users.identity.dates import add_calendar_months
from iic_booking.users.identity.flags import student_lifecycle_enabled
from iic_booking.users.identity.service import UserEligibilityService, UserIdentityService
from iic_booking.users.models.channel_i_identity import (
    StudentDisableReason,
    StudentLifecycleEvent,
    StudentValidityExtension,
    StudentValiditySource,
)
from iic_booking.users.models.user_type import UserType

STUDENT_TYPES = {UserType.STUDENT, UserType.INDIVIDUAL_STUDENT}


class LifecycleError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


def expire_due_students(*, actor=None) -> int:
    """Idempotently disable students whose effective end date has passed. Feature-flagged."""
    if not student_lifecycle_enabled():
        return 0
    from iic_booking.users.models import User

    today = timezone.localdate()
    count = 0
    qs = User.objects.filter(user_type__in=STUDENT_TYPES, force_inactive=False)
    for user in qs.select_related("channel_i_identity"):
        view = UserIdentityService.view(user)
        if view.validity.unresolved:
            continue
        if view.validity.effective_end_date and today > view.validity.effective_end_date:
            _disable_student(
                user,
                reason=StudentDisableReason.CHANNEL_I_STUDENT_END_DATE,
                actor=actor,
                note=str(view.validity.effective_end_date),
            )
            count += 1
    return count


@transaction.atomic
def _disable_student(user, *, reason: str, actor=None, note: str = "") -> None:
    user = type(user).objects.select_for_update().get(pk=user.pk)
    if user.force_inactive:
        return
    prev_active = user.is_active
    user.force_inactive = True
    user.is_active = False
    user.save(update_fields=["force_inactive", "is_active"])
    StudentLifecycleEvent.objects.create(
        user=user,
        action="DISABLED",
        reason=reason,
        previous_value=str(prev_active),
        new_value="False",
        actor=actor,
    )


@transaction.atomic
def request_six_month_extension(*, student, faculty, reason: str) -> StudentValidityExtension:
    if not student_lifecycle_enabled():
        raise LifecycleError("FEATURE_DISABLED", "Student lifecycle is not enabled.", status=403)
    if not UserEligibilityService.is_wallet_owner_of(faculty, student):
        raise LifecycleError(
            "FORBIDDEN",
            "Only the student's actual faculty/wallet owner may request an extension.",
            status=403,
        )
    profile = UserIdentityService.get_profile(student)
    if not profile:
        raise LifecycleError("NO_PROFILE", "No Channel-I identity profile is stored for this student.")
    if profile.student_end_date:
        raise LifecycleError(
            "CHANNEL_I_END_DATE_AUTHORITATIVE",
            "A Channel-I end date is present. Local extensions cannot override institutional end dates.",
        )
    validity = UserIdentityService.compute_validity(profile)
    if validity.unresolved or not validity.effective_end_date:
        raise LifecycleError("STUDENT_VALIDITY_UNRESOLVED", "Student validity is unresolved.")
    if validity.validity_source == StudentValiditySource.CHANNEL_I_END_DATE:
        raise LifecycleError(
            "CHANNEL_I_END_DATE_AUTHORITATIVE",
            "A Channel-I end date is present. Local extensions cannot override institutional end dates.",
        )
    pending = StudentValidityExtension.objects.filter(student=student, status="SUBMITTED").exists()
    if pending:
        raise LifecycleError("PENDING_EXTENSION", "An extension request is already pending.")
    requested = add_calendar_months(validity.effective_end_date, 6)
    return StudentValidityExtension.objects.create(
        student=student,
        previous_expiry=validity.effective_end_date,
        requested_expiry=requested,
        extension_months=6,
        requested_by=faculty,
        reason=(reason or "").strip() or "Six-month extension",
        status="SUBMITTED",
    )


@transaction.atomic
def approve_extension(*, extension: StudentValidityExtension, admin, reason: str = "") -> StudentValidityExtension:
    from iic_booking.users.models.user_type import UserType as UT

    if getattr(admin, "user_type", None) != UT.ADMIN:
        raise LifecycleError("FORBIDDEN", "Only Main Administrator can approve extensions.", status=403)
    extension = StudentValidityExtension.objects.select_for_update().get(pk=extension.pk)
    if extension.status != "SUBMITTED":
        raise LifecycleError("INVALID_STATE", f"Cannot approve extension in status {extension.status}.")
    profile = UserIdentityService.get_profile(extension.student)
    if profile and profile.student_end_date:
        raise LifecycleError(
            "CHANNEL_I_END_DATE_AUTHORITATIVE",
            "A Channel-I end date is now present. Local extensions cannot override it.",
        )
    extension.status = "APPROVED"
    extension.approved_by = admin
    extension.approved_at = timezone.now()
    extension.approved_expiry = extension.requested_expiry
    if reason:
        extension.reason = f"{extension.reason}\nApproval: {reason}".strip()
    extension.save()
    if profile:
        profile.derived_end_date = extension.approved_expiry
        profile.validity_source = StudentValiditySource.ADMIN_EXTENSION
        profile.save(update_fields=["derived_end_date", "validity_source", "updated_at"])
    StudentLifecycleEvent.objects.create(
        user=extension.student,
        action="EXTENSION_APPROVED",
        reason="ADMIN_EXTENSION",
        previous_value=str(extension.previous_expiry),
        new_value=str(extension.approved_expiry),
        actor=admin,
    )
    return extension
