"""Permission ranks for knowledge retrieval (Phase AI.2)."""

from __future__ import annotations

from iic_booking.research_copilot.models import SecurityLevel

SECURITY_RANK = {
    SecurityLevel.PUBLIC: 0,
    SecurityLevel.AUTHENTICATED: 1,
    SecurityLevel.OPERATOR: 2,
    SecurityLevel.DEPT_ADMIN: 3,
    SecurityLevel.ADMIN: 4,
}


def role_bucket_rank(role_bucket: str) -> int:
    b = (role_bucket or "default").lower()
    if b in {"public", "anonymous", "guest"}:
        return SECURITY_RANK[SecurityLevel.PUBLIC]
    if b == "admin":
        return SECURITY_RANK[SecurityLevel.ADMIN]
    if b == "dept_admin":
        return SECURITY_RANK[SecurityLevel.DEPT_ADMIN]
    if b == "operator":
        return SECURITY_RANK[SecurityLevel.OPERATOR]
    if b in {"student", "faculty", "external", "default"}:
        return SECURITY_RANK[SecurityLevel.AUTHENTICATED]
    return SECURITY_RANK[SecurityLevel.AUTHENTICATED]


def allowed_security_levels(role_bucket: str) -> set[str]:
    rank = role_bucket_rank(role_bucket)
    return {level for level, r in SECURITY_RANK.items() if r <= rank}


def can_access_document(*, role_bucket: str, security_level: str, department_id: int | None, user_department_id: int | None) -> bool:
    if security_level not in allowed_security_levels(role_bucket):
        return False
    # Department-scoped docs: non-admins must match department when set
    if department_id and role_bucket not in {"admin"}:
        if user_department_id is None or int(user_department_id) != int(department_id):
            # Operator manuals / dept docs — deny cross-dept
            if security_level in {
                SecurityLevel.OPERATOR,
                SecurityLevel.DEPT_ADMIN,
                SecurityLevel.ADMIN,
            }:
                return False
    return True
