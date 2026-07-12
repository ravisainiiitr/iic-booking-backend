# Urgent booking request + no-slot allocation log for internal users

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0040_booking_created_by"),
    ]

    operations = [
        migrations.CreateModel(
            name="NoSlotAllocationLog",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                ("number_of_samples", models.PositiveIntegerField(default=1, help_text="Number of samples requested")),
                ("slots_requested", models.PositiveIntegerField(default=1, help_text="Number of slots requested")),
                ("duration_minutes", models.PositiveIntegerField(blank=True, help_text="Total duration requested in minutes", null=True)),
                ("equipment", models.ForeignKey(help_text="Equipment for which slots were requested", on_delete=models.deletion.CASCADE, related_name="no_slot_allocation_logs", to="equipment.equipment")),
                ("user", models.ForeignKey(help_text="User who requested booking but got no slots", on_delete=models.deletion.CASCADE, related_name="no_slot_allocation_logs", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-requested_at"],
                "verbose_name": "No slot allocation log entry",
                "verbose_name_plural": "No slot allocation log",
            },
        ),
        migrations.CreateModel(
            name="UrgentBookingRequest",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                ("disclaimer_accepted", models.BooleanField(default=False, help_text="User confirmed disclaimer about genuine urgent requirement")),
                ("number_of_samples", models.PositiveIntegerField(default=1)),
                ("slots_requested", models.PositiveIntegerField(default=1)),
                ("duration_minutes", models.PositiveIntegerField(blank=True, null=True)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("APPROVED", "Approved"), ("REJECTED", "Rejected")], default="PENDING", max_length=20)),
                ("admin_notes", models.TextField(blank=True, default="")),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("decided_by", models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name="urgent_requests_decided", to=settings.AUTH_USER_MODEL, verbose_name="Decided by")),
                ("equipment", models.ForeignKey(help_text="Equipment for which urgent booking is requested", on_delete=models.deletion.CASCADE, related_name="urgent_booking_requests", to="equipment.equipment")),
                ("user", models.ForeignKey(help_text="User who requested urgent booking", on_delete=models.deletion.CASCADE, related_name="urgent_booking_requests", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-requested_at"],
                "verbose_name": "Urgent booking request",
                "verbose_name_plural": "Urgent booking requests",
            },
        ),
    ]
