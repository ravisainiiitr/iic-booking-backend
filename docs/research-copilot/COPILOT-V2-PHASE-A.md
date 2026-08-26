# Copilot V2 — Phase A

## Goal
Production-quality conversational **read** layer. Operational queries must work when the LLM is unavailable.

## Must work without LLM
Equipment discovery, slots (this week / tomorrow / earliest), cost estimate, my bookings, wallet balance/tx, sample status, results, RA status, affiliations, pending actions, published docs/RAG where indexed.

## Hard requirement
`Search available slots for FESEM this week` → intent + equipment resolve + date window + portal slots + cards. No LLM required.

## Acceptance
See `COPILOT-V2-E2E-TEST.md`. Gate: **PHASE A READY — MUTATIONS REMAIN DISABLED**.
