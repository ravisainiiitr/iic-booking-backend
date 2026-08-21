"""Central identity classification and feature eligibility. All features must use this."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from django.utils import timezone

from iic_booking.users.identity.dates import add_calendar_years
from iic_booking.users.identity.extract import normalize_label
from iic_booking.users.identity.flags import (
    hod_affiliation_enabled,
    student_lifecycle_enabled,
    wallet_credit_enabled,
)
from iic_booking.users.models.channel_i_identity import (
    ChannelIDepartmentMapping,
    ChannelIIdentityProfile,
    DegreeClassificationKind,
    HeadOfDepartmentAssignment,
    PortalUserClassification,
    StudentDegreeClassification,
    StudentValiditySource,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

STAFF_PORTAL_TYPES = frozenset(
    {
        UserType.ADMIN,
        UserType.DEPT_ADMIN,
        UserType.MANAGER,
        UserType.OPERATOR,
        UserType.FINANCE,
        UserType.EXTERNAL_RELATIONS,
        UserType.ORG_ADMIN,
    }
)
STUDENT_PORTAL_TYPES = frozenset({UserType.STUDENT, UserType.INDIVIDUAL_STUDENT})
CREDIT_ELIGIBLE_CLASSIFICATIONS = frozenset(
    {
        PortalUserClassification.FACULTY,
        PortalUserClassification.HEAD_OF_DEPARTMENT,
        PortalUserClassification.STAFF,
    }
)


@dataclass
class StudentValidity:
    channel_i_end_date: date | None
    derived_end_date: date | None
    effective_end_date: date | None
    validity_source: str
    is_active_student: bool
    unresolved: bool
    reason: str = ""


@dataclass
class IdentityView:
    classification: str
    portal_user_type: str
    is_undergraduate: bool
    is_student: bool
    is_hod: bool
    channel_i_department_name: str
    internal_department_id: int | None
    internal_department_name: str
    department_mapped: bool
    department_status: str
    validity: StudentValidity
    degree_name: str
    degree_classification: str
    channel_i_user_id: str
    channel_i_username: str
    student_start_date: date | None
    extras: dict[str, Any] = field(default_factory=dict)


class UserIdentityService:
    @staticmethod
    def get_profile(user) -> ChannelIIdentityProfile | None:
        pk = getattr(user, "pk", None)
        if not pk:
            return None
        return ChannelIIdentityProfile.objects.filter(user_id=pk).first()

    @staticmethod
    def classify_degree(degree_name: str) -> tuple[str, str]:
        """Return (classification_or_empty, normalized_name). Empty classification = unclassified."""
        key = normalize_label(degree_name)
        if not key:
            return "", ""
        row = StudentDegreeClassification.objects.filter(
            channel_i_degree_name_normalized=key, active=True
        ).first()
        if not row:
            return "", degree_name
        return row.classification, row.normalized_degree_name or row.channel_i_degree_name

    @staticmethod
    def map_department(channel_i_name: str) -> tuple[ChannelIDepartmentMapping | None, str]:
        key = normalize_label(channel_i_name)
        if not key:
            return None, "UNRESOLVED"
        row = ChannelIDepartmentMapping.objects.filter(channel_i_department_name_normalized=key).first()
        if not row or not row.active:
            return row, "UNMAPPED"
        if not row.internal_department_id:
            return row, "UNMAPPED"
        return row, "MAPPED"

    @staticmethod
    def active_hod_for_department(department_id: int) -> HeadOfDepartmentAssignment | None:
        today = timezone.localdate()
        return (
            HeadOfDepartmentAssignment.objects.filter(
                department_id=department_id,
                active=True,
                effective_from__lte=today,
            )
            .filter(models_q_effective_to(today))
            .select_related("user", "department")
            .first()
        )

    @staticmethod
    def compute_validity(profile: ChannelIIdentityProfile | None, *, user=None) -> StudentValidity:
        if profile is None:
            return StudentValidity(None, None, None, StudentValiditySource.UNRESOLVED, False, True, "NO_PROFILE")
        channel_end = profile.student_end_date
        start = profile.student_start_date
        derived = None
        source = StudentValiditySource.UNRESOLVED
        if channel_end:
            source = StudentValiditySource.CHANNEL_I_END_DATE
            effective = channel_end
        elif start:
            derived = add_calendar_years(start, 5)
            # Local extension may lengthen derived date only when Channel-I end is absent.
            if profile.derived_end_date and profile.derived_end_date > derived:
                derived = profile.derived_end_date
                source = StudentValiditySource.ADMIN_EXTENSION
            else:
                source = StudentValiditySource.START_DATE_PLUS_5_YEARS
            effective = derived
        else:
            return StudentValidity(
                None,
                None,
                None,
                StudentValiditySource.UNRESOLVED,
                False,
                True,
                "STUDENT_VALIDITY_UNRESOLVED",
            )
        today = timezone.localdate()
        # Active through end_date; disabled after end_date (portal local date).
        is_active = today <= effective
        return StudentValidity(channel_end, derived, effective, source, is_active, False)

    @classmethod
    def classify_user(cls, user) -> str:
        hod = (
            HeadOfDepartmentAssignment.objects.filter(user=user, active=True)
            .filter(models_q_effective_to(timezone.localdate()))
            .exists()
        )
        if hod:
            return PortalUserClassification.HEAD_OF_DEPARTMENT
        portal = (getattr(user, "user_type", None) or "").strip()
        if not portal:
            return PortalUserClassification.UNKNOWN
        if portal in STAFF_PORTAL_TYPES:
            return PortalUserClassification.STAFF
        if portal == UserType.FACULTY:
            return PortalUserClassification.FACULTY
        if portal in STUDENT_PORTAL_TYPES:
            profile = cls.get_profile(user)
            degree = (profile.student_degree_name if profile else "") or getattr(user, "degree_name", "") or ""
            degree_class, _ = cls.classify_degree(degree)
            if degree_class == DegreeClassificationKind.UNDERGRADUATE:
                return PortalUserClassification.UNDERGRADUATE_STUDENT
            if not degree_class:
                return PortalUserClassification.UNKNOWN
            return PortalUserClassification.OTHER_STUDENT
        return PortalUserClassification.UNKNOWN

    @classmethod
    def view(cls, user) -> IdentityView:
        profile = cls.get_profile(user)
        classification = cls.classify_user(user)
        channel_dept = ""
        if profile:
            channel_dept = profile.student_department_name or profile.faculty_department_name or ""
        mapping, dept_status = cls.map_department(channel_dept) if channel_dept else (None, "UNRESOLVED")
        internal = mapping.internal_department if mapping and mapping.internal_department_id else None
        validity = cls.compute_validity(profile, user=user)
        degree_name = (profile.student_degree_name if profile else "") or getattr(user, "degree_name", "") or ""
        degree_class, _ = cls.classify_degree(degree_name)
        return IdentityView(
            classification=classification,
            portal_user_type=getattr(user, "user_type", "") or "",
            is_undergraduate=classification == PortalUserClassification.UNDERGRADUATE_STUDENT,
            is_student=classification
            in {
                PortalUserClassification.UNDERGRADUATE_STUDENT,
                PortalUserClassification.OTHER_STUDENT,
            }
            or (getattr(user, "user_type", None) in STUDENT_PORTAL_TYPES),
            is_hod=classification == PortalUserClassification.HEAD_OF_DEPARTMENT,
            channel_i_department_name=channel_dept or "Not available",
            internal_department_id=internal.id if internal else None,
            internal_department_name=internal.name if internal else "Not available",
            department_mapped=dept_status == "MAPPED",
            department_status=dept_status,
            validity=validity,
            degree_name=degree_name or "Not available",
            degree_classification=degree_class or "UNKNOWN",
            channel_i_user_id=(profile.channel_i_user_id if profile else "") or getattr(user, "internal_id", "") or "Not available",
            channel_i_username=(profile.channel_i_username if profile else "") or "Not available",
            student_start_date=profile.student_start_date if profile else None,
            extras={
                "derived_end_date": validity.derived_end_date,
                "channel_i_end_date": validity.channel_i_end_date,
                "validity_source": validity.validity_source,
            },
        )


def models_q_effective_to(today: date):
    from django.db.models import Q

    return Q(effective_to__isnull=True) | Q(effective_to__gte=today)


class UserEligibilityService:
    @staticmethod
    def is_disabled(user) -> bool:
        """Lifecycle disable (force_inactive). Do not treat pending-approval is_active=False as this."""
        return bool(getattr(user, "force_inactive", False))

    @classmethod
    def can_request_wallet_credit(cls, user) -> tuple[bool, str, str]:
        if not wallet_credit_enabled():
            return False, "FEATURE_DISABLED", "Wallet Credit Facility is not enabled."
        if cls.is_disabled(user):
            return False, "ACCOUNT_DISABLED", "Disabled users cannot request wallet credit."
        view = UserIdentityService.view(user)
        if view.classification in {
            PortalUserClassification.UNDERGRADUATE_STUDENT,
            PortalUserClassification.OTHER_STUDENT,
        }:
            return (
                False,
                "CREDIT_NOT_ALLOWED_FOR_USER_TYPE",
                "Wallet Credit Facility is available only to eligible faculty/staff/internal users. "
                "Student accounts are not eligible.",
            )
        if view.portal_user_type in STUDENT_PORTAL_TYPES:
            return (
                False,
                "CREDIT_NOT_ALLOWED_FOR_USER_TYPE",
                "Wallet Credit Facility is available only to eligible faculty/staff/internal users. "
                "Student accounts are not eligible.",
            )
        if view.classification == PortalUserClassification.UNKNOWN:
            return (
                False,
                "USER_TYPE_UNKNOWN",
                "Your institutional user type is not established. Credit requests require administrator/profile verification.",
            )
        if view.classification not in CREDIT_ELIGIBLE_CLASSIFICATIONS:
            return False, "CREDIT_NOT_ALLOWED_FOR_USER_TYPE", "Your user type is not eligible for Wallet Credit Facility."
        return True, "OK", "Eligible to request credit."

    @classmethod
    def can_create_booking(cls, user) -> tuple[bool, str]:
        if student_lifecycle_enabled() and cls.is_disabled(user):
            return False, "ACCOUNT_DISABLED"
        return True, "OK"

    @classmethod
    def can_create_affiliation(cls, user) -> tuple[bool, str]:
        if student_lifecycle_enabled() and cls.is_disabled(user):
            return False, "ACCOUNT_DISABLED"
        return True, "OK"

    @classmethod
    def evaluate_hod_join(cls, student, target_user) -> tuple[bool, str, str]:
        """Return (ok, code, message). Caller uses this when target is an active HoD."""
        if not hod_affiliation_enabled():
            return True, "OK", ""
        if cls.is_disabled(student) and student_lifecycle_enabled():
            return False, "ACCOUNT_DISABLED", "Disabled students cannot create affiliation requests."
        view = UserIdentityService.view(student)
        if view.classification == PortalUserClassification.UNKNOWN and view.portal_user_type in STUDENT_PORTAL_TYPES:
            if view.degree_classification == "UNKNOWN" and view.is_student:
                # Unclassified Channel-I degree
                profile = UserIdentityService.get_profile(student)
                if profile and profile.has_student_payload and not UserIdentityService.classify_degree(
                    profile.student_degree_name
                )[0]:
                    return (
                        False,
                        "USER_TYPE_UNRESOLVED",
                        "Your user classification is unresolved. Contact the administrator.",
                    )
        hod = (
            HeadOfDepartmentAssignment.objects.filter(user=target_user, active=True)
            .filter(models_q_effective_to(timezone.localdate()))
            .select_related("department")
            .first()
        )
        if not hod:
            return True, "OK", ""  # not an HoD path
        if view.classification == PortalUserClassification.OTHER_STUDENT or (
            view.portal_user_type in STUDENT_PORTAL_TYPES and not view.is_undergraduate
        ):
            if view.classification != PortalUserClassification.UNDERGRADUATE_STUDENT:
                return (
                    False,
                    "HOD_NOT_AVAILABLE_FOR_USER_TYPE",
                    "Head of Department affiliation is available only to undergraduate students.",
                )
        if view.department_status != "MAPPED" or not view.internal_department_id:
            return (
                False,
                "STUDENT_DEPARTMENT_UNRESOLVED",
                "Your institutional department is not yet mapped in the portal. Please contact the administrator.",
            )
        if hod.department_id != view.internal_department_id:
            return (
                False,
                "HOD_DEPARTMENT_MISMATCH",
                "Undergraduate students may join only the Head of Department of their own department.",
            )
        return True, "OK", ""

    @classmethod
    def get_valid_hod(cls, student):
        if not hod_affiliation_enabled():
            return None
        view = UserIdentityService.view(student)
        if not view.is_undergraduate or not view.internal_department_id:
            return None
        return UserIdentityService.active_hod_for_department(view.internal_department_id)

    @classmethod
    def is_wallet_owner_of(cls, faculty, student) -> bool:
        return WalletJoinRequest.objects.filter(
            student=student,
            faculty=faculty,
            status=WalletJoinRequestStatus.APPROVED,
        ).exists()
