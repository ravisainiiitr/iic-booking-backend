"""List (default) or mark records that are obviously test data, by name / email / code patterns.

Users get ``is_test_account`` (through ``users.test_account_flags``: the Main Administrator account is never
marked); equipment and categories get a ``TestDataFlag`` (visibility and booking are not changed). Dry run by
default; ``--apply`` writes. ``--skip-users`` / ``--skip-equipment`` / ``--skip-categories`` leave out ids a
reviewer judged ambiguous; ``--equipment-ids`` adds equipment known to be test data.

Output goes to public CI logs: users are shown by id, type and reason (their name only when the name itself says
"test"), never by email.
"""

from __future__ import annotations

import re

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from iic_booking.equipment.models import Equipment, EquipmentCategory
from iic_booking.equipment.testdata import marked_ids, mark
from iic_booking.equipment.testdata_models import TestDataKind

NAME_PATTERNS = [
    (re.compile(r"^\s*test\b", re.I), "name starts with 'Test'"),
    (re.compile(r"\(\s*test\s*\)", re.I), "name contains '(TEST)'"),
    (re.compile(r"\bTEST\b"), "name contains 'TEST'"),
    (re.compile(r"\btest\s+(user|account|officer|operator|lab|admin|student|faculty|staff|oic|equipment)\b", re.I),
     "name says 'test <role>'"),
]
CODE_PATTERN = re.compile(r"test", re.I)
TEST_DOMAIN_PATTERN = re.compile(r"\.test$", re.I)
# ``linked_to_test_wallet`` alone is not proof: a real student may be linked to a test wallet.
STRONG_USER_REASONS = {"test_email_domain", "test_employee_id", "name_has_test", "email_has_test", "email_domain_test"}


def _ids(raw: str) -> set[int]:
    return {int(x) for x in re.split(r"[,\s]+", raw or "") if x.strip().isdigit()}


def _name_reason(name: str) -> str:
    for pattern, reason in NAME_PATTERNS:
        if pattern.search(name or ""):
            return reason
    return ""


def _user_reasons(user) -> list[str]:
    from iic_booking.users.test_account_flags import candidate_reasons

    reasons = candidate_reasons(user)
    domain = (user.email or "").rpartition("@")[2]
    if TEST_DOMAIN_PATTERN.search(domain) and "test_email_domain" not in reasons:
        reasons.append("email_domain_test")
    return [r for r in reasons if r in STRONG_USER_REASONS]


def _equipment_reason(e, extra: set[int]) -> str:
    if e.equipment_id in extra:
        return "listed as test equipment"
    reason = _name_reason(e.name or "")
    if reason:
        return reason
    if CODE_PATTERN.search(e.code or ""):
        return "code contains 'TEST'"
    return ""


class Command(BaseCommand):
    help = "List or mark obvious test users, equipment and equipment categories (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the marks (default: dry run).")
        parser.add_argument("--skip-users", default="", help="User ids to leave out (comma separated).")
        parser.add_argument("--skip-equipment", default="", help="Equipment ids to leave out.")
        parser.add_argument("--skip-categories", default="", help="Category ids to leave out.")
        parser.add_argument("--equipment-ids", default="", help="Equipment ids that are test data whatever the name.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        skip_users, skip_equipment = _ids(opts["skip_users"]), _ids(opts["skip_equipment"])
        skip_categories, extra = _ids(opts["skip_categories"]), _ids(opts["equipment_ids"])
        marks = marked_ids()
        out = self.stdout.write
        out(f"mode={'APPLY' if apply else 'DRY RUN'}")

        from iic_booking.users.test_account_flags import is_protected_account

        users = []
        for u in get_user_model().objects.filter(is_test_account=False).order_by("pk"):
            reasons = _user_reasons(u)
            if reasons:
                users.append((u, reasons))
        out(f"users_candidates={len(users)}")
        for u, reasons in users:
            state = "protected" if is_protected_account(u) else ("skip" if u.pk in skip_users else "flag")
            name = f" name={u.name!r}" if "name_has_test" in reasons else ""
            out(f"  user id={u.pk} [{state}]{name} type={u.user_type} active={u.is_active} reasons={','.join(reasons)}")

        equipment = []
        for e in Equipment.objects.order_by("equipment_id"):
            reason = _equipment_reason(e, extra)
            if not reason:
                continue
            if e.visible_to_test_accounts_only or e.equipment_id in marks["equipment"]:
                out(f"  equipment id={e.equipment_id} [already test] code={e.code!r} name={e.name!r}")
                continue
            equipment.append((e, reason))
        out(f"equipment_candidates={len(equipment)}")
        for e, reason in equipment:
            state = "skip" if e.equipment_id in skip_equipment else "flag"
            out(f"  equipment id={e.equipment_id} [{state}] code={e.code!r} name={e.name!r} status={e.status} "
                f"reason={reason}")
        already = Equipment.objects.filter(visible_to_test_accounts_only=True).count()
        out(f"equipment_visible_to_test_accounts_only={already}")

        categories = []
        for c in EquipmentCategory.objects.order_by("pk"):
            reason = _name_reason(c.name or "") or ("code contains 'TEST'" if CODE_PATTERN.search(c.code or "") else "")
            if reason and c.pk not in marks["category"]:
                categories.append((c, reason))
        out(f"category_candidates={len(categories)}")
        for c, reason in categories:
            state = "skip" if c.pk in skip_categories else "flag"
            out(f"  category id={c.pk} [{state}] name={c.name!r} code={c.code!r} reason={reason}")

        if not apply:
            out("Dry run: nothing written. Re-run with --apply to mark the [flag] rows.")
            return
        from iic_booking.users.test_account_flags import set_test_account_flag

        with transaction.atomic():
            flagged_users = []
            for u, _ in users:
                if u.pk in skip_users or is_protected_account(u):
                    continue
                set_test_account_flag(u, True, source="flag_test_data")
                flagged_users.append(u.pk)
            n_eq = sum(
                mark(TestDataKind.EQUIPMENT, e, reason=r)
                for e, r in equipment
                if e.equipment_id not in skip_equipment
            )
            n_cat = sum(mark(TestDataKind.CATEGORY, c, reason=r) for c, r in categories if c.pk not in skip_categories)
        out(f"flagged_users={len(flagged_users)} ids={flagged_users}")
        out(f"flagged_equipment={n_eq} flagged_categories={n_cat}")
