# Generated manually for TicketEvent + expanded ticket types

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("support", "0006_alter_ticket_attachment_max_length"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ticket",
            name="ticket_type",
            field=models.CharField(
                choices=[
                    ("booking", "Booking Issues"),
                    ("equipment", "Equipment Support"),
                    ("payment", "Payment Issues"),
                    ("account", "Account Support"),
                    ("technical", "Technical Problems"),
                    ("laboratory", "Laboratory Requests"),
                    ("general", "General Enquiries"),
                    ("other", "Other"),
                    ("quality_improvement", "Quality improvement suggestions/Bugs"),
                ],
                db_index=True,
                default="other",
                help_text="Type of ticket",
                max_length=50,
                verbose_name="Ticket Type",
            ),
        ),
        migrations.CreateModel(
            name="TicketEvent",
            fields=[
                ("event_id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("created", "Created"),
                            ("status_changed", "Status Changed"),
                            ("assigned", "Assigned"),
                            ("comment", "Comment"),
                            ("internal_note", "Internal Note"),
                            ("resolved", "Resolved"),
                            ("closed", "Closed"),
                            ("priority_changed", "Priority Changed"),
                            ("notes_updated", "Notes Updated"),
                        ],
                        db_index=True,
                        max_length=40,
                        verbose_name="Event Type",
                    ),
                ),
                ("message", models.TextField(blank=True, default="", verbose_name="Message")),
                ("from_value", models.CharField(blank=True, default="", max_length=255, verbose_name="From")),
                ("to_value", models.CharField(blank=True, default="", max_length=255, verbose_name="To")),
                ("metadata", models.JSONField(blank=True, default=dict, verbose_name="Metadata")),
                (
                    "is_internal",
                    models.BooleanField(
                        default=False,
                        help_text="Hide from non-staff requesters when True.",
                        verbose_name="Is Internal",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Created at")),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ticket_events",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Actor",
                    ),
                ),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="events",
                        to="support.ticket",
                        verbose_name="Ticket",
                    ),
                ),
            ],
            options={
                "verbose_name": "Ticket Event",
                "verbose_name_plural": "Ticket Events",
                "ordering": ["created_at"],
            },
        ),
    ]
