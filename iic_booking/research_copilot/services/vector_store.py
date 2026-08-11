"""Vector store abstraction — Django ORM JSON store by default; adapters for pgvector/Qdrant/etc."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Q, QuerySet

from iic_booking.research_copilot.models import KnowledgeChunk, KnowledgeDocument, DocumentStatus
from iic_booking.research_copilot.services.embeddings import cosine_similarity
from iic_booking.research_copilot.services.knowledge_permissions import allowed_security_levels


@dataclass
class VectorHit:
    chunk_id: str
    document_id: str
    score: float
    content: str
    title: str
    metadata: dict


class VectorStore(ABC):
    name: str = "base"

    @abstractmethod
    def upsert_chunk(self, chunk: KnowledgeChunk) -> None:
        raise NotImplementedError

    @abstractmethod
    def delete_document(self, document_id) -> None:
        raise NotImplementedError

    @abstractmethod
    def similarity_search(
        self,
        *,
        query_vector: list[float],
        allowed_levels: set[str],
        department_id: int | None,
        limit: int = 8,
    ) -> list[VectorHit]:
        raise NotImplementedError


class DjangoORMVectorStore(VectorStore):
    """
    Portable default store: embeddings in KnowledgeChunk.embedding JSONField.
    Suitable for SQLite/Postgres without pgvector extension.
    """

    name = "django_orm"

    def upsert_chunk(self, chunk: KnowledgeChunk) -> None:
        # Already persisted on the model — no external index.
        chunk.save(update_fields=["embedding", "embedding_model", "embedding_version"])

    def delete_document(self, document_id) -> None:
        KnowledgeChunk.objects.filter(document_id=document_id).delete()

    def similarity_search(
        self,
        *,
        query_vector: list[float],
        allowed_levels: set[str],
        department_id: int | None,
        limit: int = 8,
    ) -> list[VectorHit]:
        qs: QuerySet = KnowledgeChunk.objects.select_related("document").filter(
            document__status=DocumentStatus.ACTIVE,
            document__security_level__in=list(allowed_levels),
        ).exclude(embedding=[])
        # Department filter: include global (null) + matching dept
        if department_id is not None:
            qs = qs.filter(Q(document__department_id__isnull=True) | Q(document__department_id=department_id))

        hits: list[VectorHit] = []
        # Cap scan for performance on large corpora
        max_scan = int(getattr(settings, "RESEARCH_COPILOT_VECTOR_SCAN_LIMIT", 2000))
        for chunk in qs.order_by("-created_at")[:max_scan]:
            emb = chunk.embedding or []
            if not isinstance(emb, list) or not emb:
                continue
            score = cosine_similarity(query_vector, emb)
            doc = chunk.document
            hits.append(
                VectorHit(
                    chunk_id=str(chunk.id),
                    document_id=str(doc.id),
                    score=score,
                    content=chunk.content,
                    title=doc.title,
                    metadata={
                        "category": doc.category,
                        "security_level": doc.security_level,
                        "external_url": doc.external_url,
                        "source_uri": doc.source_uri,
                        "equipment_id": doc.equipment_id,
                        "version": doc.version,
                    },
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]


class PgVectorStore(VectorStore):
    """Placeholder adapter — enable when pgvector extension + column are provisioned."""

    name = "pgvector"

    def upsert_chunk(self, chunk: KnowledgeChunk) -> None:
        DjangoORMVectorStore().upsert_chunk(chunk)

    def delete_document(self, document_id) -> None:
        DjangoORMVectorStore().delete_document(document_id)

    def similarity_search(self, **kwargs) -> list[VectorHit]:
        return DjangoORMVectorStore().similarity_search(**kwargs)


class QdrantStore(VectorStore):
    name = "qdrant"

    def upsert_chunk(self, chunk: KnowledgeChunk) -> None:
        raise NotImplementedError("Configure Qdrant client via RESEARCH_COPILOT_VECTOR_STORE=qdrant (future)")

    def delete_document(self, document_id) -> None:
        raise NotImplementedError

    def similarity_search(self, **kwargs) -> list[VectorHit]:
        raise NotImplementedError


def get_vector_store() -> VectorStore:
    name = (getattr(settings, "RESEARCH_COPILOT_VECTOR_STORE", None) or "django_orm").lower()
    if name == "pgvector":
        return PgVectorStore()
    if name == "qdrant":
        return QdrantStore()
    if name in {"milvus", "chroma"}:
        # Fall back until adapters are provisioned
        return DjangoORMVectorStore()
    return DjangoORMVectorStore()
