"""Learning rules: who manages content, completion, quiz scoring, the training-request prerequisite and the logbook."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import (
    LearningModule,
    LearningSetting,
    LogbookEntry,
    LogbookKind,
    ModuleProgress,
    QuizAttempt,
    QuizQuestion,
)

logger = logging.getLogger(__name__)

MAX_DOCUMENT_MB = 10
MAX_OPTIONS = 6


class LearningError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid", extra: dict | None = None):
        super().__init__(message)
        self.message, self.status, self.code, self.extra = message, status, code, extra or {}


def require(ok: bool, message: str = "You do not have access to this.") -> None:
    if not ok:
        raise LearningError(message, status=403, code="forbidden")


# --------------------------------------------------------------------------- access
def staff_equipment_ids(user) -> set[int] | None:
    """Content managers: Main Admin (all), the equipment's OIC / temporary OIC and Lab Operators."""
    from iic_booking.equipment.models import EquipmentOperator
    from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic
    from iic_booking.users.models.user_type import UserType

    if not user or not user.is_authenticated:
        return set()
    if user.user_type == UserType.ADMIN:
        return None
    ids: set[int] = set()
    if user.user_type == UserType.MANAGER:
        ids |= {int(x) for x in get_equipment_ids_managed_by_oic(user.id)}
    if user.user_type == UserType.OPERATOR:
        ids |= set(EquipmentOperator.objects.filter(operator=user).values_list("equipment_id", flat=True))
    return ids


def is_staff_for(user, equipment_id: int) -> bool:
    ids = staff_equipment_ids(user)
    return ids is None or int(equipment_id) in ids


def can_configure(user, equipment_id: int) -> bool:
    """The prerequisite switch: OIC or Main Admin."""
    from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic
    from iic_booking.users.models.user_type import UserType

    if not user or not user.is_authenticated:
        return False
    if user.user_type == UserType.ADMIN:
        return True
    return user.user_type == UserType.MANAGER and int(equipment_id) in set(get_equipment_ids_managed_by_oic(user.id))


# --------------------------------------------------------------------------- content
def _bool(v) -> bool:
    return v in (True, "true", "True", "1", 1)


def _int(data, name, lo, hi):
    try:
        v = int(data.get(name))
    except (TypeError, ValueError):
        raise LearningError(f"{name} must be a whole number.") from None
    if not lo <= v <= hi:
        raise LearningError(f"{name} must be between {lo} and {hi}.")
    return v


def apply_module(m: LearningModule, data: dict, *, creating: bool) -> None:
    if creating or "title" in data:
        title = str(data.get("title") or "").strip()
        if not title:
            raise LearningError("A title is required.")
        m.title = title[:200]
    for f, n in (("summary", 500), ("body", 50000), ("acknowledgement_text", 500)):
        if f in data:
            setattr(m, f, str(data.get(f) or "")[:n])
    if "video_url" in data:
        url = str(data.get("video_url") or "").strip()
        if url and not url.startswith("https://"):
            raise LearningError("The video link must start with https://.")
        m.video_url = url[:500]
    for f in ("is_required", "requires_acknowledgement", "is_published"):
        if f in data:
            setattr(m, f, _bool(data[f]))
    if "order" in data:
        m.order = _int(data, "order", 0, 999)
    if "pass_mark_pct" in data:
        m.pass_mark_pct = _int(data, "pass_mark_pct", 0, 100)
    if "max_attempts_per_day" in data:
        m.max_attempts_per_day = _int(data, "max_attempts_per_day", 1, 20)


def create_module(equipment, actor, data: dict) -> LearningModule:
    require(is_staff_for(actor, equipment.pk), "Only the equipment's OIC, Lab Operators or the Main Admin can add learning material.")
    m = LearningModule(equipment=equipment, created_by=actor)
    apply_module(m, data, creating=True)
    m.save()
    return m


def update_module(m: LearningModule, actor, data: dict) -> LearningModule:
    require(is_staff_for(actor, m.equipment_id))
    apply_module(m, data, creating=False)
    if m.is_published and m.pass_mark_pct and not m.questions.filter(is_active=True).exists():
        raise LearningError("Add at least one question, or set the pass mark to 0 (no quiz), before publishing.",
                            code="no_questions")
    m.save()
    return m


def attach_document(m: LearningModule, actor, upload) -> LearningModule:
    require(is_staff_for(actor, m.equipment_id))
    if upload is None:
        raise LearningError("Choose a PDF file.")
    if not str(upload.name).lower().endswith(".pdf") or (getattr(upload, "content_type", "") or "application/pdf") != "application/pdf":
        raise LearningError("Only PDF documents can be attached.")
    if upload.size > MAX_DOCUMENT_MB * 1024 * 1024:
        raise LearningError(f"The document must be at most {MAX_DOCUMENT_MB} MB.")
    head = upload.read(5)
    upload.seek(0)
    if head != b"%PDF-":
        raise LearningError("This file is not a PDF.")
    m.document.save(upload.name, upload, save=True)
    return m


