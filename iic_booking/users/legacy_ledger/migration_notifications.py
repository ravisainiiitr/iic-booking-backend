"""Phase 8C — migration notification batching (Celery async; dry-run safe)."""

from __future__ import annotations

import logging
from typing import Any, Iterable

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from iic_booking.communication.email_branding import user_display_name
from iic_booking.users.legacy_ledger.migration_emails import (
    build_migration_email,
    classify_migration_template,
    preview_sample_context,
)
from iic_booking.users.models import User
from iic_booking.users.models.portal_migration import (
    LegacyBookingMigrationBatch,
    MigrationNotificationBatch,
    MigrationNotificationRecipient,
    MigrationNotificationStatus,
    MigrationNotificationTemplate,
    PortalMigrationState,
)

logger = logging.getLogger(__name__)


def _deployment_is_production() -> bool:
    env = str(getattr(settings, "DEPLOYMENT_ENVIRONMENT", "") or "").upper()
    return env in {"PRODUCTION", "PROD"}


def migration_email_context(user=None, **overrides) -> dict[str, Any]:
    state = PortalMigrationState.get_solo()
    opens = state.migration_start_at or state.booking_opens_at
    if opens:
        local = timezone.localtime(opens) if timezone.is_aware(opens) else opens
        migration_datetime = local.strftime("%d %B %Y, %H:%M %Z").strip()
    else:
        migration_datetime = "[CONFIGURED MIGRATION DATE/TIME]"
    ctx = {
        "user_name": user_display_name(user) if user is not None else "User",
        "new_portal_url": state.new_portal_url or getattr(settings, "FRONTEND_URL", "") or "",
        "migration_datetime": migration_datetime,
        "support_email": getattr(settings, "SUPPORT_EMAIL", "") or "",
        "support_phone": getattr(settings, "SUPPORT_PHONE", "") or "",
        "portal_name": "IIC Booking Portal",
    }
    ctx.update({k: v for k, v in overrides.items() if v is not None})
    return ctx


def select_notification_candidates(users: Iterable[User] | None = None) -> dict[str, Any]:
    """Classify recipients; never invent roles. Report unsupported/ambiguous."""
    qs = users if users is not None else User.objects.filter(is_active=True).exclude(email="")
    if hasattr(qs, "iterator"):
        iterable = qs.iterator(chunk_size=500)
    else:
        iterable = qs

    selected: list[dict] = []
    skipped: list[dict] = []
    invalid_email: list[dict] = []
    seen_emails: set[str] = set()
    duplicate_email: list[dict] = []
    by_template = {t: 0 for t in MigrationNotificationTemplate.values}

    for user in iterable:
        email = (getattr(user, "email", "") or "").strip()
        if not email or "@" not in email:
            invalid_email.append({"user_id": user.pk, "reason": "invalid_email"})
            continue
        email_key = email.lower()
        template, role = classify_migration_template(user)
        if not template:
            skipped.append({"user_id": user.pk, "reason": role, "email_present": True})
            continue
        if email_key in seen_emails:
            duplicate_email.append({"user_id": user.pk, "email": email_key, "template": template})
            continue
        seen_emails.add(email_key)
        selected.append(
            {
                "user_id": user.pk,
                "email": email,
                "role": role,
                "template": template,
            }
        )
        by_template[template] = by_template.get(template, 0) + 1

    return {
        "total_recipients": len(selected),
        "by_template": by_template,
        "faculty": by_template.get(MigrationNotificationTemplate.FACULTY_MIGRATION, 0),
        "students": by_template.get(MigrationNotificationTemplate.STUDENT_MIGRATION, 0),
        "oic": by_template.get(MigrationNotificationTemplate.OIC_MIGRATION, 0),
        "admin": by_template.get(MigrationNotificationTemplate.ADMIN_MIGRATION, 0),
        "skipped": len(skipped),
        "invalid_email": len(invalid_email),
        "duplicate_email": len(duplicate_email),
        "selected": selected,
        "skipped_rows": skipped[:100],
        "invalid_email_rows": invalid_email[:50],
        "duplicate_email_rows": duplicate_email[:50],
    }


