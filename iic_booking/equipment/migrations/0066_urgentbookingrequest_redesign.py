# Urgent booking request redesign: request_type, evidence file, wallet owner approval

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0065_equipment_slot_window_reference"),
    ]

    operations = [
        migrations.AddField(
            model_name="urgentbookingrequest",
            name="request_type",
            field=models.CharField(
                choices=[("NO_SLOT", "Unable to get slot despite repeated trials"), ("REVIEWER_URGENT", "Urgent comment from reviewer")],
                default="NO_SLOT",
                help_text="Reason: no slot despite trials, or urgent comment from reviewer",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="urgentbookingrequest",
            name="evidence_file",
            field=models.FileField(
                blank=True,
                help_text="Documentary evidence for urgent comment from reviewer",
                null=True,
                upload_to="urgent_requests/%Y/%m/%d/",
            ),
        ),
        migrations.AddField(
            model_name="urgentbookingrequest",
            name="evidence_original_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="urgentbookingrequest",
            name="wallet_approved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="urgentbookingrequest",
            name="wallet_approved_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="urgent_requests_wallet_approved",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Approved by wallet owner",
            ),
        ),
        migrations.AddField(
            model_name="urgentbookingrequest",
            name="wallet_notes",
            field=models.TextField(blank=True, default=""),
        ),
    ]
