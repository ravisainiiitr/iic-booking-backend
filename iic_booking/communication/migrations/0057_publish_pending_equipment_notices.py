from django.db import migrations
from django.db.models import Q
from django.utils import timezone

NON_OPERATIONAL_STATUSES = ("REPAIR", "INACTIVE", "MAINTENANCE")
COMMENT = "Published automatically: equipment unavailability notices from the Officer in Charge do not need approval."


def publish_pending(apps, schema_editor):
    """Equipment-unavailability notices waiting for Main Admin approval go on the board, as new ones now do."""
    Notice = apps.get_model("communication", "Notice")
    now = timezone.now()
    Notice.objects.filter(
        source="EQUIPMENT_UNAVAILABLE",
        approval_status="PENDING",
        needs_oic_expiry=False,
        equipment__status__in=NON_OPERATIONAL_STATUSES,
    ).filter(Q(expiry_date__isnull=True) | Q(expiry_date__gt=now)).update(
        approval_status="APPROVED",
        is_active=True,
        reviewed_at=now,
        review_comment=COMMENT,
        updated_at=now,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("communication", "0056_switch_off_charge_recalculated_email"),
        ("equipment", "0001_initial"),
    ]

    operations = [migrations.RunPython(publish_pending, migrations.RunPython.noop)]
