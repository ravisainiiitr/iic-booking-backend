from celery import shared_task

from .client import WhatsAppError


@shared_task(name="whatsapp.deliver", bind=True, max_retries=5, ignore_result=True)
def deliver_whatsapp(self, message_id: int) -> str:
    """Send one queued WhatsApp message; network errors and Meta 5xx/429 are retried with backoff."""
    from .service import deliver

    try:
        return deliver(message_id)
    except WhatsAppError as exc:
        raise self.retry(exc=exc, countdown=min(600, 30 * 2 ** self.request.retries)) from exc
