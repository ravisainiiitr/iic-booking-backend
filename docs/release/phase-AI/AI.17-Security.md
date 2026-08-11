# AI.17 — Security

**Date:** 2026-08-11

## Authoritative gates

- Backend `RESEARCH_COPILOT_ENABLED`
- Optional `RESEARCH_COPILOT_PILOT_EMAILS`
- Frontend Vite flag is a soft gate only
- Android uses bootstrap `enabled` from backend

## Authorization

- Every Copilot request runs as the authenticated user
- Conversations are user-scoped (`get_object_or_404(..., user=request.user)`)
- Tools call existing permission-aware portal services
- Mutations require confirmation cards; Copilot does not write booking rows directly

## Prompt injection

Untrusted document/context is wrapped (`<<<UNTRUSTED_DOCUMENT_CONTEXT>>>`). Document instructions cannot override system rules, reveal secrets, or bypass permissions.

## Tool allowlist

Only registered tools execute. The model cannot invoke arbitrary Python callables.

## Audit (no secrets)

Logged: conversation create, replies, tool execute/deny, feedback, busy/unavailable categories.  
Never logged: API keys, passwords, access tokens, session tokens.

## Provider diagnostics

`GET /api/v1/research-copilot/llm/health/` is admin-only and returns provider/model/status/concurrency — **never** API keys or base URLs.
