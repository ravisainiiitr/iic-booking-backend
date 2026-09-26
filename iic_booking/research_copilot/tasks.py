"""Research Copilot background jobs (manual extraction and index rebuilds)."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="research_copilot.process_manual", acks_late=True, soft_time_limit=1800, time_limit=2100)
def process_manual_task(document_id: str) -> dict:
    from iic_booking.research_copilot.services.manuals import process_manual

    result = process_manual(document_id)
    logger.info("copilot manual processed document=%s result=%s", document_id, result)
    return result


@shared_task(name="research_copilot.index_document", acks_late=True, soft_time_limit=1800, time_limit=2100)
def index_document_task(document_id: str) -> dict:
    from iic_booking.research_copilot.models import KnowledgeDocument
    from iic_booking.research_copilot.services.ingestion import index_document

    doc = KnowledgeDocument.objects.filter(id=document_id).first()
    if doc is None:
        return {"ok": False, "error": "NOT_FOUND"}
    job = index_document(doc)
    return {"ok": job.status == "indexed", "job_id": str(job.id)}


@shared_task(name="research_copilot.rebuild_all_indexes", acks_late=True, soft_time_limit=7200, time_limit=7500)
def rebuild_all_indexes_task() -> dict:
    from iic_booking.research_copilot.services.ingestion import rebuild_all_indexes

    result = rebuild_all_indexes()
    logger.info("copilot index rebuild finished %s", result)
    return result
