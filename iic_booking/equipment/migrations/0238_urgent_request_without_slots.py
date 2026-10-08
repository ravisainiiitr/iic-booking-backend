"""Type B urgent requests without slots: booking inputs, required time and the amount shown on submission
are stored on the request; the OIC allocates the slots on approval (additive columns with defaults)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0237_disruption_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='urgentbookingrequest',
            name='requires_slot_allocation',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Type B request submitted with the booking inputs only (no slots); the OIC chooses the slots '
                    'on approval. duration_minutes holds the required time.'
                ),
            ),
        ),
        migrations.AddField(
            model_name='urgentbookingrequest',
            name='input_values',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Booking inputs (Step 1) given by the user for a request without slots',
            ),
        ),
        migrations.AddField(
            model_name='urgentbookingrequest',
            name='estimated_charge',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=10,
                null=True,
                help_text='Amount shown to the user on submission (category rate + 50% urgent surcharge, GST if any)',
            ),
        ),
        migrations.AddField(
            model_name='urgentbookingrequest',
            name='estimated_charge_breakdown',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='urgentbookingrequest',
            name='preferred_schedule',
            field=models.TextField(
                blank=True,
                default='',
                help_text='Preferred dates / time given by the user (free text, optional)',
            ),
        ),
    ]
