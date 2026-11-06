from celery import shared_task

from .client import WhatsAppError


@shared_task(name="whatsapp.deliver", bind=True, max_retries=5, ignore_result=True)
def deliver_whatsapp(self, message_id: int) -> str:
    """Send one queued WhatsApp message; network errors and Meta 5xx/429 are retried with backoff."""
    from .service import deliver

    try:
        return deliver(message_id)
    except WhatsAppError as exc:
        if self.request.retries >= self.max_retries:
            from .service import mark_retries_exhausted

            mark_retries_exhausted(message_id, str(exc))
            return "failed"
        raise self.retry(exc=exc, countdown=min(600, 30 * 2 ** self.request.retries)) from exc


@shared_task(name="whatsapp.purge_expired", ignore_result=True)
def purge_expired_whatsapp() -> dict:
    """Daily: applies the retention period to incoming messages and webhook records."""
    from .service import purge_expired

    return purge_expired()