def apply_question(q: QuizQuestion, data: dict, *, creating: bool) -> None:
    if creating or "text" in data:
        text = str(data.get("text") or "").strip()
        if not text:
            raise LearningError("The question is required.")
        q.text = text[:2000]
    if creating or "options" in data or "correct" in data:
        options = data.get("options", q.options)
        correct = data.get("correct", q.correct)
        if not isinstance(options, list) or not 2 <= len(options) <= MAX_OPTIONS:
            raise LearningError(f"Give between 2 and {MAX_OPTIONS} options.")
        options = [str(o).strip()[:500] for o in options]
        if any(not o for o in options):
            raise LearningError("Options cannot be empty.")
        if not isinstance(correct, list) or not correct:
            raise LearningError("Mark at least one correct option.")
        try:
            correct = sorted({int(i) for i in correct})
        except (TypeError, ValueError):
            raise LearningError("correct must list option numbers.") from None
        if any(i < 0 or i >= len(options) for i in correct):
            raise LearningError("A correct option number is out of range.")
        q.options, q.correct = options, correct
    if "explanation" in data:
        q.explanation = str(data.get("explanation") or "")[:2000]
    if "order" in data:
        q.order = _int(data, "order", 0, 999)
    if "is_active" in data:
        q.is_active = _bool(data["is_active"])


def add_question(m: LearningModule, actor, data: dict) -> QuizQuestion:
    require(is_staff_for(actor, m.equipment_id))
    q = QuizQuestion(module=m)
    apply_question(q, data, creating=True)
    q.save()
    return q


def update_question(q: QuizQuestion, actor, data: dict) -> QuizQuestion:
    require(is_staff_for(actor, q.module.equipment_id))
    apply_question(q, data, creating=False)
    m = q.module
    if (not q.is_active and m.is_published and m.pass_mark_pct
            and not m.questions.filter(is_active=True).exclude(pk=q.pk).exists()):
        raise LearningError("A published module with a quiz needs at least one active question.", code="no_questions")
    q.save()
    return q


# --------------------------------------------------------------------------- learner progress
def visible_modules(user, equipment):
    qs = LearningModule.objects.filter(equipment=equipment)
    if not is_staff_for(user, equipment.pk):
        qs = qs.filter(is_published=True)
    return qs


def _progress(user, m) -> ModuleProgress:
    row, _ = ModuleProgress.objects.get_or_create(user=user, module=m)
    return row


def is_complete(m: LearningModule, p: ModuleProgress | None) -> bool:
    if p is None:
        return False
    if m.requires_acknowledgement and not p.acknowledged_at:
        return False
    if m.pass_mark_pct and not p.passed_at:
        return False
    return p.viewed_at is not None or p.acknowledged_at is not None or p.passed_at is not None


def mark_viewed(m: LearningModule, user) -> ModuleProgress:
    require(m.is_published or is_staff_for(user, m.equipment_id))
    p = _progress(user, m)
    if not p.viewed_at:
        p.viewed_at = timezone.now()
        p.save(update_fields=["viewed_at", "updated_at"])
    return p


def acknowledge(m: LearningModule, user) -> ModuleProgress:
    require(m.is_published or is_staff_for(user, m.equipment_id))
    if not m.requires_acknowledgement:
        raise LearningError("This module has no sign-off.")
    p = _progress(user, m)
    now = timezone.now()
    p.viewed_at = p.viewed_at or now
    p.acknowledged_at = p.acknowledged_at or now
    p.save(update_fields=["viewed_at", "acknowledged_at", "updated_at"])
    return p


