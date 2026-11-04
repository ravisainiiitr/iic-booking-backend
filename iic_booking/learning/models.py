"""Online learning before hands-on training (per equipment): modules with text, video and SOP document, a
read-and-understood sign-off, multiple-choice quizzes; and a digital logbook of each booking's run."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

USER = settings.AUTH_USER_MODEL


class LearningSetting(models.Model):
    equipment = models.OneToOneField("equipment.Equipment", on_delete=models.CASCADE, related_name="learning_setting")
    modules_before_training_request = models.BooleanField(
        default=False, help_text=_("Required online modules must be complete before a training request is accepted."),
    )
    updated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)


class LearningModule(models.Model):
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.CASCADE, related_name="learning_modules")
    title = models.CharField(max_length=200)
    summary = models.CharField(max_length=500, blank=True, default="")
    body = models.TextField(blank=True, default="", help_text=_("Reading material (plain text or Markdown)."))
    video_url = models.URLField(max_length=500, blank=True, default="")
    document = models.FileField(upload_to="learning/%Y/%m/", blank=True, null=True, help_text=_("SOP or safety PDF."))
    order = models.PositiveSmallIntegerField(default=0)
    is_required = models.BooleanField(default=True)
    requires_acknowledgement = models.BooleanField(default=True)
    acknowledgement_text = models.CharField(
        max_length=500, blank=True,
        default="I have read and understood this material, including the safety instructions, and will follow it.",
    )
    pass_mark_pct = models.PositiveSmallIntegerField(default=80, help_text=_("0 = no quiz."))
    max_attempts_per_day = models.PositiveSmallIntegerField(default=3)
    is_published = models.BooleanField(default=False)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["equipment_id", "order", "pk"]


class QuizQuestion(models.Model):
    module = models.ForeignKey(LearningModule, on_delete=models.CASCADE, related_name="questions")
    text = models.TextField()
    options = models.JSONField(default=list)
    correct = models.JSONField(default=list, help_text=_("Indexes of the correct options."))
    explanation = models.TextField(blank=True, default="")
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "pk"]


class ModuleProgress(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="learning_progress")
    module = models.ForeignKey(LearningModule, on_delete=models.CASCADE, related_name="progress")
    viewed_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    best_score_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    passed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "module"], name="learning_progress_once")]


class QuizAttempt(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="quiz_attempts")
    module = models.ForeignKey(LearningModule, on_delete=models.CASCADE, related_name="attempts")
    answers = models.JSONField(default=dict)
    score_pct = models.PositiveSmallIntegerField()
    passed = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class LogbookKind(models.TextChoices):
    NOTE = "NOTE", _("Note")
    PARAMETERS = "PARAMETERS", _("Run parameters")
    ISSUE = "ISSUE", _("Problem with the equipment")
    SAFETY = "SAFETY", _("Safety observation")


class LogbookEntry(models.Model):
    """One line in a booking's digital logbook. Never edited or deleted; a correction is a new entry."""

    booking = models.ForeignKey("equipment.Booking", on_delete=models.CASCADE, related_name="logbook_entries")
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.CASCADE, related_name="logbook_entries")
    author = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name="+")
    kind = models.CharField(max_length=12, choices=LogbookKind.choices, default=LogbookKind.NOTE)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["equipment", "created_at"])]
