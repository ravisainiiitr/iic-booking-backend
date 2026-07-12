# Add BookingSampleTraceReplyAttachment for reply file uploads

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0038_bookingsampletrace_user_reply"),
    ]

    operations = [
        migrations.CreateModel(
            name="BookingSampleTraceReplyAttachment",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("file", models.FileField(upload_to="sample_trace_replies/%Y/%m/%d/", verbose_name="File")),
                ("original_name", models.CharField(blank=True, default="", max_length=255, verbose_name="Original filename")),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "sample_trace",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="reply_attachments",
                        to="equipment.bookingsampletrace",
                        verbose_name="Sample trace event",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="sample_trace_reply_attachments",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Uploaded by",
                    ),
                ),
            ],
            options={
                "verbose_name": "Sample trace reply attachment",
                "verbose_name_plural": "Sample trace reply attachments",
                "ordering": ["uploaded_at"],
            },
        ),
    ]
