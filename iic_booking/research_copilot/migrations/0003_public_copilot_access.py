# Generated manually for AI.24.1 public Copilot conversations

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("research_copilot", "0002_knowledge_engine"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="conversation",
            name="access_mode",
            field=models.CharField(
                blank=True,
                default="authenticated",
                help_text="public | authenticated — set by backend, never by the LLM",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="conversation",
            name="anonymous_session_key",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
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
        migrations.AddIndex(
            model_name="conversation",
            index=models.Index(
                fields=["anonymous_session_key", "-updated_at"],
                name="research_co_anonymo_idx",
            ),
        ),
    ]
