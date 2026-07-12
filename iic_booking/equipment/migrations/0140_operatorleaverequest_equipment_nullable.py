from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0139_operator_leave_request"),
    ]

    operations = [
        migrations.AlterField(
            model_name="operatorleaverequest",
            name="equipment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="operator_leave_requests",
                to="equipment.equipment",
            ),
        ),
    ]

