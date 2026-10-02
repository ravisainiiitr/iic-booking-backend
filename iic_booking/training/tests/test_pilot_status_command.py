import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from iic_booking.training.management.commands.training_pilot_status import mask_email
from iic_booking.users.models.user_type import UserType

from .conftest import make_user


def run(*args):
    out = StringIO()
    call_command("training_pilot_status", "--json", *args, stdout=out)
    return json.loads(out.getvalue())


def test_mask_email():
    assert mask_email("ravi.saini@iitr.ac.in") == "r***@iitr.ac.in"
    assert mask_email("not-an-email") == "***"


@pytest.mark.django_db
def test_status_reports_settings_without_full_emails(world, settings):
    settings.TRAINING_MODULE_ENABLED = False
    settings.TRAINING_PILOT_EQUIPMENT_CODES = f"{world.equipment.code},MISSING1"
    settings.TRAINING_PILOT_OIC_EMAILS = world.oic.email
    interval = IntervalSchedule.objects.create(every=30, period="minutes")
    PeriodicTask.objects.create(name="training-housekeeping", task="training.housekeeping", interval=interval)

    out = StringIO()
    call_command("training_pilot_status", "--json", stdout=out)
    raw = out.getvalue()
    report = json.loads(raw)

    assert world.oic.email not in raw
    assert report["mode"] == "status"
    assert report["module_enabled"] is False
    assert {e["code"]: e["found"] for e in report["pilot_equipment"]} == {world.equipment.code: True, "MISSING1": False}
    assert report["pilot_oic_email_count"] == 1
    assert report["pilot_oics"] == [{"email": mask_email(world.oic.email), "active_user": True, "is_pilot_oic": True}]
    assert report["housekeeping_task"] == {"name": "training-housekeeping", "exists": True, "enabled": True}
    assert set(report["migrations"]) == {"on_disk", "applied", "pending"}


@pytest.mark.django_db
def test_validate_accepts_oic_and_temporary_oic_and_warns_for_non_oic(world):
    report = run(
        "--codes", world.equipment.code,
        "--oic-emails", f"{world.oic.email.upper()}, {world.temp_oic.email},{world.other_oic.email}",
    )
    assert report["mode"] == "validate"
    assert report["errors"] == []
    assert [o["is_pilot_oic"] for o in report["pilot_oics"]] == [True, True, False]
    assert len(report["warnings"]) == 1 and mask_email(world.other_oic.email) in report["warnings"][0]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "codes,emails,expected",
    [
        ("", "", "at least one equipment code"),
        ("apreo", "", "is not valid"),
        ("NOPE99", "", "No equipment with code NOPE99"),
        (None, "bad-email", "not a valid email"),
        (None, "ghost@iitr.ac.in", "does not belong to an active user"),
    ],
)
def test_validate_fails_clearly(world, codes, emails, expected):
    args = ["--codes", world.equipment.code if codes is None else codes, "--oic-emails", emails]
    with pytest.raises(CommandError, match=expected):
        call_command("training_pilot_status", *args, stdout=StringIO())


@pytest.mark.django_db
def test_validate_rejects_inactive_user(world):
    user = make_user(user_type=UserType.MANAGER, admin_approved=False)
    user.refresh_from_db()
    assert user.is_active is False
    with pytest.raises(CommandError, match="does not belong to an active user"):
        call_command(
            "training_pilot_status", "--codes", world.equipment.code, "--oic-emails", user.email, stdout=StringIO()
        )
