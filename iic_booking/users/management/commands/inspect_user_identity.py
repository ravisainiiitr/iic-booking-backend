from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.channel_i_identity import (
    is_wallet_migration_eligible,
    looks_like_iic_operator_code,
)
from iic_booking.users.models import User


class Command(BaseCommand):
    help = (
        "Read-only identity diagnostic. Does not modify users, wallets, or tokens. "
        "Lookup by --email or --emp-id."
    )

    def add_arguments(self, parser):
        parser.add_argument("--email", default="")
        parser.add_argument("--emp-id", default="")
        parser.add_argument("--user-id", type=int, default=0)

    def handle(self, *args, **options):
        qs = User.objects.all()
        email = (options.get("email") or "").strip()
        emp = (options.get("emp_id") or "").strip()
        uid = options.get("user_id") or 0
        if email:
            qs = qs.filter(email__iexact=email)
        elif emp:
            qs = qs.filter(emp_id=emp)
        elif uid:
            qs = qs.filter(pk=uid)
        else:
            self.stderr.write("Provide --email, --emp-id, or --user-id.")
            return
        users = list(qs[:20])
        if not users:
            self.stdout.write("No matching user.")
            return
        for user in users:
            emp_id = (user.emp_id or "").strip()
            matches = User.objects.filter(emp_id=emp_id).exclude(emp_id="").count() if emp_id else 0
            eligible, reason = is_wallet_migration_eligible(
                employee_id=emp_id,
                production_user_count=matches,
                identity_source="LEGACY_UNVERIFIED",
                has_conflict=looks_like_iic_operator_code(emp_id),
                user_is_active=bool(user.is_active),
            )
            self.stdout.write("---")
            self.stdout.write(f"django_user_id={user.pk}")
            self.stdout.write(f"email={user.email}")
            self.stdout.write(f"name={user.name}")
            self.stdout.write(f"is_active={user.is_active}")
            self.stdout.write(f"user_type={user.user_type}")
            self.stdout.write(f"channel_i_user_id=internal_id={user.internal_id or ''}")
            self.stdout.write("channel_i_username=(not stored; User.username is unused)")
            self.stdout.write(f"employee_id=emp_id={emp_id}")
            self.stdout.write("employee_id_source=LEGACY_UNVERIFIED (no provenance table)")
            self.stdout.write("verification_status=UNVERIFIED")
            self.stdout.write(
                f"iic_operator_code={looks_like_iic_operator_code(emp_id)}"
            )
            self.stdout.write(f"conflict_or_duplicate_count={matches}")
            self.stdout.write(f"wallet_migration_eligible={eligible}")
            self.stdout.write(f"wallet_migration_reason={reason}")
            self.stdout.write("secrets_omitted=access_token,refresh_token,client_secret,cookies")
