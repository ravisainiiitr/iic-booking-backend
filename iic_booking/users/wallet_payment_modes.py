"""Effective wallet payment options per department, email recipients and direct wallet recharge.

Semantics
---------
* The global switch for each option is the master. Master OFF = off everywhere.
* Master ON = each department follows its own state: ``inherit`` (default, i.e. on) or ``disabled``.
* Credit Limit is opt-in per department through ``Department.enable_wallet_credit`` (the same switch
  shown in Department settings), so the credit column edits that field instead of a second layer.
* "Department" always means the department of the sub-wallet being funded, debited or credited.

The new tables may not exist for a few minutes after a deploy (code goes live before ``migrate``).
Every read goes through ``_safe`` inside a savepoint and falls back to today's global behaviour.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, TypeVar

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, OperationalError, ProgrammingError, transaction
from django.db.models import Q
from django.utils import timezone

from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet_payment_modes import (
    DepartmentModeState,
    WalletDirectRecharge,
    WalletDirectRechargeGrant,
    WalletDirectRechargeMode,
    WalletModeDepartmentSetting,
    WalletModeEmailRecipients,
    WalletModeOption,
    WalletPaymentModeAuditEvent,
    WalletPaymentModeConfig,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
SCHEMA_ERRORS = (ProgrammingError, OperationalError)
AWAITING_APPROVAL_MESSAGE = "Awaiting Competent Authority Approval."

OPTIONS: tuple[str, ...] = tuple(WalletModeOption.values)
DEPARTMENT_STATE_OPTIONS: tuple[str, ...] = (
    WalletModeOption.PROJECT_GRANT,
    WalletModeOption.DIRECT_CASH,
    WalletModeOption.ONLINE_GATEWAY,
    WalletModeOption.PEER_TRANSFER,
    WalletModeOption.DIRECT_RECHARGE,
)
# Keys used by the user-facing wallet settings payload (``wallet_mode_flags``).
FLAG_KEYS: dict[str, str] = {
    WalletModeOption.PROJECT_GRANT: "project_grant_recharge_enabled",
    WalletModeOption.DIRECT_CASH: "direct_cash_recharge_enabled",
    WalletModeOption.ONLINE_GATEWAY: "online_gateway_recharge_enabled",
    WalletModeOption.PEER_TRANSFER: "peer_transfer_enabled",
    WalletModeOption.CREDIT: "credit_facility_enabled",
    WalletModeOption.DIRECT_RECHARGE: "direct_recharge_enabled",
}


class SchemaPending(Exception):
    """Raised by writers when the new tables are not migrated yet."""


def _safe(fn: Callable[[], T], default: T) -> T:
    try:
        with transaction.atomic():
            return fn()
    except SCHEMA_ERRORS:
        logger.warning("wallet payment mode tables unavailable; using global behaviour", exc_info=True)
        return default


def schema_ready() -> bool:
    def probe() -> bool:
        WalletModeDepartmentSetting.objects.exists()
        WalletDirectRecharge.objects.exists()
        return True

    return _safe(probe, False)


def _department_id(department) -> int | None:
    if department is None or department == "":
        return None
    pk = getattr(department, "pk", department)
    try:
        return int(pk)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Master + department state
# ---------------------------------------------------------------------------


def direct_recharge_master_enabled() -> bool:
    return _safe(
        lambda: bool(
            WalletPaymentModeConfig.objects.filter(pk=1).values_list("direct_recharge_enabled", flat=True).first()
        ),
        False,
    )


def master_states() -> dict[str, bool]:
    from iic_booking.users.models.wallet_sric_settings import WalletSricSettings
    from iic_booking.users.wallet_credit_facility_v2 import feature_enabled as credit_feature_enabled

    s = WalletSricSettings.get_singleton()
    return {
        WalletModeOption.PROJECT_GRANT: bool(s.project_grant_recharge_enabled),
        WalletModeOption.DIRECT_CASH: bool(s.direct_cash_recharge_enabled),
        WalletModeOption.ONLINE_GATEWAY: bool(s.online_gateway_recharge_enabled),
        WalletModeOption.PEER_TRANSFER: bool(s.peer_transfer_enabled),
        WalletModeOption.CREDIT: bool(credit_feature_enabled()),
        WalletModeOption.DIRECT_RECHARGE: direct_recharge_master_enabled(),
    }


def master_enabled(option: str) -> bool:
    if option == WalletModeOption.DIRECT_RECHARGE:
        return direct_recharge_master_enabled()
    return master_states()[option]


def department_state(option: str, department) -> str:
    did = _department_id(department)
    if did is None or option not in DEPARTMENT_STATE_OPTIONS:
        return DepartmentModeState.INHERIT
    value = _safe(
        lambda: WalletModeDepartmentSetting.objects.filter(department_id=did).values_list(option, flat=True).first(),
        None,
    )
    return value or DepartmentModeState.INHERIT


def department_allows(option: str, department) -> bool:
    """Department-level check only (the caller checks the master)."""
    did = _department_id(department)
    if did is None:
        return True
    if option == WalletModeOption.CREDIT:
        from iic_booking.users.models.department import Department

        return Department.objects.filter(pk=did, enable_wallet_credit=True).exists()
    return department_state(option, did) != DepartmentModeState.DISABLED


def option_enabled(option: str, department=None) -> bool:
    return master_enabled(option) and department_allows(option, department)


def department_state_rows() -> dict[int, dict[str, str]]:
    def load():
        rows = WalletModeDepartmentSetting.objects.values("department_id", *DEPARTMENT_STATE_OPTIONS)
        return {r["department_id"]: {o: r[o] for o in DEPARTMENT_STATE_OPTIONS} for r in rows}

    return _safe(load, {})


def user_department_modes(masters: dict[str, bool], *, project_grant_exempt: bool = False) -> dict[str, dict[str, bool]]:
    """``{department_id: {flag_key: False}}`` for departments that switch an enabled master off."""
    out: dict[str, dict[str, bool]] = {}
    for dept_id, states in department_state_rows().items():
        off: dict[str, bool] = {}
        for option, state in states.items():
            if state != DepartmentModeState.DISABLED or not masters.get(option):
                continue
            if option == WalletModeOption.PROJECT_GRANT and project_grant_exempt:
                continue
            off[FLAG_KEYS[option]] = False
        if off:
            out[str(dept_id)] = off
    return out


def recharge_departments():
    from iic_booking.users.models.department import Department, DepartmentType
    from iic_booking.users.repositories.wallet_repository import get_internal_departments_with_equipment

    from iic_booking.users.models.wallet import SubWallet

    ids = set(get_internal_departments_with_equipment().values_list("id", flat=True))
    ids |= set(SubWallet.objects.values_list("department_id", flat=True).distinct())
    ids |= set(department_state_rows().keys())
    ids |= set(Department.objects.filter(enable_wallet_credit=True).values_list("id", flat=True))
    return Department.objects.filter(id__in=ids, department_type=DepartmentType.INTERNAL).order_by("name")


def set_department_states(changes: Iterable[dict[str, Any]], *, actor, ip: str | None = None) -> int:
    """Apply ``[{department_id, option, state}]``. Credit writes ``Department.enable_wallet_credit``."""
    from iic_booking.users.models.department import Department, DepartmentType

    changes = list(changes)
    applied = 0
    try:
        with transaction.atomic():
            for change in changes:
                did = _department_id(change.get("department_id"))
                option = change.get("option")
                state = change.get("state")
                dept = Department.objects.filter(pk=did, department_type=DepartmentType.INTERNAL).first()
                if dept is None:
                    raise ValueError(f"Department {change.get('department_id')} was not found.")
                if option == WalletModeOption.CREDIT:
                    if state not in ("enabled", "disabled"):
                        raise ValueError("Credit Limit accepts enabled or disabled.")
                    new = state == "enabled"
                    if bool(dept.enable_wallet_credit) != new:
                        Department.objects.filter(pk=dept.pk).update(enable_wallet_credit=new)
                        record_audit(
                            actor, "department_credit_changed", f"department:{dept.pk}",
                            {"enable_wallet_credit": not new}, {"enable_wallet_credit": new}, ip,
                        )
                        applied += 1
                    continue
                if option not in DEPARTMENT_STATE_OPTIONS:
                    raise ValueError(f"Unknown option {option!r}.")
                if state not in DepartmentModeState.values:
                    raise ValueError("State must be inherit or disabled.")
                row, _ = WalletModeDepartmentSetting.objects.select_for_update().get_or_create(department=dept)
                previous = getattr(row, option)
                if previous == state:
                    continue
                setattr(row, option, state)
                row.updated_by = actor if getattr(actor, "is_authenticated", False) else None
                row.save(update_fields=[option, "updated_by", "updated_at"])
                record_audit(actor, "department_mode_changed", f"department:{dept.pk}", {option: previous}, {option: state}, ip)
                applied += 1
    except SCHEMA_ERRORS as exc:
        raise SchemaPending(str(exc)) from exc
    return applied


def set_direct_recharge_master(enabled: bool, *, actor, ip: str | None = None) -> None:
    try:
        with transaction.atomic():
            cfg, _ = WalletPaymentModeConfig.objects.select_for_update().get_or_create(pk=1)
            if cfg.direct_recharge_enabled == enabled:
                return
            cfg.direct_recharge_enabled = enabled
            cfg.updated_by = actor if getattr(actor, "is_authenticated", False) else None
            cfg.save()
            record_audit(
                actor, "master_changed", "direct_recharge",
                {"direct_recharge_enabled": not enabled}, {"direct_recharge_enabled": enabled}, ip,
            )
    except SCHEMA_ERRORS as exc:
        raise SchemaPending(str(exc)) from exc


def record_audit(actor, action: str, target: str, before: dict | None, after: dict | None, ip: str | None = None) -> None:
    def write():
        WalletPaymentModeAuditEvent.objects.create(
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            action=action,
            target=target[:120],
            before=before or {},
            after=after or {},
            ip_address=ip or None,
        )

    _safe(write, None)


# ---------------------------------------------------------------------------
# Email recipients
# ---------------------------------------------------------------------------

ROLE_PREFIX = "role:"
ROLES: dict[str, tuple[str, str]] = {
    "sric_office": ("SRIC Office", "SRIC Office addresses from Wallet SRIC settings (receive Approve / Decline links)."),
    "sric_bill_section": ("SRIC Bill Section", "Bill Section addresses from Wallet SRIC settings."),
    "ar_sric": ("AR SRIC", "AR SRIC addresses from Wallet SRIC settings."),
    "dean_sric": ("Dean SRIC", "Dean SRIC addresses from Wallet SRIC settings."),
    "project_grant_cc": ("Project Grant extra CC", "Extra Project Grant CC addresses from Wallet SRIC settings."),
    "cash_deposit_cc": ("Cash deposit extra CC", "Extra Direct Cash / Bank Transfer CC addresses from Wallet SRIC settings."),
    "dept_admin": ("Department Administrators", "Department Administrators of the wallet's department."),
    "dept_account_incharge": ("Accounts In-charge", "Accounts In-charge of the wallet's department (any if none is set)."),
    "dept_oic": ("Department OICs", "Officers In-charge whose home department is the wallet's department."),
    "main_admin": ("Main Administrators", "Every active Main Administrator."),
    "wallet_owner": ("Wallet owner", "Owner of the wallet (the faculty member for a shared wallet)."),
}

# Today's recipients, expressed as roles so editing Wallet SRIC settings keeps working.
BUILTIN_RECIPIENTS: dict[str, tuple[list[str], list[str]]] = {
    WalletModeOption.PROJECT_GRANT: (["role:sric_office"], ["role:ar_sric", "role:dean_sric", "role:project_grant_cc"]),
    WalletModeOption.DIRECT_CASH: (["role:sric_bill_section"], ["role:ar_sric", "role:cash_deposit_cc"]),
    WalletModeOption.ONLINE_GATEWAY: ([], []),
    WalletModeOption.PEER_TRANSFER: (["role:dept_admin", "role:dept_account_incharge"], []),
    WalletModeOption.CREDIT: ([], []),
    WalletModeOption.DIRECT_RECHARGE: ([], []),
}
# The To list of these options receives Approve / Decline links, so it may not be empty.
TO_REQUIRED_OPTIONS = frozenset({WalletModeOption.PROJECT_GRANT, WalletModeOption.DIRECT_CASH})

RECIPIENT_NOTES: dict[str, str] = {
    WalletModeOption.PROJECT_GRANT: "To receives the request with Approve / Decline links; CC gets a copy without links.",
    WalletModeOption.DIRECT_CASH: "To receives the request with Approve / Decline links; CC gets a copy without links.",
    WalletModeOption.ONLINE_GATEWAY: "Sent when an online payment is credited. No email is sent while To is empty.",
    WalletModeOption.PEER_TRANSFER: "Sender and recipient always get their own email; To and CC get the staff copy.",
    WalletModeOption.CREDIT: "Sent when a credit request is submitted. No email is sent while To is empty.",
    WalletModeOption.DIRECT_RECHARGE: "The wallet owner is always in To; the person who recharged is in CC.",
}


def normalize_recipients(values: Iterable[Any] | None) -> tuple[list[str], list[str]]:
    """Validate, lower-case and de-duplicate a To / CC list. Returns ``(clean, errors)``."""
    clean: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()
    raw: list[str] = []
    for value in values or []:
        raw.extend(p for p in re.split(r"[\s,;]+", str(value or "").strip()) if p)
    for item in raw:
        token = item.strip()
        if token.lower().startswith(ROLE_PREFIX):
            key = token[len(ROLE_PREFIX):].strip().lower()
            if key not in ROLES:
                errors.append(f"Unknown role {key!r}.")
                continue
            token = ROLE_PREFIX + key
        else:
            token = token.lower()
            try:
                validate_email(token)
            except ValidationError:
                errors.append(f"{item} is not a valid email address.")
                continue
        if token in seen:
            continue
        seen.add(token)
        clean.append(token)
    return clean, errors


def _parse_emails(raw: str) -> list[str]:
    from iic_booking.users.wallet_recharge_ops import _parse_sric_recipient_emails

    return _parse_sric_recipient_emails(raw or "")


def _user_emails(users) -> list[str]:
    return [u.email for u in users if getattr(u, "email", None)]


def _role_emails(key: str, *, department=None, wallet_owner=None) -> list[str]:
    from django.contrib.auth import get_user_model

    from iic_booking.users.models.department import Department
    from iic_booking.users.models.wallet_sric_settings import WalletSricSettings
    from iic_booking.users.wallet_recharge_workflow import (
        find_department_account_incharges,
        find_department_administrators,
        get_sric_bill_section_emails,
        get_sric_recipient_emails,
    )

    User = get_user_model()
    did = _department_id(department)
    dept = department if isinstance(department, Department) else (Department.objects.filter(pk=did).first() if did else None)
    if key == "sric_office":
        return get_sric_recipient_emails()
    if key == "sric_bill_section":
        return get_sric_bill_section_emails()
    if key in {"ar_sric", "dean_sric", "project_grant_cc", "cash_deposit_cc"}:
        s = WalletSricSettings.get_singleton()
        attr = {
            "ar_sric": "ar_sric_emails",
            "dean_sric": "dean_sric_emails",
            "project_grant_cc": "project_grant_cc_emails",
            "cash_deposit_cc": "cash_deposit_cc_emails",
        }[key]
        return _parse_emails(getattr(s, attr, "") or "")
    if key == "dept_admin":
        return _user_emails(find_department_administrators(dept)) if dept else []
    if key == "dept_account_incharge":
        return _user_emails(find_department_account_incharges(dept))
    if key == "dept_oic":
        if not dept:
            return []
        return _user_emails(User.objects.filter(user_type=UserType.MANAGER, is_active=True, department_id=dept.pk))
    if key == "main_admin":
        return _user_emails(User.objects.filter(user_type=UserType.ADMIN, is_active=True))
    if key == "wallet_owner":
        return [wallet_owner.email] if getattr(wallet_owner, "email", None) else []
    return []


def unique_emails(emails: Iterable[str], *, exclude: Iterable[str] = ()) -> list[str]:
    seen = {(e or "").strip().lower() for e in exclude}
    out: list[str] = []
    for e in emails:
        value = (e or "").strip()
        if not value or "@" not in value or value.lower() in seen:
            continue
        seen.add(value.lower())
        out.append(value)
    return out


def expand_recipients(tokens: Iterable[str], *, department=None, wallet_owner=None) -> list[str]:
    out: list[str] = []
    for token in tokens or []:
        if token.startswith(ROLE_PREFIX):
            out.extend(_role_emails(token[len(ROLE_PREFIX):], department=department, wallet_owner=wallet_owner))
        else:
            out.append(token)
    return unique_emails(out)


def recipient_rows() -> list[WalletModeEmailRecipients]:
    return _safe(lambda: list(WalletModeEmailRecipients.objects.select_related("department", "updated_by")), [])


def configured_recipients(option: str, department=None) -> tuple[list[str], list[str], str]:
    """Raw To / CC tokens for ``option`` with their source: ``department``, ``default`` or ``builtin``."""
    did = _department_id(department)

    def load():
        qs = WalletModeEmailRecipients.objects.filter(option=option)
        qs = qs.filter(Q(department__isnull=True) | Q(department_id=did)) if did else qs.filter(department__isnull=True)
        return {r.department_id: (list(r.to_recipients or []), list(r.cc_recipients or [])) for r in qs}

    rows = _safe(load, {})
    if did and did in rows:
        return rows[did][0], rows[did][1], "department"
    if None in rows:
        return rows[None][0], rows[None][1], "default"
    to, cc = BUILTIN_RECIPIENTS.get(option, ([], []))
    return list(to), list(cc), "builtin"


@dataclass
class ResolvedRecipients:
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    configured_to: list[str] = field(default_factory=list)
    configured_cc: list[str] = field(default_factory=list)
    source: str = "builtin"


def resolve_recipients(
    option: str,
    *,
    department=None,
    requester=None,
    wallet_owner=None,
    fixed_to: Iterable[str] = (),
) -> ResolvedRecipients:
    """Configured To / CC for the department plus fixed people. The requester is always in CC."""
    to_tokens, cc_tokens, source = configured_recipients(option, department)
    configured_to = expand_recipients(to_tokens, department=department, wallet_owner=wallet_owner)
    configured_cc = expand_recipients(cc_tokens, department=department, wallet_owner=wallet_owner)
    to = unique_emails([*fixed_to, *configured_to])
    requester_email = getattr(requester, "email", "") or ""
    cc = unique_emails([requester_email, *configured_cc], exclude=to)
    return ResolvedRecipients(to=to, cc=cc, configured_to=configured_to, configured_cc=configured_cc, source=source)


def save_recipients(option: str, department_id, to: list[str], cc: list[str], *, actor, ip: str | None = None):
    from iic_booking.users.models.department import Department, DepartmentType

    did = _department_id(department_id)
    if did is not None and not Department.objects.filter(pk=did, department_type=DepartmentType.INTERNAL).exists():
        raise ValueError("Department was not found.")
    try:
        with transaction.atomic():
            row = WalletModeEmailRecipients.objects.select_for_update().filter(option=option, department_id=did).first()
            before = {"to": list(row.to_recipients), "cc": list(row.cc_recipients)} if row else {}
            if row is None:
                row = WalletModeEmailRecipients(option=option, department_id=did)
            row.to_recipients = to
            row.cc_recipients = cc
            row.updated_by = actor if getattr(actor, "is_authenticated", False) else None
            row.save()
            record_audit(actor, "recipients_saved", f"{option}:{did or 'default'}", before, {"to": to, "cc": cc}, ip)
            return row
    except SCHEMA_ERRORS as exc:
        raise SchemaPending(str(exc)) from exc


def reset_recipients(option: str, department_id, *, actor, ip: str | None = None) -> bool:
    did = _department_id(department_id)
    try:
        with transaction.atomic():
            deleted, _ = WalletModeEmailRecipients.objects.filter(option=option, department_id=did).delete()
    except SCHEMA_ERRORS as exc:
        raise SchemaPending(str(exc)) from exc
    if deleted:
        record_audit(actor, "recipients_reset", f"{option}:{did or 'default'}", {}, {}, ip)
    return bool(deleted)


def send_option_email(
    option: str,
    *,
    department,
    requester,
    subject: str,
    text_body: str,
    html_body: str = "",
    wallet_owner=None,
    fixed_to: Iterable[str] = (),
    only_when_configured: bool = True,
) -> ResolvedRecipients | None:
    """Send one email to the resolved To / CC. Test accounts' mail goes to the test inbox only."""
    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives

    from iic_booking.users.test_accounts import email_redirects, is_test_user

    resolved = resolve_recipients(
        option, department=department, requester=requester, wallet_owner=wallet_owner, fixed_to=fixed_to
    )
    if only_when_configured and not resolved.configured_to:
        return None
    to, cc = resolved.to, resolved.cc
    if not to and cc:
        to, cc = cc[:1], cc[1:]
    if not to:
        return None
    if is_test_user(requester) or is_test_user(wallet_owner):
        to, cc = email_redirects(), []
        if not to:
            return None
    message = EmailMultiAlternatives(
        subject=subject, body=text_body, from_email=settings.DEFAULT_FROM_EMAIL, to=to, cc=cc
    )
    if html_body:
        message.attach_alternative(html_body, "text/html")
    try:
        message.send(fail_silently=True)
    except Exception:  # noqa: BLE001
        logger.exception("wallet %s email failed", option)
    return ResolvedRecipients(to=to, cc=cc, source=resolved.source)


