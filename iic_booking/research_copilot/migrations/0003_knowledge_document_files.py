# Equipment manual uploads: source file metadata on KnowledgeDocument.
# Pre-existing index-name drift (manually named indexes in 0001/0002) is intentionally left untouched.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("research_copilot", "0002_knowledge_engine"),
    ]

    operations = [
        migrations.AddField(
            model_name="knowledgedocument",
            name="file_sha256",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="knowledgedocument",
            name="file_size",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="knowledgedocument",
            name="original_filename",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="knowledgedocument",
            name="page_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="knowledgedocument",
            name="source_file_key",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
    ]
