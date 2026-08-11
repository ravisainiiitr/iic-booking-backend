# Generated manually for Phase AI.2 Knowledge Engine

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("research_copilot", "0001_initial_research_copilot"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="knowledgegap",
            name="suggested_faq",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.CreateModel(
            name="KnowledgeDocument",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=512)),
                ("source_type", models.CharField(default="markdown", max_length=32)),
                (
                    "category",
                    models.CharField(
                        choices=[
                            ("user_guide", "User Guide"),
                            ("operator_manual", "Operator Manual"),
                            ("sop", "SOP"),
                            ("troubleshooting", "Troubleshooting"),
                            ("deployment", "Deployment Guide"),
                            ("release_notes", "Release Notes"),
                            ("faq", "FAQ"),
                            ("policy", "Policy"),
                            ("training", "Training Material"),
                            ("equipment", "Equipment Knowledge"),
                            ("known_issues", "Known Issues"),
                            ("other", "Other"),
                        ],
                        default="other",
                        max_length=32,
                    ),
                ),
                (
                    "security_level",
                    models.CharField(
                        choices=[
                            ("public", "Public"),
                            ("authenticated", "Authenticated Users"),
                            ("operator", "Operator / Lab Staff"),
                            ("dept_admin", "Department Admin"),
                            ("admin", "Institute Administrator"),
                        ],
                        db_index=True,
                        default="authenticated",
                        max_length=32,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("active", "Active"),
                            ("archived", "Archived"),
                            ("failed", "Failed"),
                        ],
                        default="active",
                        max_length=16,
                    ),
                ),
                (
                    "index_status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("indexing", "Indexing"),
                            ("indexed", "Indexed"),
                            ("stale", "Stale"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("version", models.CharField(blank=True, default="1.0", max_length=64)),
                ("language", models.CharField(blank=True, default="en", max_length=16)),
                ("tags", models.JSONField(blank=True, default=list)),
                ("department_id", models.IntegerField(blank=True, db_index=True, null=True)),
                ("equipment_id", models.IntegerField(blank=True, db_index=True, null=True)),
                ("source_uri", models.CharField(blank=True, default="", max_length=1024)),
                ("external_url", models.URLField(blank=True, default="")),
                ("content_text", models.TextField(blank=True, default="")),
                ("content_hash", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("embedding_version", models.CharField(blank=True, default="", max_length=64)),
                ("chunk_count", models.PositiveIntegerField(default=0)),
                ("error_message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("indexed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="knowledge_documents_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-updated_at"]},
        ),
        migrations.CreateModel(
            name="KnowledgeChunk",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("chunk_index", models.PositiveIntegerField(default=0)),
                ("content", models.TextField()),
                ("token_estimate", models.PositiveIntegerField(default=0)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("embedding", models.JSONField(blank=True, default=list)),
                ("embedding_model", models.CharField(blank=True, default="", max_length=128)),
                ("embedding_version", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="chunks",
                        to="research_copilot.knowledgedocument",
                    ),
                ),
            ],
            options={"ordering": ["document_id", "chunk_index"]},
        ),
        migrations.CreateModel(
            name="EmbeddingJob",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("job_type", models.CharField(default="index", max_length=32)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("indexing", "Indexing"),
                            ("indexed", "Indexed"),
                            ("stale", "Stale"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("provider", models.CharField(blank=True, default="", max_length=64)),
                ("detail", models.JSONField(blank=True, default=dict)),
                ("error_message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "document",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="embedding_jobs",
                        to="research_copilot.knowledgedocument",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="SearchQueryLog",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("query", models.CharField(max_length=1024)),
                ("intent", models.CharField(blank=True, default="", max_length=64)),
                ("role_bucket", models.CharField(blank=True, default="", max_length=32)),
                ("hit_count", models.PositiveIntegerField(default=0)),
                ("top_score", models.FloatField(blank=True, null=True)),
                ("latency_ms", models.PositiveIntegerField(default=0)),
                ("citation_ids", models.JSONField(blank=True, default=list)),
                ("low_confidence", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
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
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="knowledgedocument",
            index=models.Index(fields=["category", "security_level", "status"], name="research_co_categor_ai2doc_idx"),
        ),
        migrations.AddIndex(
            model_name="knowledgedocument",
            index=models.Index(fields=["index_status", "status"], name="research_co_index_s_ai2doc_idx"),
        ),
        migrations.AddIndex(
            model_name="knowledgechunk",
            index=models.Index(fields=["document", "chunk_index"], name="research_co_documen_ai2chk_idx"),
        ),
    ]
