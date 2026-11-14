"""Remote Analysis large data (14 Nov 2026): speed tests, raw-data pre-load and the results outbox.

Bulk bytes never pass through the portal: agents move them straight to/from S3 with presigned URLs. These rows are
the portal's record of each transfer (progress, checksums, notices).
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class SpeedTestStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    COMPLETED = "completed", _("Completed")
    FAILED = "failed", _("Failed")


class TransferSpeedTest(models.Model):
    """One link speed test of an Analysis PC (RAA agent) or an Equipment PC (DSA)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="speed_tests",
    )
    sync_agent = models.ForeignKey(
        "sync.DepartmentSyncAgent",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="speed_tests",
    )
    status = models.CharField(max_length=16, choices=SpeedTestStatus.choices, default=SpeedTestStatus.PENDING)
    object_key = models.CharField(max_length=512, blank=True, default="")
    bytes = models.BigIntegerField(default=0)
    upload_mbps = models.FloatField(null=True, blank=True)
    download_mbps = models.FloatField(null=True, blank=True)
    seconds = models.FloatField(null=True, blank=True)
    measured_at = models.DateTimeField(null=True, blank=True)
    detail = models.CharField(max_length=300, blank=True, default="")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workstation", "status", "-created_at"], name="ra_speed_ws_idx"),
            models.Index(fields=["sync_agent", "status", "-created_at"], name="ra_speed_dsa_idx"),
        ]

    def __str__(self) -> str:
        return f"Speed test {self.id} ({self.status})"


class PreloadState(models.TextChoices):
    QUEUED = "queued", _("Queued")
    DOWNLOADING = "downloading", _("Downloading")
    READY = "ready", _("Ready")
    FAILED = "failed", _("Failed")
    DISCARDED = "discarded", _("Discarded")


class InputPreload(models.Model):
    """Raw data the reserved Analysis PC pre-loads into its locked holding folder before the user joins."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reservation = models.OneToOneField(
        "remote_analysis.AnalysisReservation", on_delete=models.CASCADE, related_name="input_preload"
    )
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="input_preloads",
    )
    state = models.CharField(max_length=16, choices=PreloadState.choices, default=PreloadState.QUEUED)
    revision = models.PositiveIntegerField(default=1, help_text=_("Bumped each time the input changes."))
    files_done = models.PositiveIntegerField(default=0)
    files_total = models.PositiveIntegerField(default=0)
    bytes_done = models.BigIntegerField(default=0)
    bytes_total = models.BigIntegerField(default=0)
    rate_bps = models.FloatField(null=True, blank=True, help_text=_("Observed download rate (bytes per second)."))
    detail = models.CharField(max_length=300, blank=True, default="")
    command = models.ForeignKey(
        "remote_analysis.RemoteCommand", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    queued_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    discarded_at = models.DateTimeField(null=True, blank=True)
    last_progress_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["workstation", "state"], name="ra_preload_ws_idx")]

    def __str__(self) -> str:
        return f"Preload {self.reservation_id} ({self.state})"


class OutputState(models.TextChoices):
    QUEUED = "queued", _("Queued")
    UPLOADING = "uploading", _("Uploading")
    PAUSED = "paused", _("Paused")
    COMPLETE = "complete", _("Complete")
    FAILED = "failed", _("Failed")


class OutputUpload(models.Model):
    """A session's results outbox, uploaded by the Analysis PC in the background after the session ended."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(
        "remote_analysis.RemoteDesktopSession", on_delete=models.CASCADE, related_name="output_upload"
    )
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="output_uploads",
    )
    booking = models.ForeignKey(
        "equipment.Booking", null=True, blank=True, on_delete=models.SET_NULL, related_name="analysis_output_uploads"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    research_link = models.ForeignKey(
        "my_research.ResearchWorkspaceBooking", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    state = models.CharField(max_length=16, choices=OutputState.choices, default=OutputState.QUEUED)
    files_done = models.PositiveIntegerField(default=0)
    files_total = models.PositiveIntegerField(default=0)
    bytes_done = models.BigIntegerField(default=0)
    bytes_total = models.BigIntegerField(default=0)
    rate_bps = models.FloatField(null=True, blank=True)
    detail = models.CharField(max_length=300, blank=True, default="")
    paused_reason = models.CharField(max_length=300, blank=True, default="")
    last_progress_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    staff_alerted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["state", "last_progress_at"], name="ra_outbox_state_idx")]

    def __str__(self) -> str:
        return f"Outbox {self.session_id} ({self.state})"


class OutputFileStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    DONE = "done", _("Done")
    SKIPPED = "skipped", _("Already in My Research")


class OutputUploadFile(models.Model):
    """One results file of an outbox; ``id`` is the ``file_id`` the agent uses."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    outbox = models.ForeignKey(OutputUpload, on_delete=models.CASCADE, related_name="files")
    rel_path = models.CharField(max_length=1024)
    size = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    planned_research_id = models.UUIDField(default=uuid.uuid4, help_text=_("Id the My Research file gets on completion."))
    key = models.CharField(max_length=1024, blank=True, default="")
    s3_upload_id = models.CharField(max_length=1024, blank=True, default="")
    part_size = models.BigIntegerField(default=0)
    status = models.CharField(max_length=16, choices=OutputFileStatus.choices, default=OutputFileStatus.PENDING)
    research_file = models.ForeignKey(
        "my_research.ResearchFile", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    detail = models.CharField(max_length=300, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["outbox", "rel_path"], name="ra_outbox_file_unique_path")]

    def __str__(self) -> str:
        return self.rel_path
