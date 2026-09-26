from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0198_booking_result_views_and_data_shares'),
    ]

    operations = [
        migrations.AddField(
            model_name='repeatsamplerequest',
            name='bookable_from',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text='Earliest slot start the user may book for the approved repeat (approval time + 48 hours)',
            ),
        ),
        migrations.AddField(
            model_name='repeatsamplerequest',
            name='extra_week_granted',
            field=models.BooleanField(
                default=False,
                help_text='One additional week of slot access was granted for booking the approved repeat',
            ),
        ),
        migrations.AddField(
            model_name='repeatsamplerequest',
            name='booked_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text='When the user created the complimentary repeat booking',
            ),
        ),
        migrations.AlterField(
            model_name='repeatsamplerequest',
            name='new_booking',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name='created_from_repeat_request',
                to='equipment.booking',
                help_text='Complimentary repeat booking created by the user after approval',
            ),
        ),
    ]
