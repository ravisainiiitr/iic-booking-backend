# Copilot V2 Implementation Plan

## Decision lock
Hybrid phased (Option 3): Phase A deterministic reads first; Phase B/C mutations feature-flagged OFF.

## Architecture
User → Intent resolver → deterministic domain tools (preferred) → structured cards  
Complex research → RAG → LLM (optional)  
Mutations (later) → confirmation → existing domain services (never direct DB)

## Phases
- **A (now):** equipment, availability, pricing, user context, RAG, rate-limit split, FE cards
- **B (later):** create/cancel/reschedule/modify booking via domain APIs
- **C (later):** wallet recharge / credit request

## Non-goals
No rewrite of booking/wallet/RA business logic. No enabling mutation flags in this release.
