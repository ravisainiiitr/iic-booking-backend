"""Report-facing user categories: one mapping from user-type codes to the names shown in reports and exports.

Booking ``user_type_snapshot`` values and ``User.user_type`` codes come in mixed case (``RND``, ``Industry``); every
lookup is case-insensitive. The frontend mirrors this in ``src/lib/userCategories.ts``.
"""

from __future__ import annotations

from .models.user_type import UserType

CATEGORY_LABELS: dict[str, str] = {
    "iitr_student": "IITR Students",
    "iitr_faculty": "IITR Faculty",
    "iitr_staff": "IITR Staff",
    "iitr_startup": "IITR Startups",
    "external_academic": "External – Educational / Academic",
    "external_industry": "External – Industry",
    "government_rnd": "External – Govt R&D",
    "external_startup": "External – Startup / MSME",
    "other": "External – Other",
}
INTERNAL_CATEGORIES = ("iitr_faculty", "iitr_student", "iitr_startup", "iitr_staff")
EXTERNAL_CATEGORIES = ("external_academic", "external_industry", "government_rnd", "external_startup", "other")
NOT_RECORDED = "Not recorded"

STAFF_TYPES = (
    UserType.ADMIN,
    UserType.DEPT_ADMIN,
    UserType.MANAGER,
    UserType.OPERATOR,
    UserType.FINANCE,
    UserType.OC_STORES,
    UserType.HOD,
    UserType.EXTERNAL_RELATIONS,
    UserType.ORG_ADMIN,
)

_CODE_TO_CATEGORY: dict[str, str] = {
    UserType.STUDENT: "iitr_student",
    UserType.INDIVIDUAL_STUDENT: "iitr_student",
    UserType.FACULTY: "iitr_faculty",
    UserType.STARTUP_INCUBATED_IITR: "iitr_startup",
    **{code: "iitr_staff" for code in STAFF_TYPES},
    UserType.EXTERNAL: "external_academic",
    UserType.INSTITUTE.lower(): "external_industry",
    UserType.RND.lower(): "government_rnd",
    UserType.EXTERNAL_STARTUP_MSME: "external_startup",
    UserType.OTHER: "other",
}


def normalize_code(code: str | None) -> str:
    return str(code or "").strip().lower()


def category_for(code: str | None) -> str:
    """Category key of a user-type code; unknown or empty codes are ``other``."""
    return _CODE_TO_CATEGORY.get(normalize_code(code), "other")


def category_label(code: str | None) -> str:
    """``student`` → ``IITR Students``, ``RND`` → ``External – Govt R&D``; empty → ``Not recorded``."""
    if not normalize_code(code):
        return NOT_RECORDED
    return CATEGORY_LABELS[category_for(code)]


def is_internal(code: str | None) -> bool:
    return category_for(code) in INTERNAL_CATEGORIES