@transaction.atomic
def submit_quiz(m: LearningModule, user, answers: dict) -> dict:
    require(m.is_published or is_staff_for(user, m.equipment_id))
    if not m.pass_mark_pct:
        raise LearningError("This module has no quiz.")
    questions = list(m.questions.filter(is_active=True))
    if not questions:
        raise LearningError("This quiz has no questions yet.")
    since = timezone.now() - timedelta(days=1)
    if QuizAttempt.objects.filter(user=user, module=m, created_at__gte=since).count() >= m.max_attempts_per_day:
        raise LearningError(f"You can try this quiz {m.max_attempts_per_day} times a day; try again tomorrow.",
                            status=429, code="too_many_attempts")
    if not isinstance(answers, dict):
        raise LearningError("answers must map question ids to the chosen option numbers.")
    results, right = [], 0
    clean: dict[str, list[int]] = {}
    for q in questions:
        raw = answers.get(str(q.pk), answers.get(q.pk, []))
        if not isinstance(raw, list):
            raw = [raw]
        try:
            chosen = sorted({int(i) for i in raw})
        except (TypeError, ValueError):
            chosen = []
        clean[str(q.pk)] = chosen
        ok = chosen == sorted(q.correct)
        right += ok
        results.append({"question_id": q.pk, "correct": ok, "correct_options": q.correct, "explanation": q.explanation})
    score = round(100 * right / len(questions))
    passed = score >= m.pass_mark_pct
    QuizAttempt.objects.create(user=user, module=m, answers=clean, score_pct=score, passed=passed)
    p = ModuleProgress.objects.select_for_update().get_or_create(user=user, module=m)[0]
    now = timezone.now()
    p.attempts += 1
    p.viewed_at = p.viewed_at or now
    p.best_score_pct = max(p.best_score_pct or 0, score)
    if passed and not p.passed_at:
        p.passed_at = now
    p.save()
    return {"score_pct": score, "passed": passed, "pass_mark_pct": m.pass_mark_pct, "results": results,
            "complete": is_complete(m, p)}


def required_incomplete(user, equipment) -> list[LearningModule]:
    mods = list(LearningModule.objects.filter(equipment=equipment, is_published=True, is_required=True))
    prog = {p.module_id: p for p in ModuleProgress.objects.filter(user=user, module__in=mods)}
    return [m for m in mods if not is_complete(m, prog.get(m.pk))]


def setting_for(equipment) -> LearningSetting:
    return LearningSetting.objects.filter(equipment=equipment).first() or LearningSetting(equipment=equipment)


def save_setting(equipment, actor, data: dict) -> LearningSetting:
    require(can_configure(actor, equipment.pk), "Only the equipment's OIC or the Main Admin can change this.")
    row, _ = LearningSetting.objects.get_or_create(equipment=equipment)
    if "modules_before_training_request" in data:
        row.modules_before_training_request = _bool(data["modules_before_training_request"])
    row.updated_by = actor
    row.save()
    return row


def training_request_blocker(user, equipment) -> list[str]:
    """Titles of required modules still to complete when the equipment asks for them before a training request."""
    row = LearningSetting.objects.filter(equipment=equipment, modules_before_training_request=True).first()
    if row is None:
        return []
    return [m.title for m in required_incomplete(user, equipment)]


# --------------------------------------------------------------------------- logbook
def can_use_logbook(user, booking) -> bool:
    if booking.user_id == getattr(user, "pk", None):
        return True
    if is_staff_for(user, booking.equipment_id):
        return True
    from iic_booking.users.models.user_type import UserType

    if getattr(user, "user_type", None) == UserType.DEPT_ADMIN:
        return getattr(booking.equipment, "internal_department_id", None) == getattr(user, "department_id", -1)
    return False


def add_entry(booking, user, data: dict) -> LogbookEntry:
    require(can_use_logbook(user, booking), "Only the person who booked or the equipment's staff can write in this logbook.")
    kind = data.get("kind") or LogbookKind.NOTE
    if kind not in LogbookKind.values:
        raise LearningError("Unknown entry kind.")
    text = str(data.get("text") or "").strip()
    if not text:
        raise LearningError("Write something in the entry.")
    row = LogbookEntry.objects.create(booking=booking, equipment=booking.equipment, author=user, kind=kind, text=text[:5000])
    if kind in (LogbookKind.ISSUE, LogbookKind.SAFETY):
        _notify_staff(row)
    return row


def _notify_staff(entry: LogbookEntry) -> None:
    from iic_booking.communication.in_app import equipment_oic_users
    from iic_booking.equipment.models import EquipmentOperator
    from iic_booking.users.display import get_user_display_name

    eq = entry.equipment
    users = list(equipment_oic_users(eq)) + [o.operator for o in EquipmentOperator.objects.filter(equipment=eq).select_related("operator")]
    users = [u for u in {u.pk: u for u in users if u is not None}.values() if u.pk != entry.author_id]
    if not users:
        return
    who = get_user_display_name(entry.author) if entry.author_id else "Someone"
    label = entry.get_kind_display()

    def send():
        try:
            from iic_booking.communication.in_app import notify_in_app

            notify_in_app(users, title=f"{label}: {eq.name}", message=f"{who} (booking #{entry.booking_id}): {entry.text[:300]}",
                          link=f"/booking-management?booking={entry.booking_id}", notification_type="warning",
                          event="learning.logbook_issue", extra={"equipment_id": eq.pk, "booking_id": entry.booking_id})
        except Exception:
            logger.exception("logbook notification failed entry=%s", entry.pk)

    transaction.on_commit(send)
