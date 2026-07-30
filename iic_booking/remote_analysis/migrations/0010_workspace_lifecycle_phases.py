# Generated manually for automatic data sync lifecycle states

from django.db import migrations, models


LEGACY_MAP = {
    "QUEUED": "Preparing",
    "PREPARING": "Preparing",
    "DOWNLOADING": "DownloadingInput",
    "READY": "InputReady",
    "UPLOADING": "UploadingOutput",
    "RETRYING": "RetryPending",
    "COMPLETED": "Completed",
    "FAILED": "PreparationFailed",
    "CANCELLED": "Cancelled",
}


def forwards_map_phases(apps, schema_editor):
    AnalysisWorkspace = apps.get_model("remote_analysis", "AnalysisWorkspace")
    for old, new in LEGACY_MAP.items():
        AnalysisWorkspace.objects.filter(sync_phase=old).update(sync_phase=new)


def backwards_map_phases(apps, schema_editor):
    reverse = {
        "Preparing": "PREPARING",
        "DownloadingInput": "DOWNLOADING",
        "VerifyingInput": "DOWNLOADING",
        "InputReady": "READY",
        "SessionStarting": "READY",
        "SessionActive": "READY",
        "CollectingOutput": "UPLOADING",
        "UploadingOutput": "UPLOADING",
        "UploadVerified": "COMPLETED",
        "Cleanup": "COMPLETED",
        "Completed": "COMPLETED",
        "PreparationFailed": "FAILED",
        "UploadFailed": "FAILED",
        "RetryPending": "RETRYING",
        "CleanupFailed": "FAILED",
        "Cancelled": "CANCELLED",
    }
    AnalysisWorkspace = apps.get_model("remote_analysis", "AnalysisWorkspace")
    for new, old in reverse.items():
        AnalysisWorkspace.objects.filter(sync_phase=new).update(sync_phase=old)


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0009_auto_data_sync_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="analysisworkspace",
            name="upload_verified_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Set when collect upload checksums are verified on the portal.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="analysisworkspace",
            name="sync_phase",
            field=models.CharField(
                choices=[
                    ("Preparing", "Preparing Workspace"),
                    ("DownloadingInput", "Downloading Input"),
                    ("VerifyingInput", "Verifying Input"),
                    ("InputReady", "Input Ready"),
                    ("SessionStarting", "Session Starting"),
                    ("SessionActive", "Session Active"),
                    ("CollectingOutput", "Collecting Output"),
                    ("UploadingOutput", "Uploading Output"),
                    ("UploadVerified", "Upload Verified"),
                    ("Cleanup", "Cleanup"),
                    ("Completed", "Completed"),
                    ("PreparationFailed", "Preparation Failed"),
                    ("UploadFailed", "Upload Failed"),
                    ("RetryPending", "Retry Pending"),
                    ("CleanupFailed", "Cleanup Failed"),
                    ("Cancelled", "Cancelled"),
                ],
                db_index=True,
                default="Preparing",
                help_text="Automatic data sync lifecycle phase shown to users.",
                max_length=32,
            ),
        ),
        migrations.RunPython(forwards_map_phases, backwards_map_phases),
    ]
