# Generated migration: add Document link type and document file to MenuItem

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("cms", "0003_heroslide"),
    ]

    operations = [
        migrations.AddField(
            model_name="menuitem",
            name="document",
            field=models.FileField(
                blank=True,
                help_text="Upload a PDF or other document. Used when Link type is 'Document (PDF upload)'.",
                null=True,
                upload_to="cms/menu_documents/%Y/%m/",
                verbose_name="Document (PDF)",
            ),
        ),
        migrations.AlterField(
            model_name="menuitem",
            name="link_type",
            field=models.CharField(
                choices=[
                    ("internal_anchor", "Internal anchor (#section)"),
                    ("internal_route", "Internal route (/path)"),
                    ("external_url", "External URL"),
                    ("trigger", "Trigger (e.g. Contact / Ticket form)"),
                    ("document", "Document (PDF upload)"),
                ],
                default="internal_anchor",
                max_length=20,
                verbose_name="Link type",
            ),
        ),
        migrations.AlterField(
            model_name="menuitem",
            name="url",
            field=models.CharField(
                blank=True,
                help_text="Anchor (#section), path (/equipments), or full URL. Leave blank for trigger or document.",
                max_length=500,
                verbose_name="URL or anchor",
            ),
        ),
    ]
