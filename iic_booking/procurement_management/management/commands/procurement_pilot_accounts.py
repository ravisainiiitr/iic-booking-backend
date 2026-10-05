"""Create the flagged test accounts used to pilot Procurement & Assets (same conventions as ``seed_test_users``).

Usage (passwords are read from stdin as JSON and never printed)::

    echo '{"finance": "...", "oc_stores": "...", "hod": "..."}' | \
        python manage.py procurement_pilot_accounts --department-id 33 --types finance oc_stores hod

An account that already exists (``test.<type>@iic-booking.test``) is reported as reused and left unchanged. No wallets
are created or credited.
"""

from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from iic_booking.procurement_management.access import MODULE_USER_TYPES
from iic_booking.users.models import Department, User
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.test_accounts import user_email_for_type


class Command(BaseCommand):
    help = "Create Procurement & Assets pilot test accounts (is_test_account) without touching wallets."

    def add_arguments(self, parser):
        parser.add_argument("--department-id", type=int, required=True)
        parser.add_argument("--types", nargs="+", required=True)

    def handle(self, *args, **options):
        dept = Department.objects.filter(pk=options["department_id"], department_type=DepartmentType.INTERNAL).first()
        if dept is None:
            raise CommandError("Internal department not found.")
        labels = dict(UserType.get_choices())
        types = list(dict.fromkeys(options["types"]))
        bad = [t for t in types if t not in MODULE_USER_TYPES or t == UserType.ADMIN]
        if bad:
            raise CommandError(f"Not a procurement staff type: {', '.join(bad)}")
        try:
            passwords = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError as exc:
            raise CommandError("stdin must be a JSON object of type -> password.") from exc

        with transaction.atomic():
            for code in types:
                email = user_email_for_type(code)
                user = User.objects.filter(email__iexact=email).first()
                if user is not None:
                    self.stdout.write(f"PILOT_ACCOUNT reused id={user.pk} type={user.user_type} email={user.email} name={user.name!r}")
                    continue
                password = passwords.get(code)
                if not password or len(password) < 12:
                    raise CommandError(f"A password of at least 12 characters is required for {code}.")
                user = User(
                    email=email,
                    name=f"Test {labels[code]}",
                    user_type=code,
                    is_test_account=True,
                    email_verified=True,
                    admin_approved=True,
                    supervisor_approved=True,
                    force_inactive=False,
                    access_on_hold=False,
                    department=dept,
                )
                user.set_password(password)
                user.save()
                self.stdout.write(f"PILOT_ACCOUNT created id={user.pk} type={code} email={email} name={user.name!r}")
