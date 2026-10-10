"""Who supervises a user — one rule for the user card, the Users overview table and its export.

IITR students are linked to their supervisor through the supervisor's wallet (an approved request to join it); the
profile ``supervisor`` field is only filled for Post-docs and Research Associates. First match wins:

1. ``profile`` — the profile supervisor, once the account no longer awaits their approval;
2. ``wallet`` — the owner of the wallet the user's approved join request links them to;
3. ``registration`` — the faculty member who approved a self-registered account;
4. not yet confirmed (``pending`` is true): a profile supervisor who has not approved the account, a registration
   request awaiting the faculty, a pending request to join a wallet, a pending email invite to a supervisor who is
   not on the portal yet;
5. ``booking`` — the owner of the wallet charged for the user's most recent booking.

Faculty have no supervisor. Every source is fetched for the whole batch at once (one query each).
"""

from __future__ import annotations

from typing import Any, Iterable

from django.contrib.auth import get_user_model
from django.db.models import F
from django.db.models.functions import Lower

SOURCES = {
    "profile": "Supervisor on the profile",
    "wallet": "Linked to the supervisor's wallet",
    "registration": "Approved the registration",
    "pending_profile": "Supervisor on the profile — has not approved the account yet",
    "pending_registration": "Registration sent to the supervisor — awaiting their decision",
    "pending_wallet": "Asked to join the supervisor's wallet — awaiting approval",
    "pending_invite": "Supervisor invited by email — not on the portal yet",
    "booking": "Owner of the wallet charged for the latest booking",
}
PENDING_SOURCES = frozenset({"pending_profile", "pending_registration", "pending_wallet", "pending_invite"})
POSTDOC_ALIASES = ("IITR Post Doctoral Fellows", "IITR Research Associates in Projects")


def _needs_profile_approval(alias: str | None) -> bool:
    return (alias or "").strip() in POSTDOC_ALIASES


def resolve_supervisors(user_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """``{user id: supervisor}`` for the users that have one; see the module docstring for the precedence."""
    from iic_booking.users.display import get_user_display_name
    from iic_booking.users.models import RegistrationApproval, SupervisorInvite, SupervisorInviteStatus
    from iic_booking.users.models.registration_approval import RegistrationApprovalStatus
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.models.wallet import SubWalletTransaction, WalletJoinRequest, WalletJoinRequestStatus

    User = get_user_model()
    ids = sorted({int(i) for i in user_ids if i})
    if not ids:
        return {}
    people = {
        p["id"]: p
        for p in User.objects.filter(pk__in=ids).values("id", "user_type", "user_type_alias", "supervisor_id", "supervisor_approved")
    }
    found: dict[int, tuple[str, int | None, dict[str, str] | None]] = {}

    def take(uid: int, source: str, sup_id: int | None = None, captured: dict[str, str] | None = None) -> None:
        if uid in found or uid not in people or (sup_id is None and not captured) or sup_id == uid:
            return
        found[uid] = (source, sup_id, captured)

    def left() -> list[int]:
        return [i for i in people if i not in found and people[i]["user_type"] != UserType.FACULTY]

    for uid, p in people.items():
        if p["user_type"] != UserType.FACULTY and p["supervisor_id"]:
            if not _needs_profile_approval(p["user_type_alias"]) or p["supervisor_approved"]:
                take(uid, "profile", p["supervisor_id"])

    def joins(status: str, source: str) -> None:
        pending = left()
        if not pending:
            return
        rows = (
            WalletJoinRequest.objects.filter(student_id__in=pending, status=status)
            .order_by("-updated_at", "-pk")
            .values_list("student_id", "wallet__user_id", "faculty_id")
        )
        for student, owner, faculty in rows:
            take(student, source, owner or faculty)

    def registrations(statuses: list[str], source: str) -> None:
        pending = left()
        if not pending:
            return
        rows = RegistrationApproval.objects.filter(
            user_id__in=pending, status__in=statuses, faculty__isnull=False
        ).values_list("user_id", "faculty_id")
        for uid, faculty in rows:
            take(uid, source, faculty)

    joins(WalletJoinRequestStatus.APPROVED, "wallet")
    registrations([RegistrationApprovalStatus.APPROVED], "registration")

    for uid in left():
        p = people[uid]
        if p["supervisor_id"]:
            take(uid, "pending_profile", p["supervisor_id"])
    registrations(
        [RegistrationApprovalStatus.PENDING_FACULTY, RegistrationApprovalStatus.PENDING_ADMIN], "pending_registration"
    )
    joins(WalletJoinRequestStatus.PENDING, "pending_wallet")

    pending = left()
    if pending:
        invites = list(
            SupervisorInvite.objects.filter(student_id__in=pending, status=SupervisorInviteStatus.PENDING)
            .order_by("-created_at", "-pk")
            .values("student_id", "email", "supervisor_name", "department__name")
        )
        emails = {(i["email"] or "").strip().lower() for i in invites} - {""}
        on_portal = (
            dict(User.objects.annotate(e=Lower("email")).filter(e__in=emails).values_list("e", "pk")) if emails else {}
        )
        for i in invites:
            email = (i["email"] or "").strip()
            take(
                i["student_id"],
                "pending_invite",
                on_portal.get(email.lower()),
                {"name": (i["supervisor_name"] or "").strip(), "email": email, "department": i["department__name"] or ""},
            )

    pending = left()
    if pending:
        charged = (
            SubWalletTransaction.objects.filter(
                related_user_id__in=pending, transaction_type=SubWalletTransaction.TransactionType.DEBIT
            )
            .exclude(sub_wallet__wallet__user_id=F("related_user_id"))
            .order_by("related_user_id", "-created_at", "-pk")
            .distinct("related_user_id")
            .values_list("related_user_id", "sub_wallet__wallet__user_id")
        )
        for uid, owner in charged:
            take(uid, "booking", owner)

    supervisors = {
        u.pk: u
        for u in User.objects.filter(pk__in={s for _, s, _ in found.values() if s}).select_related("department")
    }
    out: dict[int, dict[str, Any]] = {}
    for uid, (source, sup_id, captured) in found.items():
        sup = supervisors.get(sup_id) if sup_id else None
        captured = captured or {}
        if sup is None and not (captured.get("name") or captured.get("email")):
            continue
        out[uid] = {
            "id": sup.pk if sup else None,
            "name": (get_user_display_name(sup) if sup else "") or captured.get("name") or captured.get("email") or "",
            "email": (sup.email if sup else "") or captured.get("email") or "",
            "department": (sup.department.name if sup and sup.department_id else "") or captured.get("department") or "",
            "source": source,
            "source_display": SOURCES[source],
            "pending": source in PENDING_SOURCES,
        }
    return out


def supervisor_label(entry: dict[str, Any] | None) -> str:
    """One-line text for tables and exports: ``Name (pending confirmation)``; empty when not linked."""
    if not entry:
        return ""
    return f"{entry['name']} (pending confirmation)" if entry.get("pending") else entry["name"]
