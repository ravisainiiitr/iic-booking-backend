"""Synthetic data for local acceptance testing of the external-booking enhancement.

Only runs under config.settings.acceptance_local (database iic_extbook_accept_dev on this machine). Re-runnable.
Every identity is synthetic (``@acceptance.invalid`` addresses cannot receive mail). One shared password is generated
on the first run and kept in ``.acceptance_local/credentials.txt`` (local, untracked); it is never printed.
Feature switches are changed through the same service functions as the admin API, so the change logs record them.
"""

import secrets
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from _bootstrap import ROOT, setup

settings = setup()

from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from iic_booking.equipment.models import (  # noqa: E402
    ChargeProfile,
    DynamicInputField,
    DynamicInputFieldType,
    Equipment,
    EquipmentManager,
    EquipmentOperator,
    EquipmentProfileType,
    Holiday,
    SlotMaster,
)
from iic_booking.equipment.slot_utils import SlotGenerator  # noqa: E402
from iic_booking.external_services import policy, window_admin  # noqa: E402
from iic_booking.external_services.models import ExternalBookingWindowConfig  # noqa: E402
from iic_booking.users.models import Department, ExternalBillingProfile, SubWallet, Wallet  # noqa: E402
from iic_booking.users.models.department import DepartmentType  # noqa: E402
from iic_booking.users.models.portal_migration import PortalMigrationState  # noqa: E402
from iic_booking.users.models.user_type import UserType  # noqa: E402
from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus  # noqa: E402
from iic_booking.users.tests.factories import UserFactory  # noqa: E402

DOMAIN = "acceptance.invalid"
IST = timezone.get_fixed_timezone(330)
REASON = "Local acceptance testing only (database iic_extbook_accept_dev); not a production setting."
CREDENTIALS = ROOT / ".acceptance_local" / "credentials.txt"

# key, user type, display name, internal department?, staff flag
ACCOUNTS = [
    ("ext.academic", UserType.EXTERNAL, "Synthetic External Academic", False, False),
    ("ext.academic2", UserType.EXTERNAL, "Synthetic External Academic Two", False, False),
    ("ext.industry", UserType.INSTITUTE, "Synthetic External Industry", False, False),
    ("student", UserType.STUDENT, "Synthetic Internal Student", True, False),
    ("faculty", UserType.FACULTY, "Synthetic Internal Faculty", True, False),
    ("oic", UserType.MANAGER, "Synthetic Officer In Charge", True, True),
    ("operator", UserType.OPERATOR, "Synthetic Lab Operator", True, False),
    ("accounts", UserType.FINANCE, "Synthetic Accounts In Charge", True, False),
    ("admin", UserType.ADMIN, "Synthetic Main Administrator", True, True),
]

EQUIPMENT = [
    ("ACCXRD", "Synthetic X-Ray Diffractometer (acceptance)"),
    ("ACCSEM", "Synthetic Scanning Electron Microscope (acceptance)"),
]
SYNTHETIC_HOLIDAY = date(2026, 11, 20)
FIRST_MONDAY, LAST_MONDAY = date(2026, 10, 26), date(2026, 12, 21)


def shared_password() -> str:
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    if CREDENTIALS.exists():
        for line in CREDENTIALS.read_text(encoding="utf-8").splitlines():
            if line.startswith("password: "):
                return line.split(": ", 1)[1].strip()
    return "Acc-" + secrets.token_urlsafe(12)


