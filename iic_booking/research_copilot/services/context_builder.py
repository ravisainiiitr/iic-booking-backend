"""Role-aware portal context pack (no secrets, no cross-user data in AI.1)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class CopilotContext:
    user_id: int | None
    display_name: str
    email: str
    user_type: str
    role_bucket: str
    department_id: int | None
    department_name: str
    capabilities: list[str] = field(default_factory=list)
    lifecycle_hint: str = (
        "Research Idea → Equipment Selection → Booking → Wallet → Sample → "
        "Experiment → Results → Remote Analysis → Support → Publication"
    )

    def to_dict(self) -> dict:
        return asdict(self)


def _role_bucket(user_type: str) -> str:
    t = (user_type or "").strip().lower()
    if t in {"admin", "superuser"}:
        return "admin"
    if t in {"dept_admin", "department_admin"}:
        return "dept_admin"
    if t in {"manager", "oic", "lab_incharge"}:
        return "operator"
    if t in {"operator"}:
        return "operator"
    if t in {"faculty", "staff"}:
        return "faculty"
    if t in {"external", "external_user", "industry"}:
        return "external"
    if t in {"student", "phd", "mtech", "btech", "project_student"}:
        return "student"
    return "default"


def _capabilities(bucket: str) -> list[str]:
    base = ["ask_docs", "booking_guidance", "status_guidance", "escalate_support"]
    if bucket in {"faculty", "student", "external", "default"}:
        base += ["wallet_guidance", "equipment_advisor"]
    if bucket in {"operator", "dept_admin", "admin"}:
        base += ["operator_guidance", "equipment_health"]
    if bucket in {"dept_admin", "admin"}:
        base += ["dsa_guidance", "deployment_guidance"]
    if bucket == "admin":
        base += ["admin_guidance"]
    return base


def build_context(user) -> CopilotContext:
    user_type = str(getattr(user, "user_type", "") or "")
    bucket = _role_bucket(user_type)
    dept = getattr(user, "department", None)
    return CopilotContext(
        user_id=getattr(user, "id", None),
        display_name=(getattr(user, "name", None) or getattr(user, "get_full_name", lambda: "")() or "")[:128],
        email=(getattr(user, "email", None) or "")[:255],
        user_type=user_type,
        role_bucket=bucket,
        department_id=getattr(dept, "id", None) if dept is not None else getattr(user, "department_id", None),
        department_name=(getattr(dept, "name", None) or "")[:255] if dept is not None else "",
        capabilities=_capabilities(bucket),
    )
