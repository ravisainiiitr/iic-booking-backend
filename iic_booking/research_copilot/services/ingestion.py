"""Document chunking and ingestion (Phase AI.2)."""

from __future__ import annotations

import hashlib
import re
from django.db import transaction
from django.utils import timezone

from iic_booking.research_copilot.models import (
    DocumentStatus,
    EmbeddingJob,
    IndexStatus,
    KnowledgeChunk,
    KnowledgeDocument,
)
from iic_booking.research_copilot.services.embeddings import get_embedding_provider
from iic_booking.research_copilot.services.vector_store import get_vector_store

_WS = re.compile(r"\s+")


def estimate_tokens(text: str) -> int:
    return max(1, len((text or "").split()))


def split_text(text: str, *, chunk_size: int = 500, overlap: int = 80) -> list[str]:
    """Approximate token chunks by words with overlap."""
    words = (text or "").split()
    if not words:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(len(words), start + chunk_size)
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        start = max(end - overlap, start + 1)
    return chunks


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@transaction.atomic
def upsert_document(
    *,
    title: str,
    content_text: str,
    category: str = "other",
    security_level: str = "authenticated",
    source_type: str = "markdown",
    version: str = "1.0",
    language: str = "en",
    tags: list | None = None,
    department_id: int | None = None,
    equipment_id: int | None = None,
    source_uri: str = "",
    external_url: str = "",
    created_by=None,
    document_id=None,
    index_now: bool = True,
) -> KnowledgeDocument:
    text = _WS.sub(" ", (content_text or "").strip())
    ch = content_hash(text)
    if document_id:
        doc = KnowledgeDocument.objects.select_for_update().get(id=document_id)
        doc.title = title[:512]
        doc.content_text = text
        doc.category = category
        doc.security_level = security_level
        doc.source_type = source_type
        doc.version = version[:64]
        doc.language = language[:16]
        doc.tags = tags or []
        doc.department_id = department_id
        doc.equipment_id = equipment_id
        doc.source_uri = (source_uri or "")[:1024]
        doc.external_url = external_url or ""
        if doc.content_hash != ch:
            doc.content_hash = ch
            doc.index_status = IndexStatus.STALE
        doc.status = DocumentStatus.ACTIVE
        doc.save()
    else:
        doc = KnowledgeDocument.objects.create(
            title=title[:512],
            content_text=text,
            content_hash=ch,
            category=category,
            security_level=security_level,
            source_type=source_type,
            version=version[:64],
            language=language[:16],
            tags=tags or [],
            department_id=department_id,
            equipment_id=equipment_id,
            source_uri=(source_uri or "")[:1024],
            external_url=external_url or "",
            created_by=created_by,
            status=DocumentStatus.ACTIVE,
            index_status=IndexStatus.PENDING,
        )
    if index_now:
        index_document(doc)
    return doc


def index_document(doc: KnowledgeDocument) -> EmbeddingJob:
    job = EmbeddingJob.objects.create(
        document=doc,
        job_type="index",
        status=IndexStatus.INDEXING,
    )
    doc.index_status = IndexStatus.INDEXING
    doc.error_message = ""
    doc.save(update_fields=["index_status", "error_message", "updated_at"])

    store = get_vector_store()
    provider = get_embedding_provider()
    job.provider = f"{provider.name}:{provider.version}"
    job.save(update_fields=["provider"])

    try:
        # Replace chunks
        KnowledgeChunk.objects.filter(document=doc).delete()
        parts = split_text(doc.content_text)
        if not parts:
            raise ValueError("empty_document")
        vectors = provider.embed_texts(parts)
        chunks: list[KnowledgeChunk] = []
        for i, (part, vec) in enumerate(zip(parts, vectors)):
            chunk = KnowledgeChunk.objects.create(
                document=doc,
                chunk_index=i,
                content=part,
                token_estimate=estimate_tokens(part),
                metadata={
                    "category": doc.category,
                    "security_level": doc.security_level,
                    "title": doc.title,
                },
                embedding=vec,
                embedding_model=provider.name,
                embedding_version=provider.version,
            )
            store.upsert_chunk(chunk)
            chunks.append(chunk)

        doc.chunk_count = len(chunks)
        doc.embedding_version = provider.version
        doc.index_status = IndexStatus.INDEXED
        doc.indexed_at = timezone.now()
        doc.status = DocumentStatus.ACTIVE
        doc.save(
            update_fields=[
                "chunk_count",
                "embedding_version",
                "index_status",
                "indexed_at",
                "status",
                "updated_at",
            ]
        )
        job.status = IndexStatus.INDEXED
        job.finished_at = timezone.now()
        job.detail = {"chunks": len(chunks)}
        job.save(update_fields=["status", "finished_at", "detail"])
    except Exception as exc:
        doc.index_status = IndexStatus.FAILED
        doc.status = DocumentStatus.FAILED
        doc.error_message = str(exc)[:2000]
        doc.save(update_fields=["index_status", "status", "error_message", "updated_at"])
        job.status = IndexStatus.FAILED
        job.error_message = str(exc)[:2000]
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error_message", "finished_at"])
    return job


def rebuild_all_indexes() -> dict:
    docs = KnowledgeDocument.objects.filter(status__in=[DocumentStatus.ACTIVE, DocumentStatus.FAILED, DocumentStatus.DRAFT])
    ok = fail = 0
    for doc in docs.iterator():
        job = index_document(doc)
        if job.status == IndexStatus.INDEXED:
            ok += 1
        else:
            fail += 1
    return {"indexed": ok, "failed": fail, "total": ok + fail}