# ---------------------------------------------------------------------------
# Direct wallet recharge
# ---------------------------------------------------------------------------

DIRECT_RECHARGE_MAX_AMOUNT = Decimal("10000000.00")
REFERENCE_REQUIRED_MODES = frozenset(
    {WalletDirectRechargeMode.BANK_TRANSFER, WalletDirectRechargeMode.CHEQUE, WalletDirectRechargeMode.DEMAND_DRAFT}
)
ATTACHMENT_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg")
ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024


class DirectRechargeError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra or {}


def is_main_admin(user) -> bool:
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_superuser", False) or getattr(user, "user_type", None) == UserType.ADMIN)
    )


def valid_grants(user, at=None):
    at = at or timezone.now()
    return WalletDirectRechargeGrant.objects.filter(
        user=user, revoked_at__isnull=True, valid_from__lte=at, valid_until__gt=at
    ).select_related("department")


def grant_status(grant: WalletDirectRechargeGrant, at=None) -> str:
    at = at or timezone.now()
    if grant.revoked_at:
        return "revoked"
    if grant.valid_until <= at:
        return "expired"
    if grant.valid_from > at:
        return "scheduled"
    return "active"


def serialize_grant(grant: WalletDirectRechargeGrant) -> dict[str, Any]:
    from iic_booking.users.display import get_user_display_name

    u = grant.user
    return {
        "id": grant.pk,
        "user": {"id": u.pk, "name": get_user_display_name(u) or u.email, "email": u.email, "user_type": u.user_type},
        "valid_from": grant.valid_from.isoformat(),
        "valid_until": grant.valid_until.isoformat(),
        "department_id": grant.department_id,
        "department_name": grant.department.name if grant.department_id else None,
        "max_amount_per_transaction": str(grant.max_amount_per_transaction)
        if grant.max_amount_per_transaction is not None
        else None,
        "reason": grant.reason,
        "granted_by": getattr(grant.granted_by, "email", None),
        "created_at": grant.created_at.isoformat() if grant.created_at else None,
        "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
        "revoked_by": getattr(grant.revoked_by, "email", None),
        "revoke_reason": grant.revoke_reason,
        "status": grant_status(grant),
        "recharge_count": getattr(grant, "recharge_count", None),
    }