def write_credentials(password: str, users: dict) -> None:
    lines = [
        "Local acceptance accounts (synthetic; database iic_extbook_accept_dev only). Do not reuse elsewhere.",
        f"password: {password}",
        "",
    ]
    lines += [f"{u.user_type:12} {u.email}" for u in users.values()]
    CREDENTIALS.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    password = shared_password()
    with transaction.atomic():
        state, _ = PortalMigrationState.objects.get_or_create(singleton_key="default")
        state.booking_opens_at = datetime(2026, 9, 30, 21, 0, tzinfo=IST)
        state.end_user_booking_enabled = True
        state.save()

        dept, _ = Department.objects.get_or_create(
            code="ACCML",
            defaults={"name": "Synthetic Materials Lab (acceptance)", "department_type": DepartmentType.INTERNAL},
        )
        dept.equipment_booking_enabled = True
        dept.equipment_visibility_enabled = True
        dept.save()

        users = {}
        for key, user_type, name, internal, staff in ACCOUNTS:
            fields = {
                "user_type": user_type, "name": name, "admin_approved": True, "email_verified": True,
                "supervisor_approved": True, "is_active": True, "is_staff": staff,
            }
            if internal:
                fields["department"] = dept
                # Internal accounts normally sign in with Channel i; this is the user's own "Sign in with email"
                # profile switch (no Channel i locally).
                fields["email_login_enabled"] = True
            user = UserFactory(email=f"{key}@{DOMAIN}", password=password, **fields)
            for k, v in fields.items():
                setattr(user, k, v)
            user.set_password(password)
            user.save()
            users[key] = user

        for key in ("ext.academic", "ext.academic2", "ext.industry"):
            u = users[key]
            ExternalBillingProfile.objects.update_or_create(user=u, defaults={
                "billing_name": f"{u.name} Pvt (synthetic)", "gstin": "",
                "billing_address_line1": "1 Synthetic Street", "billing_address_line2": "Test Park",
                "billing_city": "Testpur", "billing_state": "Uttarakhand", "billing_pincode": "247667",
                "billing_country": "India",
            })
            wallet, _ = Wallet.objects.get_or_create(user=u)
            sub, _ = SubWallet.objects.get_or_create(wallet=wallet, department=dept,
                                                     defaults={"balance": Decimal("0.00")})
            if key == "ext.industry" and sub.balance < Decimal("10000"):
                sub.credit(Decimal("10000") - sub.balance, description="Synthetic acceptance credit", related_user=u)

        faculty_wallet, _ = Wallet.objects.get_or_create(user=users["faculty"])
        fsub, _ = SubWallet.objects.get_or_create(wallet=faculty_wallet, department=dept,
                                                  defaults={"balance": Decimal("0.00")})
        if fsub.balance < Decimal("50000"):
            fsub.credit(Decimal("50000") - fsub.balance, description="Synthetic acceptance credit",
                        related_user=users["faculty"])
        WalletJoinRequest.objects.update_or_create(
            student=users["student"], faculty=users["faculty"],
            defaults={"wallet": faculty_wallet, "status": WalletJoinRequestStatus.APPROVED,
                      "message": "Synthetic acceptance join", "responded_at": timezone.now()},
        )

        Holiday.objects.get_or_create(date=SYNTHETIC_HOLIDAY, defaults={
            "reason": "Synthetic acceptance holiday", "is_active": True})

        equipment = []
        for code, name in EQUIPMENT:
            eq, _ = Equipment.objects.get_or_create(code=code, defaults={
                "name": name, "slot_duration_minutes": 60, "user_rating_enabled": False,
                "internal_department": dept, "status": "ACTIVE", "external_slot_quota_percent": 100,
            })
            equipment.append(eq)
            charges = {UserType.EXTERNAL: "500.00", UserType.INSTITUTE: "800.00", UserType.STUDENT: "100.00",
                       UserType.FACULTY: "100.00"}
            for user_type, amount in charges.items():
                ChargeProfile.objects.get_or_create(
                    equipment=eq, user_type=user_type,
                    defaults={"profile_type": EquipmentProfileType.HOUR, "time_formula": "60",
                              "primary_unit_charge": Decimal(amount)},
                )
            # The requirement field users fill in on the booking page (special requests need at least one value).
            DynamicInputField.objects.get_or_create(equipment=eq, user_type="", field_key="A", defaults={
                "field_label": "Number of samples", "field_type": DynamicInputFieldType.NUMERIC, "is_required": True,
                "default_value": "1", "help_text": "1\n10\n1"})
            EquipmentManager.objects.get_or_create(equipment=eq, manager=users["oic"])
            EquipmentOperator.objects.get_or_create(equipment=eq, operator=users["operator"])
            for n, hour in enumerate(range(9, 17), start=1):
                SlotMaster.objects.get_or_create(equipment=eq, slot_number=n, defaults={
                    "open_time": time(hour, 0), "close_time": time(hour + 1, 0), "is_active": True})

    created = 0
    for eq in equipment:
        monday = FIRST_MONDAY
        while monday <= LAST_MONDAY:
            created += len(SlotGenerator.generate_slots_for_week(eq, monday, monday + timedelta(days=6),
                                                                 allow_holiday=True))
            monday += timedelta(days=7)

    admin = users["admin"]
    config = ExternalBookingWindowConfig.load()
    if not config.enabled:
        window_admin.update(admin, {
            "enabled": True, "initial_open_at": "2026-10-28T21:00:00+05:30", "first_week_start": "2026-11-09",
            "initial_weeks": 4, "auto_expand": True, "expansion_weekday": 2, "expansion_time": "21:00",
            "min_lead_days": 0, "reason": REASON,
        })
    policy.update_settings(admin, {
        "special_requests_enabled": True, "special_request_surcharge": False, "max_open_special_requests": 3,
        "short_notice_days": 12, "payment_window_hours": 24, "invoice_requests_enabled": True,
        "early_invoice_allowed": True, "auto_request_without_preference": False,
        "sric_dispatch_mode": "test", "sric_test_recipient_email": f"sric-test@{DOMAIN}",
        "physical_copies_enabled": True, "sample_labels_enabled": True,
        "iic_postal_address": "IIC (LOCAL ACCEPTANCE TEST ADDRESS)\nSynthetic Building, Test Road\nTestpur 247667",
        "reason": REASON,
    })
    write_credentials(password, users)

    print(f"Department: {dept.name} (id {dept.pk})")
    for eq in equipment:
        print(f"Equipment: {eq.name} (id {eq.pk})")
    print(f"Daily slots created this run: {created}")
    print("Accounts: " + ", ".join(f"{k} ({u.user_type})" for k, u in users.items()))
    print(f"Credentials file: {CREDENTIALS}")


if __name__ == "__main__":
    main()
