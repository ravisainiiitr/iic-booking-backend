# Additive only: new tables for curated Copilot knowledge articles, their version history and
# Copilot-to-ticket escalations, plus nullable / database-defaulted columns on existing Copilot
# tables. Existing rows and conversation history are untouched.

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0199_repeat_sample_self_booking"),
        ("research_copilot", "0004_conversation_access_mode"),
        ("support", "0008_portal_feedback"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CopilotKnowledgeArticle",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=255)),
                ("question", models.TextField(blank=True, default="")),
                ("answer", models.TextField()),
                (
                    "category",
                    models.CharField(
                        choices=[
                            ("booking", "Booking"),
                            ("cancellation", "Cancellation & reschedule"),
                            ("wallet", "Wallet & payments"),
                            ("equipment", "Equipment"),
                            ("account", "Account & profile"),
                            ("faculty", "Faculty & supervisors"),
                            ("results", "Samples & results"),
                            ("remote_analysis", "Remote Analysis"),
                            ("my_research", "My Research"),
                            ("policy", "Portal policy"),
                            ("general", "General"),
                        ],
                        db_index=True,
                        default="general",
                        max_length=32,
                    ),
                ),
                ("keywords", models.JSONField(blank=True, default=list)),
                (
                    "audience",
                    models.CharField(
                        choices=[
                            ("all", "All signed-in users"),
                            ("internal", "Internal users"),
                            ("external", "External users"),
                            ("student", "Students"),
                            ("faculty", "Faculty"),
                            ("staff", "Staff (admin, OIC, lab)"),
                        ],
                        default="all",
                        max_length=16,
                    ),
                ),
                ("related_feature", models.CharField(blank=True, default="", max_length=64)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("manual", "Written by admin"),
                            ("ticket", "Support ticket resolution"),
                            ("gap", "Unanswered question"),
                        ],
                        default="manual",
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("pending_approval", "Pending approval"),
                            ("approved", "Approved"),
                            ("inactive", "Inactive"),
                        ],
                        db_index=True,
                        default="draft",
                        max_length=24,
                    ),
                ),
                ("version", models.PositiveIntegerField(default=1)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("usage_count", models.PositiveIntegerField(default=0)),
                ("helpful_count", models.PositiveIntegerField(default=0)),
                ("not_helpful_count", models.PositiveIntegerField(default=0)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="copilot_articles_approved",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="copilot_articles_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="copilot_articles_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "source_ticket",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="copilot_knowledge_articles",
                        to="support.ticket",
                    ),
                ),
                (
                    "related_equipment",
                    models.ManyToManyField(
                        blank=True, related_name="copilot_knowledge_articles", to="equipment.equipment"
                    ),
                ),
            ],
            options={
                "ordering": ["-updated_at"],
                "indexes": [models.Index(fields=["status", "category"], name="research_co_status_2abcbb_idx")],
            },
        ),
        migrations.CreateModel(
            name="CopilotKnowledgeArticleVersion",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("version", models.PositiveIntegerField()),
                ("title", models.CharField(max_length=255)),
                ("question", models.TextField(blank=True, default="")),
                ("answer", models.TextField()),
                ("category", models.CharField(max_length=32)),
                ("keywords", models.JSONField(blank=True, default=list)),
                ("audience", models.CharField(max_length=16)),
                ("status", models.CharField(max_length=24)),
                ("change", models.CharField(blank=True, default="", max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "article",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="versions",
                        to="research_copilot.copilotknowledgearticle",
                    ),
                ),
                (
                    "changed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="copilot_article_versions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="CopilotEscalation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("question", models.TextField(blank=True, default="")),
                ("intent", models.CharField(blank=True, default="", max_length=64)),
                ("entities", models.JSONField(blank=True, default=dict)),
                ("equipment_id", models.IntegerField(blank=True, null=True)),
                ("booking_id", models.IntegerField(blank=True, null=True)),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("no_verified_answer", "No verified answer"),
                            ("action_failed", "Action could not complete"),
                            ("user_requested", "User asked for support"),
                            ("negative_feedback", "Negative feedback"),
                            ("other", "Other"),
                        ],
                        default="other",
                        max_length=32,
                    ),
                ),
                ("copilot_response", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="escalations",
                        to="research_copilot.conversation",
                    ),
                ),
                (
                    "message",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="escalations",
                        to="research_copilot.message",
                    ),
                ),
                (
                    "ticket",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="copilot_escalations",
                        to="support.ticket",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="research_copilot_escalations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "knowledge_article",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="escalations",
                        to="research_copilot.copilotknowledgearticle",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddField(
            model_name="conversation",
            name="state",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="messagefeedback",
            name="reason",
            field=models.CharField(blank=True, db_default="", default="", max_length=32),
        ),
        migrations.AddField(
            model_name="messagefeedback",
            name="intent",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        migrations.AddField(
            model_name="messagefeedback",
            name="knowledge_article",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="feedback",
                to="research_copilot.copilotknowledgearticle",
            ),
        ),
        migrations.AddField(
            model_name="knowledgegap",
            name="status",
            field=models.CharField(blank=True, db_default="open", db_index=True, default="open", max_length=16),
        ),
        migrations.AddField(
            model_name="knowledgegap",
            name="intent",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        migrations.AddField(
            model_name="knowledgegap",
            name="resolved_article",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="resolved_gaps",
                to="research_copilot.copilotknowledgearticle",
            ),
        ),
        migrations.AddField(
            model_name="knowledgegap",
            name="resolved_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="research_copilot_resolved_gaps",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="knowledgegap",
            name="resolved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
