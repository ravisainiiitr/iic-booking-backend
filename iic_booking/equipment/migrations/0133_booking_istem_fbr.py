# Generated manually for I-STEM FBR workflow (external users).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0132_equipment_booking_email_extra_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="istem_fbr_number",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Facility Booking Record number from the national I-STEM portal for this request.",
                max_length=128,
                verbose_name="I-STEM FBR number",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="istem_fbr_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("PENDING_FBR", "FBR not submitted"),
                    ("PENDING_OIC", "Awaiting OIC verification"),
                    ("INVALID", "FBR rejected — correction required"),
                    ("EXECUTED", "FBR verified / executed"),
                ],
                help_text="Workflow state for external-user FBR verification by OIC. Null for internal users or pre-migration bookings.",
                max_length=20,
                null=True,
                verbose_name="I-STEM FBR status",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="istem_fbr_invalid_reason",
            field=models.TextField(
                blank=True,
                help_text="Shown to the user when OIC marks the FBR as invalid.",
                null=True,
                verbose_name="I-STEM FBR rejection reason",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="istem_fbr_executed_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When OIC marked the FBR as executed on I-STEM.",
                null=True,
                verbose_name="I-STEM FBR executed at",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="istem_fbr_verified_by",
            field=models.ForeignKey(
                blank=True,
                help_text="OIC (or admin) who last executed or rejected the FBR.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="bookings_istem_fbr_verified",
                to=settings.AUTH_USER_MODEL,
                verbose_name="I-STEM FBR verified by",
            ),
        ),
    ]
