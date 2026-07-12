# Add rating, rating_feedback, rated_at to Booking

from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0046_holiday_remove_display_mode_icon"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="rating",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="User rating 1-5 stars",
                null=True,
                validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(5)],
                verbose_name="Rating",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="rating_feedback",
            field=models.TextField(
                blank=True,
                help_text="Optional feedback text from the user who rated",
                null=True,
                verbose_name="Rating feedback",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="rated_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the user submitted the rating",
                null=True,
                verbose_name="Rated at",
            ),
        ),
    ]