@transaction.atomic
def create_notification_batch(
    *,
    migration_batch: LegacyBookingMigrationBatch | None = None,
    dry_run: bool = True,
    created_by=None,
    users: Iterable[User] | None = None,
) -> tuple[MigrationNotificationBatch, dict[str, Any]]:
    """Create batch + recipient rows. dry_run=True never queues SMTP."""
    if _deployment_is_production() and not dry_run:
        raise RuntimeError("Refusing to create live migration notifications in PRODUCTION from Phase 8C tools.")

    report = select_notification_candidates(users)
    batch = MigrationNotificationBatch.objects.create(
        migration_batch=migration_batch,
        dry_run=dry_run,
        status="DRY_RUN" if dry_run else "PENDING",
        created_by=created_by,
        counts={
            "total_recipients": report["total_recipients"],
            "faculty": report["faculty"],
            "students": report["students"],
            "oic": report["oic"],
            "admin": report["admin"],
            "skipped": report["skipped"],
            "invalid_email": report["invalid_email"],
            "duplicate_email": report["duplicate_email"],
        },
        notes="Phase 8C migration notification batch",
    )
    for row in report["selected"]:
        MigrationNotificationRecipient.objects.create(
            batch=batch,
            user_id=row["user_id"],
            recipient_email=row["email"],
            role=row["role"],
            template=row["template"],
            status=MigrationNotificationStatus.PENDING,
        )
    for row in report["skipped_rows"]:
        # skip rows without forcing fake emails
        pass
    return batch, report


def queue_notification_batch(batch: MigrationNotificationBatch, *, force: bool = False) -> dict[str, Any]:
    """Mark recipients QUEUED and enqueue Celery tasks. No SMTP inside this call."""
    if batch.dry_run and not force:
        return {"queued": 0, "skipped": batch.recipients.count(), "reason": "dry_run"}
    if _deployment_is_production():
        raise RuntimeError("Refusing to queue production migration emails in Phase 8C.")

    from iic_booking.users.tasks import send_migration_notification_recipient

    queued = 0
    skipped = 0
    now = timezone.now()
    for rec in batch.recipients.select_related("user").filter(
        status__in=[MigrationNotificationStatus.PENDING, MigrationNotificationStatus.FAILED]
    ):
        # Idempotency: already SENT in this batch → skip
        if rec.status == MigrationNotificationStatus.SENT:
            skipped += 1
            continue
        rec.status = MigrationNotificationStatus.QUEUED
        rec.queued_at = now
        rec.save(update_fields=["status", "queued_at"])
        send_migration_notification_recipient.delay(rec.id)
        queued += 1
    batch.status = "QUEUED"
    batch.activated_at = now
    batch.save(update_fields=["status", "activated_at"])
    return {"queued": queued, "skipped": skipped, "batch_id": batch.id}


def deliver_notification_recipient(recipient_id: int) -> dict[str, Any]:
    """Actual SMTP send for one recipient (Celery worker). Staging/Mailpit only in 8C."""
    if _deployment_is_production():
        raise RuntimeError("Production migration email delivery blocked.")

    rec = MigrationNotificationRecipient.objects.select_related("user", "batch").get(pk=recipient_id)
    if rec.batch.dry_run:
        rec.status = MigrationNotificationStatus.SKIPPED
        rec.failure_reason = "dry_run"
        rec.save(update_fields=["status", "failure_reason"])
        return {"status": "SKIPPED", "reason": "dry_run"}
    if rec.status == MigrationNotificationStatus.SENT:
        return {"status": "SENT", "idempotent": True}

    try:
        ctx = migration_email_context(rec.user)
        content = build_migration_email(rec.template, **ctx)
        from django.core.mail import EmailMultiAlternatives
        from iic_booking.users.test_accounts import redirect_email_address

        delivery, subject = redirect_email_address(rec.recipient_email, subject=content.subject)
        if not delivery:
            delivery = [rec.recipient_email]
        msg = EmailMultiAlternatives(
            subject=subject,
            body=content.text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=delivery,
        )
        msg.attach_alternative(content.html_body, "text/html")
        msg.send(fail_silently=False)
        rec.status = MigrationNotificationStatus.SENT
        rec.sent_at = timezone.now()
        rec.failure_reason = ""
        rec.save(update_fields=["status", "sent_at", "failure_reason"])
        return {"status": "SENT", "recipient_id": rec.id}
    except Exception as exc:
        rec.status = MigrationNotificationStatus.FAILED
        rec.retry_count = (rec.retry_count or 0) + 1
        rec.failure_reason = str(exc)[:1000]
        rec.save(update_fields=["status", "retry_count", "failure_reason"])
        logger.exception("migration notification failed id=%s", recipient_id)
        return {"status": "FAILED", "error": str(exc)}


def preview_templates() -> dict[str, Any]:
    out = {}
    for code in MigrationNotificationTemplate.values:
        ctx = preview_sample_context(code)
        content = build_migration_email(code, **ctx)
        out[code] = {
            "subject": content.subject,
            "preheader": content.preheader,
            "html_length": len(content.html_body),
            "text_excerpt": content.text_body[:240],
            "sample_context": ctx,
        }
    return out
