# Copilot V2 Phase D — Unanswered Query Pipeline

## Goal

Never hallucinate portal facts. Capture gaps for continuous improvement.

## Flow

1. Deterministic tool / RAG returns empty or low confidence.
2. `unanswered.log_unanswered(...)` writes `KnowledgeGap` row:
   - `query_summary`
   - `reason` (e.g. `RAG_EMPTY`, `EQUIPMENT_NOT_FOUND`, `NO_CAPABILITY_MATCH`)
   - `suggested_faq` metadata (intent, confidence, tools)
3. User receives soft clarification (no invented equipment/slots/prices).
4. Main Admin / OIC review via existing knowledge admin (`KnowledgeGap` + knowledge views).

## Admin review queue

Existing Research Copilot knowledge admin surfaces recent gaps (unchanged schema — no migration).

## Feedback loop

Frontend already supports 👍 / 👎 on answers. Negative feedback should be reviewed alongside unanswered rows when triaging RAG/document uploads.

## OIC knowledge management (existing)

Officers continue to upload/replace/archive documents with equipment metadata through the knowledge portal. Phase D prefers:

**CURRENT + APPROVED + EQUIPMENT-SPECIFIC** retrieval already enforced by RAG filters where present.
