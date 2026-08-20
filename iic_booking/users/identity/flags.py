from django.conf import settings


def department_mapping_enabled() -> bool:
    return bool(getattr(settings, "DEPARTMENT_MAPPING_ENABLED", False))


def hod_affiliation_enabled() -> bool:
    return bool(getattr(settings, "HOD_AFFILIATION_ENABLED", False))


def student_lifecycle_enabled() -> bool:
    return bool(getattr(settings, "STUDENT_LIFECYCLE_ENABLED", False))


def wallet_credit_enabled() -> bool:
    if hasattr(settings, "WALLET_CREDIT_ENABLED"):
        if getattr(settings, "WALLET_CREDIT_ENABLED", False):
            return True
    return bool(getattr(settings, "WALLET_CREDIT_FACILITY_V2_ENABLED", False))