def direct_recharge_access(user) -> dict[str, Any]:
    enabled = direct_recharge_master_enabled()
    admin = is_main_admin(user)
    grants = _safe(lambda: list(valid_grants(user)), []) if user is not None and getattr(user, "is_authenticated", False) else []
    return {
        "enabled_globally": enabled,
        "is_main_admin": admin,
        "allowed": bool(enabled and (admin or grants)),
        "has_grant": bool(grants),
        "grants": [serialize_grant(g) for g in grants],
        "disabled_message": AWAITING_APPROVAL_MESSAGE,
        "modes": [{"value": v, "label": str(label)} for v, label in WalletDirectRechargeMode.choices],
        "reference_required_modes": sorted(REFERENCE_REQUIRED_MODES),
    }


def _money(value) -> Decimal:
    try:
        amount = Decimal(str(value).replace(",", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise DirectRechargeError("INVALID_AMOUNT", "Enter a valid amount.")
    if amount <= 0:
        raise DirectRechargeError("INVALID_AMOUNT", "Amount must be more than zero.")
    if amount > DIRECT_RECHARGE_MAX_AMOUNT:
        raise DirectRechargeError("INVALID_AMOUNT", f"Amount cannot exceed ₹{DIRECT_RECHARGE_MAX_AMOUNT:,.2f}.")
    return amount


def check_direct_recharge_permission(user, department, amount: Decimal, *, lock: bool = False):
    """Return the grant that authorises this recharge (``None`` for the Main Administrator)."""
    if not direct_recharge_master_enabled():
        raise DirectRechargeError(
            "DIRECT_RECHARGE_DISABLED", f"Direct wallet recharge: {AWAITING_APPROVAL_MESSAGE}", status=403
        )
    if not department_allows(WalletModeOption.DIRECT_RECHARGE, department):
        raise DirectRechargeError(
            "DIRECT_RECHARGE_DEPARTMENT_DISABLED",
            f"Direct wallet recharge for this department: {AWAITING_APPROVAL_MESSAGE}",
            status=403,
        )
    if is_main_admin(user):
        return None
    did = _department_id(department)
    grants = valid_grants(user).filter(Q(department__isnull=True) | Q(department_id=did)).order_by("valid_until", "pk")
    if lock:
        grants = grants.select_for_update()
    grants = list(grants)
    if not grants:
        had_any = WalletDirectRechargeGrant.objects.filter(user=user).exists()
        raise DirectRechargeError(
            "NOT_AUTHORISED",
            "Your permission for direct wallet recharge has expired or does not cover this department."
            if had_any
            else "Only the Main Administrator or a designated person can recharge wallets directly.",
            status=403,
        )
    fitting = [g for g in grants if g.max_amount_per_transaction is None or amount <= g.max_amount_per_transaction]
    if not fitting:
        cap = max(g.max_amount_per_transaction for g in grants)
        raise DirectRechargeError(
            "OVER_GRANT_LIMIT",
            f"Your permission allows at most ₹{cap:,.2f} per transaction.",
            status=403,
            extra={"max_amount_per_transaction": str(cap)},
        )
    # Department-scoped grants are preferred over all-department ones.
    fitting.sort(key=lambda g: (g.department_id is None, g.valid_until))
    return fitting[0]


def _resolve_target(owner_id, department_id):
    from django.contrib.auth import get_user_model

    from iic_booking.users.models.department import Department, DepartmentType
    from iic_booking.users.models.wallet import SubWallet, Wallet

    User = get_user_model()
    owner = User.objects.filter(pk=_department_id(owner_id)).first()
    if owner is None:
        raise DirectRechargeError("WALLET_NOT_FOUND", "Select the wallet to recharge.", status=404)
    department = Department.objects.filter(pk=_department_id(department_id), department_type=DepartmentType.INTERNAL).first()
    if department is None:
        raise DirectRechargeError("DEPARTMENT_NOT_FOUND", "Select the department sub-wallet to recharge.", status=404)
    wallet = Wallet.objects.filter(user=owner).first()
    if wallet is None and not owner.can_have_wallet():
        raise DirectRechargeError(
            "NO_WALLET", "This user cannot hold a wallet of their own (students use their faculty member's wallet)."
        )
    sub = SubWallet.objects.filter(wallet=wallet, department=department).first() if wallet else None
    return owner, department, wallet, sub


def wallet_owner_summary(owner) -> dict[str, Any]:
    from iic_booking.users.display import get_user_display_name

    dept = getattr(owner, "department", None)
    return {
        "id": owner.pk,
        "name": get_user_display_name(owner) or owner.email,
        "email": owner.email,
        "user_type": owner.user_type,
        "employee_id": getattr(owner, "emp_id", "") or "",
        "department_name": getattr(dept, "name", "") if dept else "",
    }


def validate_direct_recharge_input(data: dict[str, Any], attachment=None) -> dict[str, Any]:
    from datetime import date

    amount = _money(data.get("amount"))
    mode = str(data.get("mode") or "").strip()
    if mode not in WalletDirectRechargeMode.values:
        raise DirectRechargeError("INVALID_MODE", "Select how the funds were received.")
    reference = str(data.get("reference_number") or "").strip()[:120]
    if mode in REFERENCE_REQUIRED_MODES and not reference:
        raise DirectRechargeError("REFERENCE_REQUIRED", "Enter the reference / transaction number.")
    raw_date = str(data.get("transaction_date") or "").strip()
    try:
        txn_date = date.fromisoformat(raw_date)
    except ValueError:
        raise DirectRechargeError("INVALID_DATE", "Enter the transaction date.")
    if txn_date > timezone.localdate():
        raise DirectRechargeError("INVALID_DATE", "The transaction date cannot be in the future.")
    remarks = str(data.get("remarks") or "").strip()
    if len(remarks) < 3:
        raise DirectRechargeError("REMARKS_REQUIRED", "Remarks are required.")
    if attachment is not None:
        name = (getattr(attachment, "name", "") or "").lower()
        if not name.endswith(ATTACHMENT_EXTENSIONS):
            raise DirectRechargeError("INVALID_ATTACHMENT", "Attach a PDF, PNG or JPG file.")
        if (getattr(attachment, "size", 0) or 0) > ATTACHMENT_MAX_BYTES:
            raise DirectRechargeError("INVALID_ATTACHMENT", "The attachment must be 5 MB or smaller.")
    return {
        "amount": amount,
        "mode": mode,
        "reference_number": reference,
        "transaction_date": txn_date,
        "remarks": remarks[:2000],
    }


def preview_direct_recharge(user, data: dict[str, Any]) -> dict[str, Any]:
    amount = _money(data.get("amount"))
    owner, department, wallet, sub = _resolve_target(data.get("owner_id"), data.get("department_id"))
    grant = check_direct_recharge_permission(user, department, amount)
    before = Decimal(sub.balance) if sub else Decimal("0.00")
    recipients = resolve_recipients(
        WalletModeOption.DIRECT_RECHARGE,
        department=department,
        requester=user,
        wallet_owner=owner,
        fixed_to=[owner.email] if owner.email else [],
    )
    return {
        "owner": wallet_owner_summary(owner),
        "department": {"id": department.pk, "name": department.name, "code": department.code or ""},
        "sub_wallet_exists": sub is not None,
        "balance_before": str(before),
        "amount": str(amount),
        "balance_after": str(before + amount),
        "grant_id": grant.pk if grant else None,
        "performed_as": "main_admin" if grant is None else "designated_person",
        "email_to": recipients.to,
        "email_cc": recipients.cc,
    }


def perform_direct_recharge(
    *,
    actor,
    data: dict[str, Any],
    attachment=None,
    ip: str | None = None,
    user_agent: str = "",
) -> tuple[WalletDirectRecharge, bool]:
    """Credit the sub-wallet once per ``client_request_id``. Returns ``(record, created)``."""
    from iic_booking.users.models.wallet import SubWallet, Wallet

    client_request_id = str(data.get("client_request_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,64}", client_request_id):
        raise DirectRechargeError("CLIENT_REQUEST_ID_REQUIRED", "A client request id (8–64 characters) is required.")
    clean = validate_direct_recharge_input(data, attachment)

    def replay(existing: WalletDirectRecharge):
        same = (
            existing.performed_by_id == actor.pk
            and existing.amount == clean["amount"]
            and existing.department_id == _department_id(data.get("department_id"))
            and existing.wallet.user_id == _department_id(data.get("owner_id"))
        )
        if not same:
            raise DirectRechargeError(
                "DUPLICATE_REQUEST_ID", "This request id was already used for a different recharge.", status=409
            )
        return existing, False

    try:
        existing = WalletDirectRecharge.objects.select_related("wallet").filter(client_request_id=client_request_id).first()
    except SCHEMA_ERRORS as exc:
        raise SchemaPending(str(exc)) from exc
    if existing:
        return replay(existing)

    owner, department, wallet, _ = _resolve_target(data.get("owner_id"), data.get("department_id"))
    try:
        with transaction.atomic():
            grant = check_direct_recharge_permission(actor, department, clean["amount"], lock=True)
            if wallet is None:
                wallet, _ = Wallet.objects.get_or_create(user=owner)
            sub, _ = SubWallet.objects.get_or_create(wallet=wallet, department=department, defaults={"balance": Decimal("0.00")})
            sub = SubWallet.objects.select_for_update().get(pk=sub.pk)
            before = Decimal(sub.balance)
            mode_label = WalletDirectRechargeMode(clean["mode"]).label
            txn = sub.credit(clean["amount"], f"Direct wallet recharge ({mode_label})", related_user=owner)
            record = WalletDirectRecharge.objects.create(
                client_request_id=client_request_id,
                wallet=wallet,
                sub_wallet=sub,
                department=department,
                amount=clean["amount"],
                mode=clean["mode"],
                reference_number=clean["reference_number"],
                transaction_date=clean["transaction_date"],
                remarks=clean["remarks"],
                attachment=attachment,
                balance_before=before,
                balance_after=Decimal(sub.balance),
                sub_wallet_transaction=txn,
                performed_by=actor,
                performed_as="main_admin" if grant is None else "designated_person",
                grant=grant,
                ip_address=ip or None,
                user_agent=(user_agent or "")[:255],
            )
            record.reference = f"DWR-{timezone.now().year}-{record.pk:06d}"
            record.save(update_fields=["reference"])
            description = f"Direct wallet recharge {record.reference} — {mode_label}"
            if clean["reference_number"]:
                description += f", Ref: {clean['reference_number']}"
            description += f"; by {actor.email}"
            type(txn).objects.filter(pk=txn.pk).update(description=description)
            record_audit(
                actor,
                "direct_recharge_performed",
                f"direct_recharge:{record.pk}",
                {"balance": str(before)},
                {
                    "reference": record.reference,
                    "amount": str(record.amount),
                    "wallet_owner_id": owner.pk,
                    "department_id": department.pk,
                    "balance": str(record.balance_after),
                    "grant_id": grant.pk if grant else None,
                    "performed_as": record.performed_as,
                },
                ip,
            )
            transaction.on_commit(lambda: notify_direct_recharge(record.pk))
    except IntegrityError:
        existing = WalletDirectRecharge.objects.select_related("wallet").filter(client_request_id=client_request_id).first()
        if existing is None:
            raise
        return replay(existing)
    except SCHEMA_ERRORS as exc:
        raise SchemaPending(str(exc)) from exc
    return record, True


def serialize_direct_recharge(r: WalletDirectRecharge) -> dict[str, Any]:
    owner = r.wallet.user
    attachment_url = None
    try:
        if r.attachment:
            attachment_url = r.attachment.url
    except Exception:  # noqa: BLE001
        attachment_url = None
    return {
        "id": r.pk,
        "reference": r.reference,
        "client_request_id": r.client_request_id,
        "owner": wallet_owner_summary(owner),
        "department_id": r.department_id,
        "department_name": r.department.name if r.department_id else "",
        "amount": str(r.amount),
        "mode": r.mode,
        "mode_label": WalletDirectRechargeMode(r.mode).label if r.mode in WalletDirectRechargeMode.values else r.mode,
        "reference_number": r.reference_number,
        "transaction_date": r.transaction_date.isoformat(),
        "remarks": r.remarks,
        "attachment_url": attachment_url,
        "balance_before": str(r.balance_before),
        "balance_after": str(r.balance_after),
        "sub_wallet_transaction_id": r.sub_wallet_transaction_id,
        "performed_by": {"id": r.performed_by_id, "email": getattr(r.performed_by, "email", "")},
        "performed_as": r.performed_as,
        "grant_id": r.grant_id,
        "ip_address": r.ip_address,
        "email_to": r.email_to,
        "email_cc": r.email_cc,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def notify_direct_recharge(record_id: int) -> None:
    from html import escape

    from iic_booking.users.display import get_user_display_name

    try:
        r = WalletDirectRecharge.objects.select_related("wallet__user", "department", "performed_by").get(pk=record_id)
    except WalletDirectRecharge.DoesNotExist:
        return
    owner = r.wallet.user
    mode_label = WalletDirectRechargeMode(r.mode).label
    rows = [
        ("Reference", r.reference),
        ("Wallet", f"{get_user_display_name(owner) or owner.email} ({owner.email})"),
        ("Department sub-wallet", r.department.name),
        ("Amount credited", f"₹{r.amount:,.2f}"),
        ("Mode", str(mode_label)),
        ("Reference / transaction no.", r.reference_number or "—"),
        ("Transaction date", r.transaction_date.strftime("%d %b %Y")),
        ("Remarks", r.remarks),
        ("New sub-wallet balance", f"₹{r.balance_after:,.2f}"),
        ("Recharged by", r.performed_by.email),
    ]
    text = "Funds have been added to your wallet.\n\n" + "\n".join(f"{k}: {v}" for k, v in rows)
    html = (
        "<p>Funds have been added to your wallet.</p><table cellpadding='4'>"
        + "".join(f"<tr><td><strong>{escape(k)}</strong></td><td>{escape(str(v))}</td></tr>" for k, v in rows)
        + "</table>"
    )
    sent = send_option_email(
        WalletModeOption.DIRECT_RECHARGE,
        department=r.department,
        requester=r.performed_by,
        wallet_owner=owner,
        fixed_to=[owner.email] if owner.email else [],
        subject=f"[{r.reference}] ₹{r.amount:,.2f} added to your {r.department.name} wallet",
        text_body=text,
        html_body=html,
        only_when_configured=False,
    )
    if sent:
        WalletDirectRecharge.objects.filter(pk=r.pk).update(email_to=sent.to, email_cc=sent.cc)
    try:
        from iic_booking.communication.in_app import notify_in_app

        notify_in_app(
            [owner],
            title="Funds added to your wallet",
            message=f"₹{r.amount:,.2f} was added to your {r.department.name} sub-wallet ({r.reference}).",
            link="/wallet",
            notification_type="success",
            event="wallet.direct_recharge",
            created_by=r.performed_by,
            extra={"direct_recharge_id": r.pk},
        )
    except Exception:  # noqa: BLE001
        logger.debug("direct recharge in-app notification skipped", exc_info=True)


def notify_grantee(grant: WalletDirectRechargeGrant) -> None:
    try:
        from iic_booking.communication.in_app import notify_in_app

        scope = grant.department.name if grant.department_id else "all departments"
        until = timezone.localtime(grant.valid_until).strftime("%d %b %Y, %I:%M %p")
        notify_in_app(
            [grant.user],
            title="You can recharge wallets directly",
            message=f"The Main Administrator allowed you to recharge wallets ({scope}) until {until}.",
            link="/wallet/direct-recharge",
            notification_type="info",
            event="wallet.direct_recharge_grant",
            created_by=grant.granted_by,
            extra={"direct_recharge_grant_id": grant.pk},
        )
    except Exception:  # noqa: BLE001
        logger.debug("direct recharge grant notification skipped", exc_info=True)


def search_wallet_owners(query: str, limit: int = 20) -> list[dict[str, Any]]:
    from django.contrib.auth import get_user_model

    from iic_booking.users.models.wallet import SubWallet, Wallet

    User = get_user_model()
    q = (query or "").strip()
    if len(q) < 2:
        return []
    users = (
        User.objects.filter(is_active=True)
        .filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(emp_id__icontains=q))
        .filter(Q(wallet__isnull=False) | Q(user_type__in=UserType.get_wallet_eligible_codes()))
        .select_related("department")
        .order_by("name")[:limit]
    )
    users = list(users)
    wallets = {w.user_id: w for w in Wallet.objects.filter(user__in=users)}
    subs: dict[int, list[dict[str, Any]]] = {}
    for sw in SubWallet.objects.filter(wallet__in=wallets.values()).select_related("department").order_by("department__name"):
        subs.setdefault(sw.wallet.user_id, []).append(
            {"department_id": sw.department_id, "department_name": sw.department.name, "balance": str(sw.balance)}
        )
    return [{**wallet_owner_summary(u), "has_wallet": u.pk in wallets, "sub_wallets": subs.get(u.pk, [])} for u in users]
