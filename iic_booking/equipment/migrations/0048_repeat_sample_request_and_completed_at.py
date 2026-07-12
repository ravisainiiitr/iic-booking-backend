# Repeat sample request: Equipment fields, Booking completed_at, RepeatSampleRequest model

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0047_booking_rating"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="repeat_sample_request_days",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Number of days after booking completion within which user can request a repeat sample. Leave empty to disable.",
                null=True,
                verbose_name="Repeat sample request window (days)",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="repeat_sample_disclaimer",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Disclaimer text shown to user in a popup when they request to repeat the sample.",
                verbose_name="Repeat sample disclaimer",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="completed_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the booking was marked as completed. Used for repeat-sample request time limit.",
                null=True,
                verbose_name="Completed at",
            ),
        ),
        migrations.CreateModel(
            name="RepeatSampleRequest",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("APPROVED", "Approved"),
                            ("REJECTED", "Rejected"),
                        ],
                        default="PENDING",
                        max_length=20,
                    ),
                ),
                ("user_notes", models.TextField(blank=True, default="")),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                ("responded_at", models.DateTimeField(blank=True, null=True)),
                ("admin_notes", models.TextField(blank=True, default="")),
                (
                    "booking",
                    models.ForeignKey(
                        help_text="Completed booking this request refers to",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="repeat_sample_requests",
                        to="equipment.booking",
                    ),
                ),
                (
                    "new_booking",
                    models.ForeignKey(
                        blank=True,
                        help_text="New booking created when request was approved (free re-run)",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_from_repeat_request",
                        to="equipment.booking",
                    ),
                ),
                (
                    "responded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="repeat_sample_requests_responded",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Repeat sample request",
                "verbose_name_plural": "Repeat sample requests",
                "ordering": ["-requested_at"],
            },
        ),
    ]
