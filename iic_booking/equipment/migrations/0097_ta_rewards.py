from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0096_alter_chargeprofile_options_alter_booking_status_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="reward_discount_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Discount amount applied from TA reward points",
                max_digits=10,
                verbose_name="Reward discount amount",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="reward_points_used",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Reward points redeemed for this booking",
                max_digits=10,
                verbose_name="Reward points used",
            ),
        ),
        migrations.CreateModel(
            name="TARewardConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_enabled", models.BooleanField(default=False, help_text="Enable/disable TA reward earning and redemption", verbose_name="Reward system enabled")),
                ("points_per_duty_hour", models.DecimalField(decimal_places=2, default=Decimal("10.00"), max_digits=10, verbose_name="Points per duty hour")),
                ("points_per_sample", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10, verbose_name="Points per sample")),
                ("currency_per_point", models.DecimalField(decimal_places=4, default=Decimal("1.0000"), help_text="Currency discount equivalent of one reward point", max_digits=10, verbose_name="Currency value per point")),
                ("max_redeem_percent_per_booking", models.DecimalField(decimal_places=2, default=Decimal("30.00"), max_digits=5, verbose_name="Max redeem percentage per booking")),
                ("max_redeem_points_per_booking", models.PositiveIntegerField(default=300, verbose_name="Max redeem points per booking")),
                ("min_booking_amount_for_redeem", models.DecimalField(decimal_places=2, default=Decimal("100.00"), max_digits=10, verbose_name="Minimum booking amount for redemption")),
                ("expiry_days", models.PositiveIntegerField(blank=True, default=180, help_text="If set, earned points expire after this many days", null=True, verbose_name="Point expiry days")),
                ("allow_stack_with_other_discounts", models.BooleanField(default=True, verbose_name="Allow stacking with other discounts")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "TA reward config",
                "verbose_name_plural": "TA reward configs",
                "ordering": ["-updated_at"],
            },
        ),
        migrations.CreateModel(
            name="TARewardLedger",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("entry_type", models.CharField(choices=[("EARN", "Earn"), ("REDEEM", "Redeem"), ("EXPIRE", "Expire"), ("REVERSE", "Reverse"), ("ADJUST", "Adjust")], max_length=20, verbose_name="Entry type")),
                ("points", models.DecimalField(decimal_places=2, max_digits=10, verbose_name="Points")),
                ("currency_value", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10, verbose_name="Currency value")),
                ("source_type", models.CharField(choices=[("DUTY_LOG", "TA Duty Log"), ("BOOKING", "Booking"), ("MANUAL", "Manual"), ("EXPIRY", "Expiry")], max_length=20, verbose_name="Source type")),
                ("source_id", models.PositiveIntegerField(blank=True, null=True, verbose_name="Source ID")),
                ("description", models.TextField(blank=True, null=True, verbose_name="Description")),
                ("expires_at", models.DateTimeField(blank=True, null=True, verbose_name="Expires at")),
                ("is_expired", models.BooleanField(default=False, verbose_name="Is expired")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="ta_reward_ledger_entries_created", to=settings.AUTH_USER_MODEL, verbose_name="Created by")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ta_reward_ledger_entries", to=settings.AUTH_USER_MODEL, verbose_name="Student")),
            ],
            options={
                "verbose_name": "TA reward ledger entry",
                "verbose_name_plural": "TA reward ledger entries",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["student", "created_at"], name="equipment_ta_student_427f46_idx"),
                    models.Index(fields=["student", "entry_type", "created_at"], name="equipment_ta_student_197ac0_idx"),
                    models.Index(fields=["source_type", "source_id"], name="equipment_ta_source__a4d21a_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="TADutyLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("duty_date", models.DateField(verbose_name="Duty date")),
                ("hours_spent", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=6, verbose_name="Hours spent")),
                ("samples_processed", models.PositiveIntegerField(default=0, verbose_name="Samples processed")),
                ("remarks", models.TextField(blank=True, null=True, verbose_name="Remarks")),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("VERIFIED", "Verified"), ("REJECTED", "Rejected")], default="PENDING", max_length=20, verbose_name="Status")),
                ("verified_at", models.DateTimeField(blank=True, null=True, verbose_name="Verified at")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("booking", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="ta_duty_logs", to="equipment.booking", verbose_name="Related booking")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="ta_duty_logs_created", to=settings.AUTH_USER_MODEL, verbose_name="Created by")),
                ("equipment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ta_duty_logs", to="equipment.equipment", verbose_name="Equipment")),
                ("nomination", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="duty_logs", to="equipment.studentequipmentnomination", verbose_name="Nomination")),
                ("student", models.ForeignKey(help_text="Student who performed the TA duty", on_delete=django.db.models.deletion.PROTECT, related_name="ta_duty_logs", to=settings.AUTH_USER_MODEL, verbose_name="Student")),
                ("verified_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="ta_duty_logs_verified", to=settings.AUTH_USER_MODEL, verbose_name="Verified by")),
            ],
            options={
                "verbose_name": "TA duty log",
                "verbose_name_plural": "TA duty logs",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["student", "status", "duty_date"], name="equipment_ta_student_876138_idx"),
                    models.Index(fields=["equipment", "duty_date"], name="equipment_ta_equipme_0bb670_idx"),
                    models.Index(fields=["booking"], name="equipment_ta_booking_9f65d9_idx"),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="tadutylog",
            constraint=models.CheckConstraint(check=models.Q(("hours_spent__gt", 0)) | models.Q(("samples_processed__gt", 0)), name="ta_duty_hours_or_samples_positive"),
        ),
        migrations.AddConstraint(
            model_name="tadutylog",
            constraint=models.UniqueConstraint(condition=models.Q(("booking__isnull", False)), fields=("booking", "student"), name="ta_duty_unique_booking_student"),
        ),
        migrations.CreateModel(
            name="BookingRewardRedemption",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("points_used", models.DecimalField(decimal_places=2, max_digits=10, verbose_name="Points used")),
                ("discount_amount", models.DecimalField(decimal_places=2, max_digits=10, verbose_name="Discount amount")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("booking", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="reward_redemption", to="equipment.booking", verbose_name="Booking")),
                ("ledger_entry", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="booking_redemptions", to="equipment.tarewardledger", verbose_name="Ledger entry")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="booking_reward_redemptions", to=settings.AUTH_USER_MODEL, verbose_name="Student")),
            ],
            options={
                "verbose_name": "Booking reward redemption",
                "verbose_name_plural": "Booking reward redemptions",
                "ordering": ["-created_at"],
            },
        ),
    ]
