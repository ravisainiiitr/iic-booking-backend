"""Channel-I / Omniport identity extraction.

Does NOT treat Channel-I username as the institutional Employee ID until an
operator-approved claim is verified. Wallet linking remains exact Employee ID.

This module is analysis + future-backfill logic. It does not write to production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROVIDER = "channeli_omniport"

# Claims this codebase actually reads today (auth_views.omniport_callback).
MAPPED_CLAIMS = {
    "userId": "User.internal_id (create only; not updated on existing login)",
    "username": "User.emp_id (create, and existing only if emp_id empty and unique)",
    "person.fullName": "User.name (existing: only if name empty)",
    "person.displayPicture": "User.profile_picture (only if none stored)",
    "student / student_member": "UserType.STUDENT + branch/degree/department/dates",
    "facultyMember / faculty_member": "UserType.FACULTY + department/designation/joining",
    "contactInformation.instituteWebmailAddress": "User.email (login key; not updated later)",
    "contactInformation.primaryPhoneNumber": "User.phone_number (create defaults only)",
    "biologicalInformation.dateOfBirth": "User.date_of_birth",
    "roles": "fallback user_type when student/faculty objects empty",
}

# Candidate institutional-ID claim names. Presence is inspected; none is assumed.
CANDIDATE_EMPLOYEE_ID_PATHS = (
    ("facultyMember", "employeeId"),
    ("facultyMember", "employee_id"),
    ("faculty_member", "employeeId"),
    ("faculty_member", "employee_id"),
    ("facultyMember", "employeeNumber"),
    ("student", "enrolmentNumber"),
    ("student", "enrolment_number"),
    ("student_member", "enrolmentNumber"),
)


def _blank(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _nested(obj: dict, *keys: str) -> Any:
    cur: Any = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def looks_like_iic_operator_code(raw: str) -> bool:
    s = _blank(raw)
    if not s:
        return False
    upper = s.upper()
    if upper.startswith("IIC") and not s.isdigit():
        return True
    if upper in {"ADMIN", "OFFICE_IIC"}:
        return True
    return False


@dataclass
class ChannelIIdentityClaims:
    """Parsed userinfo. No secrets. Token bodies are never stored here."""

    provider: str = PROVIDER
    provider_subject: str = ""  # userId
    channel_i_username: str = ""
    email: str = ""
    name: str = ""
    student_enrolment_number: str = ""
    faculty_employee_id_claim: str = ""
    other_candidate_ids: dict[str, str] = field(default_factory=dict)
    present_top_level_keys: list[str] = field(default_factory=list)
    # Wallet key candidate. UNVERIFIED until operator confirms Channel-I schema.
    candidate_employee_id: str = ""
    candidate_employee_id_source: str = ""
    username_equals_candidate: bool = False
    username_is_operator_code: bool = False
    verified: bool = False  # always False until operator/IMG confirmation

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "provider_subject": self.provider_subject,
            "channel_i_username": self.channel_i_username,
            "email": self.email,
            "name": self.name,
            "student_enrolment_number": self.student_enrolment_number,
            "faculty_employee_id_claim": self.faculty_employee_id_claim,
            "other_candidate_ids": self.other_candidate_ids,
            "present_top_level_keys": self.present_top_level_keys,
            "candidate_employee_id": self.candidate_employee_id,
            "candidate_employee_id_source": self.candidate_employee_id_source,
            "username_equals_candidate": self.username_equals_candidate,
            "username_is_operator_code": self.username_is_operator_code,
            "verified": self.verified,
        }


def extract_channel_i_identity(user_info: dict | None) -> ChannelIIdentityClaims:
    info = user_info if isinstance(user_info, dict) else {}
    claims = ChannelIIdentityClaims()
    claims.present_top_level_keys = sorted(str(k) for k in info.keys())
    claims.provider_subject = _blank(info.get("userId") or info.get("user_id") or info.get("sub"))
    claims.channel_i_username = _blank(info.get("username") or info.get("preferred_username"))
    person = info.get("person") if isinstance(info.get("person"), dict) else {}
    claims.name = _blank(person.get("fullName") or person.get("full_name"))
    contact = info.get("contactInformation") or info.get("contact_information") or {}
    if isinstance(contact, dict):
        claims.email = _blank(
            contact.get("instituteWebmailAddress") or contact.get("institute_webmail_address")
        )

    for path in CANDIDATE_EMPLOYEE_ID_PATHS:
        val = _blank(_nested(info, *path))
        if val:
            claims.other_candidate_ids[".".join(path)] = val

    student = info.get("student") or info.get("student_member") or {}
    if isinstance(student, dict):
        claims.student_enrolment_number = _blank(
            student.get("enrolmentNumber") or student.get("enrolment_number")
        )
    faculty = info.get("facultyMember") or info.get("faculty_member") or {}
    if isinstance(faculty, dict):
        claims.faculty_employee_id_claim = _blank(
            faculty.get("employeeId")
            or faculty.get("employee_id")
            or faculty.get("employeeNumber")
            or faculty.get("employee_no")
            or faculty.get("staff_id")
        )

    # Username is Channel-I login identity, never an automatic Employee ID.
    if claims.faculty_employee_id_claim:
        claims.candidate_employee_id = claims.faculty_employee_id_claim
        claims.candidate_employee_id_source = "facultyMember.employeeId"
    elif claims.student_enrolment_number:
        claims.candidate_employee_id = claims.student_enrolment_number
        claims.candidate_employee_id_source = "student.enrolmentNumber"
    claims.username_equals_candidate = bool(
        claims.channel_i_username
        and claims.candidate_employee_id
        and claims.channel_i_username == claims.candidate_employee_id
    )
    claims.username_is_operator_code = looks_like_iic_operator_code(claims.channel_i_username)
    claims.verified = False
    return claims


@dataclass
class IdentityApplyDecision:
    action: str
    employee_id: str = ""
    reason: str = ""
    status: str = "UNVERIFIED"


def decide_employee_id_on_login(
    *,
    existing_emp_id: str,
    claims: ChannelIIdentityClaims,
    other_user_has_candidate: bool,
    operator_confirmed_claim: str = "",
) -> IdentityApplyDecision:
    """Safe backfill rules. Never guess. Never overwrite a different emp_id.

    Unverified candidates are never written. Username is never Employee ID.
    """
    existing = _blank(existing_emp_id)
    candidate = _blank(claims.candidate_employee_id)
    if operator_confirmed_claim and claims.candidate_employee_id_source == operator_confirmed_claim and candidate:
        claims.verified = True
    if existing:
        if claims.verified and candidate and existing != candidate:
            return IdentityApplyDecision(
                action="conflict",
                employee_id=existing,
                reason="existing User.emp_id differs from verified Channel-I Employee ID; do not overwrite",
                status="CONFLICT",
            )
        return IdentityApplyDecision(
            action="unchanged",
            employee_id=existing,
            reason="existing emp_id preserved; no silent overwrite",
            status="VERIFIED" if claims.verified and existing == candidate else "LEGACY_UNVERIFIED",
        )
    if looks_like_iic_operator_code(candidate):
        return IdentityApplyDecision(
            action="skip",
            reason="IIC operator/instrument code is not an institutional Employee ID",
            status="CONFLICT",
        )
    if not claims.verified or not candidate:
        return IdentityApplyDecision(
            action="skip",
            reason="unverified or missing Employee-ID claim; leave emp_id empty",
            status="UNVERIFIED",
        )
    if other_user_has_candidate:
        return IdentityApplyDecision(
            action="skip",
            reason="candidate Employee ID already belongs to another user",
            status="CONFLICT",
        )
    return IdentityApplyDecision(
        action="set_verified",
        employee_id=candidate,
        reason="empty emp_id populated from operator-confirmed Channel-I claim",
        status="CHANNEL_I_VERIFIED",
    )


def resolve_employee_id_for_omniport(
    *,
    existing_emp_id: str,
    user_info: dict,
    other_user_has_candidate: bool,
    authoritative_claim: str = "",
) -> IdentityApplyDecision:
    claims = extract_channel_i_identity(user_info)
    return decide_employee_id_on_login(
        existing_emp_id=existing_emp_id,
        claims=claims,
        other_user_has_candidate=other_user_has_candidate,
        operator_confirmed_claim=_blank(authoritative_claim),
    )


MIGRATION_ELIGIBLE_SOURCES = frozenset(
    {
        "CHANNEL_I_VERIFIED",
        "IMG_VERIFIED",
        "INSTITUTIONAL_DIRECTORY",
        "MANUAL_ADMIN_VERIFIED",
    }
)


def is_wallet_migration_eligible(
    *,
    employee_id: str,
    production_user_count: int,
    identity_source: str = "LEGACY_UNVERIFIED",
    has_conflict: bool = False,
    user_is_active: bool = True,
) -> tuple[bool, str]:
    """Wallet import is allowed only for a verified, unique Employee ID."""
    emp = _blank(employee_id)
    if not emp:
        return False, "WALLET_MAPPING_EXCEPTION: employee ID missing"
    if looks_like_iic_operator_code(emp):
        return False, "WALLET_MAPPING_EXCEPTION: IIC operator code is not HR Employee ID"
    if has_conflict:
        return False, "WALLET_MAPPING_EXCEPTION: identity conflict"
    if production_user_count != 1:
        return False, "WALLET_MAPPING_EXCEPTION: Employee ID must map to exactly one production user"
    if not user_is_active:
        return False, "WALLET_MAPPING_EXCEPTION: production user inactive"
    if identity_source not in MIGRATION_ELIGIBLE_SOURCES:
        return False, "WALLET_MAPPING_EXCEPTION: identity not verified for wallet migration"
    return True, "MIGRATION_ELIGIBLE"


LEGACY_AUTHORITATIVE = "AUTHORITATIVE_EMP_ID"
LEGACY_NO_EMP = "NO_EMP_ID"
LEGACY_DUP = "DUPLICATE_EMP_ID"
LEGACY_CONFLICT = "CONFLICTING_IDENTITY"
LEGACY_NEEDS_EXTERNAL = "NEEDS_EXTERNAL_VERIFICATION"


def classify_legacy_identity(emp_id: str, *, duplicate_ids: set[str], has_wallet: bool) -> str:
    emp = _blank(emp_id)
    if not emp:
        return LEGACY_NO_EMP
    if emp in duplicate_ids:
        return LEGACY_DUP
    if looks_like_iic_operator_code(emp):
        return LEGACY_CONFLICT
    # Unique non-empty emp_id on old portal is the only authoritative wallet key
    # present in legacy MySQL. It is still not proven equal to Channel-I.
    if has_wallet:
        return LEGACY_AUTHORITATIVE
    return LEGACY_AUTHORITATIVE


MANUAL_EXCEPTION_FIELDS = (
    "legacy_user_id",
    "legacy_wallet_id",
    "legacy_name",
    "legacy_email",
    "proposed_employee_id",  # empty unless administrator fills it
    "verification_source",
    "verified_by",
    "verified_at",
    "status",
)

STAGING_STATUSES = (
    "UNREVIEWED",
    "VERIFIED",
    "EXCEPTION",
    "CONFLICT",
    "APPROVED_FOR_MIGRATION",
    "REJECTED",
)
