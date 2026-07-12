# Generated migration: CmsPage model and MenuItem.page + link_type page

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("cms", "0004_menuitem_document_menuitem_link_type_document"),
    ]

    operations = [
        migrations.CreateModel(
            name="CmsPage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=200, verbose_name="Title")),
                ("slug", models.SlugField(max_length=200, unique=True, verbose_name="Slug")),
                (
                    "content",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="List of blocks: heading, paragraph, image, list, quote, divider.",
                        verbose_name="Content blocks",
                    ),
                ),
                ("is_published", models.BooleanField(default=False, verbose_name="Published")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["title"],
                "verbose_name": "CMS page",
                "verbose_name_plural": "CMS pages",
            },
        ),
        migrations.AddField(
            model_name="menuitem",
            name="page",
            field=models.ForeignKey(
                blank=True,
                help_text="CMS page to link to. Used when Link type is 'CMS page'.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="menu_items",
                to="cms.cmspage",
                verbose_name="CMS page",
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
                    ("page", "CMS page"),
                ],
                default="internal_anchor",
                max_length=20,
                verbose_name="Link type",
            ),
        ),
    ]
