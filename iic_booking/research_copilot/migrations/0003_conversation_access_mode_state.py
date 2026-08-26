# Generated manually for Phase D.2 — align ORM with existing production columns.
# Database columns access_mode / anonymous_session_key already exist (NOT NULL).
# This migration updates Django state only — no ALTER TABLE.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("research_copilot", "0002_knowledge_engine"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="conversation",
                    name="user",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="research_copilot_conversations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                migrations.AddField(
                    model_name="conversation",
                    name="access_mode",
                    field=models.CharField(
                        choices=[("public", "Public"), ("authenticated", "Authenticated")],
                        default="authenticated",
                        max_length=32,
                    ),
                ),
                migrations.AddField(
                    model_name="conversation",
                    name="anonymous_session_key",
                    field=models.CharField(blank=True, default="", max_length=64),
                ),
            ],
            database_operations=[],
        ),
    ]
