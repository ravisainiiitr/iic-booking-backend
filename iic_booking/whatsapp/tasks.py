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


@shared_task(name="whatsapp.sync_template_statuses", ignore_result=True)
def sync_template_statuses() -> dict:
    """Daily: store what Meta reports about each template (GET only). Does nothing until WHATSAPP_WABA_ID is set."""
    from . import conf, templates
    from .template_client import TemplateApiError, TemplateClient

    if not conf.waba_id():
        return {"skipped": "no WHATSAPP_WABA_ID"}
    client = TemplateClient()
    try:
        templates.check_ready(client)
        plan = templates.sync_statuses(client, actor="daily task")
    except (templates.TemplateConfigError, TemplateApiError) as exc:
        return {"failed": type(exc).__name__}
    return {"at_meta": len(plan.meta_rows), "missing": len(plan.to_submit)}


@shared_task(name="whatsapp.purge_expired", ignore_result=True)
def purge_expired_whatsapp() -> dict:
    """Daily: applies the retention period to incoming messages and webhook records."""
    from .service import purge_expired

    return purge_expired()
