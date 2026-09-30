"""Main Administrator: map a new-portal user to an old-portal ``users.id`` and sync wallet + legacy bookings.

The old MySQL database is only ever read (OldMySQLReader refuses write SQL).

Wallet: the legacy wallet balance is reconciled into the IIC department sub-wallet of the
target wallet with marker-based deltas, so re-running a sync never double-credits.
IITR Students cannot own a wallet on the new portal; like their wallet recharges, their
legacy balance is credited to the linked faculty wallet and attributed to the student.

Bookings: legacy bookings are archived to LegacyBookingHistoryRecord (never equipment.Booking),
and ACTIVE LegacyBookingBlock rows for the legacy user are linked to the new user.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from iic_booking.users.legacy_ledger.equipment_mapping import get_active_mapping_for_old_id
from iic_booking.users.legacy_ledger.faculty_login_wallet_sync import faculty_login_migration_id
from iic_booking.users.legacy_ledger.importer import import_transaction
from iic_booking.users.legacy_ledger.legacy_equipment_inventory import _legacy_equipment_table
from iic_booking.users.legacy_ledger.mapping import exact_employee_id
from iic_booking.users.legacy_ledger.opening_balance import (
    ADMIN_SYNC_PREFIX,
    LEGACY_CREDIT_PREFIXES,
    OpeningBalanceError,
    get_iic_department,
    net_credited_for_migration,
    reconcile_legacy_balance_to_subwallet,
)
from iic_booking.users.legacy_ledger.reader import OldMySQLReader
from iic_booking.users.models import SubWallet, SubWalletTransaction, User, UserType, Wallet
from iic_booking.users.models.portal_migration import (
    LegacyBookingBlock,
    LegacyBookingBlockStatus,
    LegacyBookingHistoryRecord,
    LegacyUserMappingStatus,
    LegacyWalletAccountMapping,
    LegacyWalletMappingStatus,
)
from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

ADMIN_BATCH_PREFIX = "admin-map"
ADMIN_MAPPING_SOURCE = "admin_legacy_user_mapping"
LEGACY_UID_KEY_PREFIX = "LEGACY-UID:"
TARGET_OWN = "own"
TARGET_LINKED_FACULTY = "linked_faculty"
MAX_LEGACY_BOOKINGS = 2000
BOOKINGS_IN_PREVIEW = 200

SENSITIVE_COLUMN_TOKENS = ("pass", "token", "otp", "secret", "hash", "salt", "session", "api_key", "remember")


class AdminSyncError(Exception):
    """User-facing error for the admin legacy user sync."""


def _money(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _json_safe(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def _is_sensitive_column(name: str) -> bool:
    low = (name or "").lower()
    return any(tok in low for tok in SENSITIVE_COLUMN_TOKENS)


def public_legacy_row(row: dict | None) -> dict:
    """JSON-safe copy of a legacy row without credential-like columns."""
    return {k: _json_safe(v) for k, v in (row or {}).items() if not _is_sensitive_column(k)}


def _first_present(row: dict, *names: str):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


# ---------------------------------------------------------------------------
# New-portal user details
# ---------------------------------------------------------------------------


def _linked_faculty_request(user: User):
    if user.user_type not in {UserType.STUDENT, UserType.OTHER}:
        return None
    return (
        WalletJoinRequest.objects.filter(student=user, status=WalletJoinRequestStatus.APPROVED)
        .select_related("wallet", "wallet__user")
        .first()
    )


def _own_wallet(user: User) -> Wallet | None:
    try:
        return user.wallet
    except Wallet.DoesNotExist:
        return None


def _wallet_brief(wallet: Wallet | None) -> dict | None:
    if wallet is None:
        return None
    owner = wallet.user
    return {
        "wallet_id": wallet.pk,
        "owner_id": owner.pk,
        "owner_name": owner.name or owner.email,
        "owner_email": owner.email,
        "total_balance": str(wallet.total_balance),
        "sub_wallets": [
            {"department": sw.department.name, "balance": str(sw.balance)}
            for sw in wallet.sub_wallets.select_related("department")
        ],
    }


def search_new_users(query: str, limit: int = 15) -> list[dict]:
    q = (query or "").strip()
    if len(q) < 2:
        return []
    qs = (
        User.objects.filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(emp_id__icontains=q))
        .select_related("department")
        .order_by("name", "email")[: max(1, min(limit, 50))]
    )
    results = []
    for u in qs:
        results.append(
            {
                "id": u.pk,
                "name": u.name or u.email,
                "email": u.email,
                "phone": u.phone_number,
                "emp_id": u.emp_id,
                "user_type": u.user_type,
                "user_type_display": u.get_user_type_display_label(),
                "department": u.department.name if u.department_id else None,
                "profile_picture": u.get_profile_picture_url_or_none(),
                "is_active": u.is_active,
            }
        )
    return results


def serialize_new_user(user: User) -> dict:
    own = _own_wallet(user) if user.can_have_wallet() else None
    join = _linked_faculty_request(user)
    supervisor = user.supervisor if user.supervisor_id else None
    mappings = [
        {
            "employee_id": m.employee_id,
            "old_user_id": m.old_user_id,
            "old_name": m.old_name,
            "old_email": m.old_email,
            "mapping_status": m.mapping_status,
            "old_wallet_balance": str(m.old_wallet_balance) if m.old_wallet_balance is not None else None,
            "migration_batch": m.migration_batch,
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        }
        for m in LegacyWalletAccountMapping.objects.filter(new_user=user).order_by("-updated_at")
    ]
    return {
        "id": user.pk,
        "name": user.name or user.email,
        "email": user.email,
        "phone": user.phone_number,
        "secondary_phone": user.secondary_phone_number,
        "emp_id": user.emp_id,
        "user_type": user.user_type,
        "user_type_display": user.get_user_type_display_label(),
        "department": user.department.name if user.department_id else None,
        "designation": user.designation,
        "degree_name": user.degree_name,
        "branch_name": user.branch_name,
        "joining_date": user.joining_date.isoformat() if user.joining_date else None,
        "graduation_date": user.graduation_date.isoformat() if user.graduation_date else None,
        "profile_picture": user.get_profile_picture_url_or_none(),
        "is_active": user.is_active,
        "date_joined": user.date_joined.isoformat() if user.date_joined else None,
        "last_login": user.last_login.isoformat() if user.last_login else None,
        "supervisor": (
            {"id": supervisor.pk, "name": supervisor.name or supervisor.email, "email": supervisor.email}
            if supervisor
            else None
        ),
        "can_have_own_wallet": user.can_have_wallet(),
        "own_wallet": _wallet_brief(own),
        "linked_faculty_wallet": _wallet_brief(join.wallet) if join and join.wallet_id else None,
        "legacy_mappings": mappings,
        "legacy_bookings_synced": LegacyBookingHistoryRecord.objects.filter(payload__new_user_id=user.pk).count(),
    }


# ---------------------------------------------------------------------------
# Legacy (old portal) reads
# ---------------------------------------------------------------------------


def legacy_candidates_for_user(user: User, reader: OldMySQLReader) -> list[dict]:
    """Old-portal accounts with the same employee/student ID or email (suggestions only)."""
    emp = exact_employee_id(user.emp_id)
    email = (user.email or "").strip().lower()
    clauses, params = [], []
    if emp:
        clauses.append("TRIM(emp_id) = %s")
        params.append(emp)
    if email:
        clauses.append("LOWER(TRIM(email)) = %s")
        params.append(email)
    if not clauses:
        return []
    rows = reader.fetchall(
        f"SELECT id, emp_id, name, email FROM users WHERE {' OR '.join(clauses)} ORDER BY id LIMIT 10",
        tuple(params),
    )
    out = []
    for r in rows:
        wallet = reader.wallet_for_user(int(r["id"]))
        matched_on = []
        if emp and exact_employee_id(r.get("emp_id")) == emp:
            matched_on.append("emp_id")
        if email and str(r.get("email") or "").strip().lower() == email:
            matched_on.append("email")
        out.append(
            {
                "legacy_user_id": int(r["id"]),
                "emp_id": exact_employee_id(r.get("emp_id")),
                "name": str(r.get("name") or ""),
                "email": str(r.get("email") or ""),
                "wallet_balance": _json_safe(wallet.get("balance")) if wallet else None,
                "matched_on": matched_on,
            }
        )
    return out


def _legacy_wallet_snapshot(reader: OldMySQLReader, legacy_uid: int) -> dict:
    wallet = reader.wallet_for_user(legacy_uid)
    credits, debits = reader.user_ledger_totals(legacy_uid)
    ledger_balance = (credits - debits).quantize(Decimal("0.01"))
    wallet_balance = _money(wallet.get("balance")) if wallet else None
    count_row = reader.fetchone(
        "SELECT COUNT(*) AS n FROM wallet_transactions WHERE user_id = %s", (legacy_uid,)
    )
    recent = reader.fetchall(
        "SELECT id, amount, balance, transaction_type, create_date, description "
        "FROM wallet_transactions WHERE user_id = %s ORDER BY id DESC LIMIT 10",
        (legacy_uid,),
    )
    return {
        "has_wallet": wallet is not None,
        "wallet_id": int(wallet["id"]) if wallet else None,
        "balance": str(wallet_balance if wallet_balance is not None else ledger_balance),
        "balance_source": "user_wallet.balance" if wallet_balance is not None else "ledger (credits - debits)",
        "wallet_balance": str(wallet_balance) if wallet_balance is not None else None,
        "ledger_balance": str(ledger_balance),
        "total_credits": str(credits.quantize(Decimal("0.01"))),
        "total_debits": str(debits.quantize(Decimal("0.01"))),
        "transaction_count": int((count_row or {}).get("n") or 0),
        "recent_transactions": [
            {
                "id": int(t["id"]),
                "type": "credit" if str(t.get("transaction_type")) == "1" else "debit" if str(t.get("transaction_type")) == "2" else str(t.get("transaction_type")),
                "amount": _json_safe(t.get("amount")),
                "running_balance": _json_safe(t.get("balance")),
                "date": _json_safe(t.get("create_date")),
                "description": str(t.get("description") or "")[:300],
            }
            for t in recent
        ],
    }


def _legacy_equipment_names(reader: OldMySQLReader, ids: set[int]) -> dict[int, dict]:
    if not ids:
        return {}
    table, _ = _legacy_equipment_table(reader)
    if not table:
        return {}
    cols = {c["Field"] for c in reader.fetchall(f"SHOW COLUMNS FROM `{table}`")}
    if "id" not in cols:
        return {}
    name_col = next((c for c in ("name", "equipment_name", "title", "equipment_title") if c in cols), None)
    code_col = next((c for c in ("code", "equipment_code", "short_name", "eq_code") if c in cols), None)
    select = ["`id` AS legacy_id"]
    select.append(f"`{name_col}` AS legacy_name" if name_col else "NULL AS legacy_name")
    select.append(f"`{code_col}` AS legacy_code" if code_col else "NULL AS legacy_code")
    ordered = sorted(ids)
    placeholders = ",".join(["%s"] * len(ordered))
    rows = reader.fetchall(
        f"SELECT {', '.join(select)} FROM `{table}` WHERE `id` IN ({placeholders})", tuple(ordered)
    )
    return {
        int(r["legacy_id"]): {"name": str(r.get("legacy_name") or ""), "code": str(r.get("legacy_code") or "")}
        for r in rows
    }


def fetch_legacy_bookings_for_user(reader: OldMySQLReader, legacy_uid: int) -> dict:
    tables = {next(iter(t.values())) for t in reader.fetchall("SHOW TABLES")}
    if "booking" not in tables:
        return {"ok": False, "error": "Legacy booking table not found.", "rows": [], "raw_rows": [], "truncated": False}
    cols = {c["Field"] for c in reader.fetchall("SHOW COLUMNS FROM `booking`")}
    user_col = next((c for c in ("user_id", "uid") if c in cols), None)
    if not user_col:
        return {"ok": False, "error": "Legacy booking table has no user column.", "rows": [], "raw_rows": [], "truncated": False}
    order_col = "id" if "id" in cols else user_col
    raw = reader.fetchall(
        f"SELECT * FROM `booking` WHERE `{user_col}` = %s ORDER BY `{order_col}` DESC LIMIT %s",
        (legacy_uid, MAX_LEGACY_BOOKINGS + 1),
    )
    truncated = len(raw) > MAX_LEGACY_BOOKINGS
    raw = raw[:MAX_LEGACY_BOOKINGS]

    eq_ids = set()
    for r in raw:
        eid = _first_present(r, "equipment_id", "instrument_id", "eq_id", "inst_id")
        try:
            if eid is not None:
                eq_ids.add(int(eid))
        except (TypeError, ValueError):
            pass
    try:
        eq_names = _legacy_equipment_names(reader, eq_ids)
    except Exception:  # noqa: BLE001 — names are cosmetic
        eq_names = {}

    rows = []
    for r in raw:
        booking_id = _first_present(r, "id", "booking_id")
        if booking_id is None:
            continue
        eid_raw = _first_present(r, "equipment_id", "instrument_id", "eq_id", "inst_id")
        try:
            eid = int(eid_raw) if eid_raw is not None else None
        except (TypeError, ValueError):
            eid = None
        mapping = get_active_mapping_for_old_id(eid) if eid is not None else None
        new_eq = mapping.new_equipment if mapping and mapping.new_equipment_id else None
        duration = _first_present(r, "time_required", "duration", "duration_minutes")
        try:
            duration = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        rows.append(
            {
                "legacy_booking_id": int(booking_id),
                "legacy_equipment_id": eid,
                "legacy_equipment_name": (eq_names.get(eid) or {}).get("name", "") if eid is not None else "",
                "legacy_equipment_code": (eq_names.get(eid) or {}).get("code", "") if eid is not None else "",
                "new_equipment": (
                    {"id": new_eq.pk, "code": new_eq.code, "name": new_eq.name} if new_eq is not None else None
                ),
                "booking_date": _json_safe(_first_present(r, "booking_date", "date", "start_at", "start_datetime")),
                "duration_minutes": duration,
                "status": _json_safe(_first_present(r, "status", "booking_status", "state")),
                "charge": _json_safe(_first_present(r, "charge", "amount", "total_charge", "booking_amount")),
                "is_deleted": _json_safe(r.get("is_deleted")),
                "is_active": _json_safe(r.get("is_active")),
                "created_at": _json_safe(_first_present(r, "create_date", "created_at", "created_on")),
            }
        )
    return {"ok": True, "rows": rows, "raw_rows": raw, "truncated": truncated}


# ---------------------------------------------------------------------------
# Wallet target + migration id
# ---------------------------------------------------------------------------


def wallet_target_options(user: User) -> list[dict]:
    options = []
    if user.can_have_wallet():
        own = _own_wallet(user)
        options.append(
            {
                "key": TARGET_OWN,
                "label": "User's own wallet",
                "wallet_id": own.pk if own else None,
                "owner_name": user.name or user.email,
                "owner_email": user.email,
                "will_create_wallet": own is None,
            }
        )
    join = _linked_faculty_request(user)
    if join and join.wallet_id:
        owner = join.wallet.user
        options.append(
            {
                "key": TARGET_LINKED_FACULTY,
                "label": f"Linked faculty wallet ({owner.name or owner.email})",
                "wallet_id": join.wallet_id,
                "owner_name": owner.name or owner.email,
                "owner_email": owner.email,
                "will_create_wallet": False,
            }
        )
    return options


def _default_target_key(user: User, options: list[dict]) -> str | None:
    keys = [o["key"] for o in options]
    if user.user_type in {UserType.STUDENT, UserType.OTHER} and TARGET_LINKED_FACULTY in keys:
        return TARGET_LINKED_FACULTY
    return keys[0] if keys else None


def migration_id_for(user: User, legacy_user: dict) -> str:
    """Faculty whose legacy emp_id matches share the login-sync id, so both syncs stay idempotent."""
    legacy_uid = int(legacy_user["id"])
    emp_old = exact_employee_id(legacy_user.get("emp_id"))
    if user.user_type == UserType.FACULTY and emp_old and emp_old == exact_employee_id(user.emp_id):
        return faculty_login_migration_id(user_id=user.pk, employee_id=emp_old)
    return f"legacy-user:{legacy_uid}"


def _existing_legacy_credits(user: User) -> list[dict]:
    """Legacy-migration credits already on wallets this user owns or is attributed to."""
    qs = (
        SubWalletTransaction.objects.filter(Q(sub_wallet__wallet__user=user) | Q(related_user=user))
        .filter(
            Q(description__startswith=LEGACY_CREDIT_PREFIXES[0])
            | Q(description__startswith=LEGACY_CREDIT_PREFIXES[1])
            | Q(description__startswith=LEGACY_CREDIT_PREFIXES[2])
        )
        .select_related("sub_wallet__wallet__user", "sub_wallet__department")
        .order_by("-created_at")[:50]
    )
    return [
        {
            "id": t.pk,
            "type": t.transaction_type,
            "amount": str(t.amount),
            "wallet_owner": t.sub_wallet.wallet.user.email,
            "department": t.sub_wallet.department.name,
            "description": (t.description or "")[:200],
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in qs
    ]


def _mapping_key(reader: OldMySQLReader, legacy_user: dict) -> str:
    emp = exact_employee_id(legacy_user.get("emp_id"))
    if emp:
        row = reader.fetchone("SELECT COUNT(*) AS n FROM users WHERE TRIM(emp_id) = %s", (emp,))
        if int((row or {}).get("n") or 0) == 1:
            return emp
    return f"{LEGACY_UID_KEY_PREFIX}{int(legacy_user['id'])}"


# ---------------------------------------------------------------------------
# Preview (test sync) and confirm
# ---------------------------------------------------------------------------


def build_sync_preview(
    user: User,
    legacy_uid: int,
    *,
    wallet_target: str | None = None,
    reader: OldMySQLReader,
) -> dict[str, Any]:
    """Read-only test sync: legacy balance, target wallet, delta, and legacy bookings."""
    blockers: list[str] = []
    warnings: list[str] = []

    legacy_user = reader.fetchone("SELECT * FROM users WHERE id = %s LIMIT 1", (int(legacy_uid),))
    if not legacy_user:
        raise AdminSyncError(f"No user with ID {legacy_uid} in the old booking database.")
    legacy_uid = int(legacy_user["id"])
    legacy_emp = exact_employee_id(legacy_user.get("emp_id"))

    other = (
        LegacyWalletAccountMapping.objects.filter(old_user_id=legacy_uid)
        .exclude(new_user__isnull=True)
        .exclude(new_user=user)
        .select_related("new_user")
        .first()
    )
    if other:
        blockers.append(
            f"Old-portal user {legacy_uid} is already mapped to {other.new_user.email}. "
            "Remove that mapping before mapping it to this user."
        )
    if legacy_emp and user.emp_id and legacy_emp != exact_employee_id(user.emp_id):
        warnings.append(
            f"Employee/student ID differs: old portal {legacy_emp}, new portal {user.emp_id}. "
            "Confirm this is the same person."
        )
    legacy_email = str(legacy_user.get("email") or "").strip().lower()
    if legacy_email and legacy_email != (user.email or "").strip().lower():
        warnings.append(f"Email differs: old portal {legacy_user.get('email')}, new portal {user.email}.")

    wallet = _legacy_wallet_snapshot(reader, legacy_uid)
    legacy_balance = Decimal(wallet["balance"])

    options = wallet_target_options(user)
    selected = wallet_target or _default_target_key(user, options)
    option = next((o for o in options if o["key"] == selected), None)
    migration_id = migration_id_for(user, legacy_user)

    wallet_reasons: list[str] = []
    if not options:
        wallet_reasons.append(
            "This user has no wallet to receive the balance. An IITR Student must first link to a faculty wallet."
        )
    elif option is None:
        wallet_reasons.append("Choose a valid target wallet.")
    if legacy_balance < 0:
        wallet_reasons.append(f"Old-portal balance is negative (₹{legacy_balance}); it cannot be synced.")

    dept = None
    try:
        dept = get_iic_department()
    except OpeningBalanceError as exc:
        wallet_reasons.append(str(exc))

    previously_credited = Decimal("0.00")
    sub_balance = Decimal("0.00")
    target_wallet_id = option["wallet_id"] if option else None
    if dept is not None and target_wallet_id:
        sub = SubWallet.objects.filter(wallet_id=target_wallet_id, department=dept).first()
        if sub is not None:
            previously_credited = net_credited_for_migration(migration_id=migration_id, sub_wallet=sub)
            sub_balance = Decimal(str(sub.balance or 0)).quantize(Decimal("0.01"))
    credited_anywhere = net_credited_for_migration(migration_id=migration_id)
    credited_elsewhere = (credited_anywhere - previously_credited).quantize(Decimal("0.01"))
    if credited_elsewhere != 0:
        wallet_reasons.append(
            f"₹{credited_elsewhere} from this old-portal wallet was already synced to a different wallet. "
            "Reverse it there before syncing to this wallet."
        )
    delta = (legacy_balance - previously_credited).quantize(Decimal("0.01")) if legacy_balance >= 0 else Decimal("0.00")
    resulting = (sub_balance + delta).quantize(Decimal("0.01"))
    if delta < 0 and resulting < 0:
        warnings.append(
            f"Old-portal balance fell since the last sync; the debit of ₹{abs(delta)} would leave the "
            f"IIC sub-wallet at ₹{resulting}."
        )

    existing_credits = _existing_legacy_credits(user)
    unrelated = [c for c in existing_credits if f"migration_id={migration_id} |" not in c["description"]]
    if unrelated:
        warnings.append(
            f"{len(unrelated)} other legacy-migration transaction(s) already exist for this user "
            "(see list). Check they are not the same old-portal money."
        )

    bookings = fetch_legacy_bookings_for_user(reader, legacy_uid)
    booking_rows = bookings["rows"]
    ids = [r["legacy_booking_id"] for r in booking_rows]
    archived = LegacyBookingHistoryRecord.objects.filter(source_booking_id__in=ids)
    archived_here = sum(1 for rec in archived if (rec.payload or {}).get("new_user_id") == user.pk)
    archived_other = sum(
        1 for rec in archived if (rec.payload or {}).get("new_user_id") not in (None, user.pk)
    )
    if archived_other:
        warnings.append(f"{archived_other} legacy booking(s) were synced to another user and will move to this user.")
    if bookings.get("truncated"):
        warnings.append(f"Only the latest {MAX_LEGACY_BOOKINGS} legacy bookings are read.")
    total_charge = Decimal("0.00")
    deleted = 0
    for r in booking_rows:
        if str(r.get("is_deleted")) in ("1", "True", "true"):
            deleted += 1
            continue
        total_charge += _money(r.get("charge")) or Decimal("0.00")
    blocks = LegacyBookingBlock.objects.filter(legacy_user_id=legacy_uid, status=LegacyBookingBlockStatus.ACTIVE)

    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "new_user": serialize_new_user(user),
        "legacy_user": {
            "legacy_user_id": legacy_uid,
            "emp_id": legacy_emp,
            "name": str(legacy_user.get("name") or ""),
            "email": str(legacy_user.get("email") or ""),
            "details": public_legacy_row(legacy_user),
        },
        "legacy_wallet": wallet,
        "wallet_sync": {
            "can_sync": not wallet_reasons,
            "reasons": wallet_reasons,
            "targets": options,
            "selected_target": option["key"] if option else None,
            "migration_id": migration_id,
            "department": dept.name if dept else None,
            "legacy_balance": str(legacy_balance),
            "previously_credited": str(previously_credited),
            "credited_elsewhere": str(credited_elsewhere),
            "delta": str(delta),
            "subwallet_balance_before": str(sub_balance),
            "subwallet_balance_after": str(resulting),
            "existing_legacy_credits": existing_credits,
        },
        "bookings": {
            "ok": bookings["ok"],
            "error": bookings.get("error"),
            "count": len(booking_rows),
            "deleted_count": deleted,
            "total_charge": str(total_charge),
            "already_synced_to_this_user": archived_here,
            "synced_to_other_user": archived_other,
            "truncated": bookings.get("truncated", False),
            "active_blocks": blocks.count(),
            "active_blocks_linked_here": blocks.filter(resolved_user=user).count(),
            "rows": booking_rows[:BOOKINGS_IN_PREVIEW],
        },
    }


def _archive_bookings(user: User, legacy_user: dict, bookings: dict, actor: User) -> dict:
    now = timezone.now().isoformat()
    emp = exact_employee_id(legacy_user.get("emp_id"))
    raw_by_id = {}
    for raw in bookings["raw_rows"]:
        bid = _first_present(raw, "id", "booking_id")
        if bid is not None:
            raw_by_id[int(bid)] = raw
    created = updated = 0
    for row in bookings["rows"]:
        payload = {
            **row,
            "legacy_row": public_legacy_row(raw_by_id.get(row["legacy_booking_id"])),
            "legacy_user_id": int(legacy_user["id"]),
            "new_user_id": user.pk,
            "new_user_email": user.email,
            "synced_by": actor.email,
            "synced_at": now,
            "source": ADMIN_MAPPING_SOURCE,
        }
        _, was_created = LegacyBookingHistoryRecord.objects.update_or_create(
            source_booking_id=row["legacy_booking_id"],
            defaults={"employee_id": emp, "payload": payload},
        )
        if was_created:
            created += 1
        else:
            updated += 1

    linked = 0
    blocks = LegacyBookingBlock.objects.filter(
        legacy_user_id=int(legacy_user["id"]), status=LegacyBookingBlockStatus.ACTIVE
    )
    for block in blocks:
        payload = dict(block.legacy_payload or {})
        payload["user_resolved_at"] = now
        payload["resolved_via"] = ADMIN_MAPPING_SOURCE
        payload["resolved_by"] = actor.email
        block.resolved_user = user
        block.user_mapping_status = LegacyUserMappingStatus.RESOLVED_CHANNEL_I
        block.user_mapping_source = ADMIN_MAPPING_SOURCE
        block.legacy_payload = payload
        block.save(update_fields=["resolved_user", "user_mapping_status", "user_mapping_source", "legacy_payload"])
        linked += 1
    return {"archived_created": created, "archived_updated": updated, "blocks_linked": linked}


def apply_sync(
    user: User,
    legacy_uid: int,
    *,
    actor: User,
    reader: OldMySQLReader,
    wallet_target: str | None = None,
    sync_wallet: bool = True,
    sync_bookings: bool = True,
    expected_legacy_balance: str | None = None,
) -> dict[str, Any]:
    """Confirm the mapping, then sync wallet balance and/or legacy bookings."""
    if not sync_wallet and not sync_bookings:
        raise AdminSyncError("Select wallet and/or bookings to sync.")
    preview = build_sync_preview(user, legacy_uid, wallet_target=wallet_target, reader=reader)
    if preview["blockers"]:
        raise AdminSyncError(" ".join(preview["blockers"]))
    ws = preview["wallet_sync"]
    if sync_wallet:
        if not ws["can_sync"]:
            raise AdminSyncError(" ".join(ws["reasons"]))
        expected = _money(expected_legacy_balance)
        if expected is None or expected != Decimal(ws["legacy_balance"]):
            raise AdminSyncError(
                f"Old-portal balance is now ₹{ws['legacy_balance']}, which differs from the tested amount. "
                "Run Test sync again before confirming."
            )

    legacy_user = reader.fetchone("SELECT * FROM users WHERE id = %s LIMIT 1", (int(legacy_uid),))
    key = _mapping_key(reader, legacy_user)
    wallet_snapshot = preview["legacy_wallet"]
    txns = list(reader.iter_wallet_transactions_for_user(int(legacy_uid))) if sync_wallet else []
    bookings = fetch_legacy_bookings_for_user(reader, int(legacy_uid)) if sync_bookings else None

    result: dict[str, Any] = {"ok": True, "mapping_key": key, "wallet": None, "bookings": None}
    with transaction.atomic():
        mapping, _ = LegacyWalletAccountMapping.objects.update_or_create(
            employee_id=key,
            defaults={
                "old_user_id": int(legacy_user["id"]),
                "old_wallet_id": wallet_snapshot["wallet_id"],
                "old_name": str(legacy_user.get("name") or "")[:255],
                "old_email": str(legacy_user.get("email") or "")[:255],
                "new_user": user,
                "channel_i_employee_id": exact_employee_id(user.emp_id),
                "channel_i_email": user.email or "",
                "channel_i_name": user.name or "",
                "mapping_status": LegacyWalletMappingStatus.VALID,
                "exception_reason": "",
                "old_credits": Decimal(wallet_snapshot["total_credits"]),
                "old_debits": Decimal(wallet_snapshot["total_debits"]),
                "old_wallet_balance": Decimal(wallet_snapshot["balance"]),
                "migration_batch": f"{ADMIN_BATCH_PREFIX}:{actor.pk}",
            },
        )

        if sync_wallet:
            batch = f"{ADMIN_BATCH_PREFIX}:{user.pk}"
            counts = {"imported": 0, "duplicate": 0, "other": 0}
            for txn in txns:
                outcome = import_transaction(txn, {"emp_id": key}, batch)
                counts[outcome if outcome in counts else "other"] += 1
            target = next(o for o in ws["targets"] if o["key"] == ws["selected_target"])
            if target["key"] == TARGET_OWN:
                target_wallet = _own_wallet(user) or Wallet.objects.create(user=user)
            else:
                target_wallet = Wallet.objects.get(pk=target["wallet_id"])
            recon = reconcile_legacy_balance_to_subwallet(
                user=user,
                target_balance=Decimal(ws["legacy_balance"]),
                migration_id=ws["migration_id"],
                legacy_closing_balance=Decimal(ws["legacy_balance"]),
                reconciliation_reference=f"admin={actor.email} legacy_uid={int(legacy_user['id'])}",
                wallet=target_wallet,
                related_user=user,
                description_prefix=ADMIN_SYNC_PREFIX,
            )
            mapping.mapping_status = LegacyWalletMappingStatus.RECONCILED
            mapping.reconciliation_status = "ADMIN_SYNCED"
            mapping.save(update_fields=["mapping_status", "reconciliation_status", "updated_at"])
            result["wallet"] = {
                "target": target["key"],
                "wallet_owner": target_wallet.user.email,
                "legacy_balance": ws["legacy_balance"],
                "delta": str(recon.delta),
                "previously_credited": str(recon.previous_credited),
                "transaction_created": recon.created,
                "subwallet_balance": str(recon.sub_wallet.balance),
                "ledger_rows": counts,
            }

        if sync_bookings and bookings is not None:
            if not bookings["ok"]:
                raise AdminSyncError(bookings.get("error") or "Could not read legacy bookings.")
            result["bookings"] = _archive_bookings(user, legacy_user, bookings, actor)
    return result


def synced_bookings_for_user(user: User, limit: int = 500) -> list[dict]:
    qs = LegacyBookingHistoryRecord.objects.filter(payload__new_user_id=user.pk).order_by("-source_booking_id")[:limit]
    return [
        {
            "legacy_booking_id": rec.source_booking_id,
            "booking_date": (rec.payload or {}).get("booking_date"),
            "legacy_equipment_name": (rec.payload or {}).get("legacy_equipment_name"),
            "new_equipment": (rec.payload or {}).get("new_equipment"),
            "duration_minutes": (rec.payload or {}).get("duration_minutes"),
            "status": (rec.payload or {}).get("status"),
            "charge": (rec.payload or {}).get("charge"),
            "synced_at": (rec.payload or {}).get("synced_at"),
        }
        for rec in qs
    ]
