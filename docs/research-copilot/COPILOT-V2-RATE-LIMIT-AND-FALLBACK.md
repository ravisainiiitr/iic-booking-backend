# Copilot V2 Rate Limit and Fallback

## Problem
Single `research_copilot_user` (60/h) throttled entire chat turns, including deterministic slot searches that need no LLM.

## Design
| Scope | Purpose | Default |
|-------|---------|---------|
| `research_copilot_read` | Chat + deterministic reads | 300/hour |
| `research_copilot_llm` | LLM-bearing turns (internal quota) | 60/hour |
| `research_copilot_mutation` | Phase B/C executes | 20/hour |
| `research_copilot_anon` | Public ask | 60/hour |
| `research_copilot_tool` | Legacy `/tools/execute/` | 120/hour |

## Fallback order
1. Deterministic domain tool  
2. Short-lived safe cache (availability TTL ≤ 60s)  
3. RAG  
4. LLM  
5. Support CTA  

Never: LLM failure → “use normal portal” when deterministic path can answer.
