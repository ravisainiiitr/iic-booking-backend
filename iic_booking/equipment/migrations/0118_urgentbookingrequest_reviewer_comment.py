from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0117_sample_trace_op_unavailable_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="urgentbookingrequest",
            name="reviewer_comment",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Faculty narrative for urgent comment from reviewer (submitted with evidence)",
            ),
        ),
    ]
