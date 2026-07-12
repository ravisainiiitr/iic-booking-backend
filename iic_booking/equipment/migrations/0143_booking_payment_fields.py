# Booking payment fields and PENDING_PAYMENT status

import decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0142_booking_not_utilized_celery_schedule"),
        ("users", "0076_department_grant_code_payments"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="wallet_amount_applied",
            field=models.DecimalField(
                decimal_places=2,
                default=decimal.Decimal("0.00"),
                help_text="Amount debited from department sub-wallet at booking time",
                max_digits=10,
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="amount_due",
            field=models.DecimalField(
                decimal_places=2,
                default=decimal.Decimal("0.00"),
                help_text="Remaining amount to collect after wallet debit",
                max_digits=10,
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="payment_settled_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When booking balance was fully paid (wallet + gateway/UTR)",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="settlement_department",
            field=models.ForeignKey(
                blank=True,
                help_text="Internal department (from equipment) for payment settlement",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="bookings_settled",
                to="users.department",
            ),
        ),
        migrations.AlterField(
            model_name="booking",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("PENDING_PAYMENT", "Awaiting payment"),
                    ("WAITLISTED", "Waitlisted"),
                    ("BOOKED", "Booked"),
                    ("DISRUPTION_PENDING", "Awaiting your choice (disruption)"),
                    ("UNDER_MAINTENANCE", "Under Maintenance"),
                    ("OTHER_DISRUPTION", "Other Disruption"),
                    ("HOLD", "Hold"),
                    ("COMPLETED", "Completed"),
                    ("CANCELLED", "Cancelled"),
                    ("ABSENT", "Operator Unavailable"),
                    ("REFUNDED", "Refunded"),
                    ("BOOKING_NOT_UTILIZED", "Booking Not Utilized"),
                ],
                default="PENDING",
                help_text="Booking status",
                max_length=30,
            ),
        ),
    ]
