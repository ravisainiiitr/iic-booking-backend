# Phase AI.2 — IIC Research Copilot Knowledge Engine (Hybrid RAG)

**Status:** Implemented  
**Depends on:** AI.1 Conversation Framework  
**STOP:** No tool execution, bookings, or portal data mutation.

## Architecture

Hybrid retrieval sits between the user question and the LLM. The model reasons over retrieved institute knowledge; it is not the primary knowledge source.

```
User Question
    → Intent Detection
    → Permission Check (role + department + security level)
    → Structured Search (equipment / statuses / policies from portal metadata)
    → Vector Search (embeddings)
    → Keyword Search (chunk + title + tags)
    → Re-ranking (dedupe + multi-channel boost)
    → Context Builder + Citations
    → LLM (streaming unchanged from AI.1)
```

### Components

| Layer | Module | Notes |
|-------|--------|-------|
| Models | `research_copilot.models` | `KnowledgeDocument`, `KnowledgeChunk`, `EmbeddingJob`, `SearchQueryLog` |
| Embeddings | `services/embeddings.py` | Provider interface: `openai`, `local` (hash), `auto` |
| Vector store | `services/vector_store.py` | Default `django_orm` (JSON embeddings). Stubs: pgvector, Qdrant, Milvus, Chroma |
| Ingestion | `services/ingestion.py` | Chunk, hash, version, incremental re-index |
| Permissions | `services/knowledge_permissions.py` | Students cannot retrieve admin/operator/dept SOPs |
| RAG | `services/rag.py` | Hybrid pipeline + citation objects |
| Seed | `services/seed_knowledge.py` | Baseline FAQs / guides / policies |
| Admin API | `knowledge_views.py` | Knowledge Center CRUD, rebuild, analytics |
| Conversation | `services/conversation.py` | Retrieves before reply; Sources footer; gaps on low confidence |

### Settings

| Setting | Default | Purpose |
|---------|---------|---------|
| `RESEARCH_COPILOT_ENABLED` | `false` | Master flag (AI.1) |
| `RESEARCH_COPILOT_EMBEDDING_PROVIDER` | `auto` | `auto` \| `openai` \| `local` |
| `RESEARCH_COPILOT_EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI model when used |
| `RESEARCH_COPILOT_VECTOR_STORE` | `django_orm` | Portable default; swap without app lock-in |
| `RESEARCH_COPILOT_VECTOR_SCAN_LIMIT` | `2000` | ORM cosine scan cap |

## Data Flow

1. Admin creates or seeds a document (`POST /knowledge/documents/` or `seed_research_copilot_knowledge`).
2. Ingestion splits text, embeds chunks, stores vectors via the active provider.
3. User asks a question in Copilot.
4. `retrieve()` filters by `allowed_security_levels(role_bucket)` and department scope.
5. Citations are attached to the assistant `Message` and rendered as a **Sources** footer (never fabricated).
6. Low retrieval confidence logs `SearchQueryLog.low_confidence` and may create a `KnowledgeGap` with a suggested FAQ draft — never auto-published.

## Embedding Strategy

- **Versioning:** `embedding_version` / `embedding_model` on documents and chunks.
- **Incremental:** content hash change → `index_status=stale` → re-index that document only.
- **Rebuild:** `POST /knowledge/rebuild-index/` re-embeds all active documents.
- **Background:** `EmbeddingJob` rows record index / reindex / rebuild_all outcomes (sync today; Celery-ready).
- **Dev:** `local` provider uses deterministic hash vectors so CI works without OpenAI.

## Security

| Role bucket | Max security level |
|-------------|-------------------|
| student / faculty / external | `authenticated` |
| operator | `operator` |
| dept_admin | `dept_admin` |
| admin | `admin` |

Students **cannot** retrieve: Admin-only SOPs, operator manuals, internal deployment docs, support runbooks, or cross-department operator/dept docs.

Knowledge Center APIs require institute admin (`IsCopilotKnowledgeAdmin`).

## Index Maintenance

```bash
python manage.py seed_research_copilot_knowledge
python manage.py seed_research_copilot_knowledge --force   # re-index seed titles
```

Admin UI: **Admin Settings → Knowledge Center**

- Documents + filters (category, index status, search)
- Seed baseline / Rebuild index / Reindex document
- Jobs list, embedding health, top queries, knowledge gaps

## Recovery

| Symptom | Action |
|---------|--------|
| Failed documents | Open Knowledge Center → filter `index_status=failed` → Reindex |
| Stale after content edit | Reindex document or rebuild all |
| Embedding provider outage | Set `RESEARCH_COPILOT_EMBEDDING_PROVIDER=local` temporarily; keyword + structured search still run |
| Wrong answers / missing docs | Review Knowledge Gaps; add FAQ manually — never auto-modify documentation |
| Performance regression | Warm path should stay &lt;300 ms for retrieval; reduce `VECTOR_SCAN_LIMIT` or migrate to pgvector/Qdrant |

## Performance targets

| Path | Target |
|------|--------|
| Cold search | &lt; 1 s |
| Warm search | &lt; 300 ms |
| LLM | Streaming (AI.1) unchanged |

## Admin API map

| Method | Path | Auth |
|--------|------|------|
| GET/POST | `/api/v1/research-copilot/knowledge/documents/` | Admin |
| GET/PATCH/DELETE | `/api/v1/research-copilot/knowledge/documents/<id>/` | Admin |
| POST | `.../reindex/` | Admin |
| POST | `/knowledge/rebuild-index/` | Admin |
| POST | `/knowledge/seed/` | Admin |
| GET | `/knowledge/jobs/` | Admin |
| GET | `/knowledge/analytics/` | Admin |
| POST | `/knowledge/search/` | Authenticated (+ feature flag) |

## Testing

```bash
pytest iic_booking/research_copilot/tests/test_knowledge_ai2.py -q
```

Covers: permission filtering, equipment/policy search, seed + incremental index, admin APIs, conversation citations.

## Out of scope (STOP)

- Tool execution / bookings / wallet mutations
- Auto-editing documentation from gaps
- Research publications / vendor manuals (future sources)
