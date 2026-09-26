# Conversation.access_mode / anonymous_session_key already exist on production (added by earlier
# out-of-tree migrations recorded there), so the state change is applied without ALTER TABLE and
# the columns and indexes are only created on databases that do not have them yet.

from django.db import migrations, models

ANON_INDEX = "research_co_anonymo_idx"


def ensure_columns(apps, schema_editor):
    Conversation = apps.get_model("research_copilot", "Conversation")
    table = Conversation._meta.db_table
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        columns = {c.name for c in connection.introspection.get_table_description(cursor, table)}
    for name in ("access_mode", "anonymous_session_key"):
        if name not in columns:
            schema_editor.add_field(Conversation, Conversation._meta.get_field(name))
    with connection.cursor() as cursor:
        existing = set(connection.introspection.get_constraints(cursor, table))
    for index in Conversation._meta.indexes:
        if index.name == ANON_INDEX and index.name not in existing:
            schema_editor.add_index(Conversation, index)


class Migration(migrations.Migration):

    dependencies = [
        ("research_copilot", "0003_knowledge_document_files"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
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
                    field=models.CharField(blank=True, db_index=True, default="", max_length=64),
                ),
                migrations.AddIndex(
                    model_name="conversation",
                    index=models.Index(fields=["anonymous_session_key", "-updated_at"], name=ANON_INDEX),
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(ensure_columns, migrations.RunPython.noop),
    ]
