# Generated migration: add BookingSampleTrace for sample/slot tracing

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0033_booking_results_available_notified_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="BookingSampleTrace",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("SAMPLE_SENT", "Sample Sent"), ("SAMPLE_ACCEPTED", "Sample Accepted"), ("SAMPLE_REJECTED", "Sample Rejected"), ("PROCESSING", "Processing"), ("COMPLETED", "Completed")], max_length=20, verbose_name="Status")),
                ("sample_identifiers", models.TextField(blank=True, default="", help_text="Optional identifiers when status is Sample Sent", verbose_name="Sample identifiers")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("booking", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sample_trace_events", to="equipment.booking", verbose_name="Booking")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="booking_sample_trace_events", to=settings.AUTH_USER_MODEL, verbose_name="Created by")),
            ],
            options={
                "verbose_name": "Booking sample trace",
                "verbose_name_plural": "Booking sample traces",
                "ordering": ["created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="bookingsampletrace",
            index=models.Index(fields=["booking", "created_at"], name="equipment_b_booking_7a1b2c_idx"),
        ),
    ]
