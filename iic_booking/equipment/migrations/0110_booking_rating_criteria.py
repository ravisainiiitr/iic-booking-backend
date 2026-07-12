# Generated manually: booking rating criteria (yes/no) + overall 0-5.

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0109_booking_maintenance_operational_marked_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="rating_on_time_operator_availability",
            field=models.BooleanField(
                blank=True,
                help_text="User rating criteria: was the operator available and on-time? (Yes/No)",
                null=True,
                verbose_name="On-time & operator availability",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="rating_laboratory_cleanliness_organization",
            field=models.BooleanField(
                blank=True,
                help_text="User rating criteria: lab cleanliness and organization (Yes/No)",
                null=True,
                verbose_name="Laboratory cleanliness & organization",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="rating_sample_handling_care",
            field=models.BooleanField(
                blank=True,
                help_text="User rating criteria: sample handling and care (Yes/No)",
                null=True,
                verbose_name="Sample handling & care",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="rating_operator_behaviour_professionalism",
            field=models.BooleanField(
                blank=True,
                help_text="User rating criteria: operator behaviour and professionalism (Yes/No)",
                null=True,
                verbose_name="Operator behaviour & professionalism",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="rating_compliance_booking_request_parameters",
            field=models.BooleanField(
                blank=True,
                help_text="User rating criteria: compliance with booking request parameters (Yes/No)",
                null=True,
                verbose_name="Compliance with booking request parameters",
            ),
        ),
        migrations.AlterField(
            model_name="booking",
            name="rating",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Overall user rating computed from criteria (0-5)",
                null=True,
                validators=[MinValueValidator(0), MaxValueValidator(5)],
                verbose_name="Rating",
            ),
        ),
    ]

