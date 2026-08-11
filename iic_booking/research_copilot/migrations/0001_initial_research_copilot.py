# Generated manually for Phase AI.1 Research Copilot

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Conversation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(blank=True, default="", max_length=255)),
                ("user_role_snapshot", models.CharField(blank=True, default="", max_length=64)),
                ("department_id_snapshot", models.IntegerField(blank=True, null=True)),
                ("is_archived", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="research_copilot_conversations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-updated_at"]},
        ),
        migrations.CreateModel(
            name="Message",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "role",
                    models.CharField(
                        choices=[("user", "User"), ("assistant", "Assistant"), ("system", "System")],
                        max_length=16,
                    ),
                ),
                ("content", models.TextField()),
                ("confidence", models.FloatField(blank=True, null=True)),
                ("citations", models.JSONField(blank=True, default=list)),
                ("suggested_actions", models.JSONField(blank=True, default=list)),
                ("escalate_hint", models.BooleanField(default=False)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="messages",
                        to="research_copilot.conversation",
                    ),
                ),
            ],
            options={"ordering": ["created_at"]},
        ),
        migrations.CreateModel(
            name="CopilotAuditEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("conversation_created", "Conversation Created"),
                            ("message_sent", "Message Sent"),
                            ("message_replied", "Message Replied"),
                            ("stream_started", "Stream Started"),
                            ("feedback", "Feedback"),
                            ("escalate_hint", "Escalate Hint"),
                            ("feature_disabled", "Feature Disabled"),
                        ],
                        db_index=True,
                        max_length=32,
                    ),
                ),
                ("message", models.CharField(blank=True, default="", max_length=512)),
                ("detail", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="audit_events",
                        to="research_copilot.conversation",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="research_copilot_audit_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="KnowledgeGap",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("query_summary", models.CharField(blank=True, default="", max_length=512)),
                ("reason", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="knowledge_gaps",
                        to="research_copilot.conversation",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="MessageFeedback",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "rating",
                    models.CharField(choices=[("up", "Thumbs up"), ("down", "Thumbs down")], max_length=8),
                ),
                ("comment", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="feedback",
                        to="research_copilot.conversation",
                    ),
                ),
                (
                    "message",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="feedback",
                        to="research_copilot.message",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="research_copilot_feedback",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="conversation",
            index=models.Index(fields=["user", "-updated_at"], name="research_co_user_id_7e8a1c_idx"),
        ),
        migrations.AddIndex(
            model_name="message",
            index=models.Index(fields=["conversation", "created_at"], name="research_co_convers_9f2b4d_idx"),
        ),
    ]
