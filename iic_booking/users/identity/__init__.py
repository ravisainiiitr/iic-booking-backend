"""Central Channel-I identity, classification, and eligibility."""

from .flags import (
    hod_affiliation_enabled,
    student_lifecycle_enabled,
    wallet_credit_enabled,
    department_mapping_enabled,
)
from .extract import extract_channel_i_academic_facts
from .service import UserEligibilityService, UserIdentityService

__all__ = [
    "UserIdentityService",
    "UserEligibilityService",
    "extract_channel_i_academic_facts",
    "hod_affiliation_enabled",
    "student_lifecycle_enabled",
    "wallet_credit_enabled",
    "department_mapping_enabled",
]
