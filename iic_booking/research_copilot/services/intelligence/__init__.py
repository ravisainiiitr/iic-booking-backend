"""
Copilot intelligence layer: intent + entity understanding, structured conversation state, choice
cards, guided portal actions and curated knowledge.

Nothing here mutates portal records. Every change still goes through the existing prepare ->
explicit confirmation -> execute path in `services.v2.mutations`, under the signed-in user.
"""

from __future__ import annotations

from django.conf import settings


def intelligence_enabled() -> bool:
    return bool(getattr(settings, "RESEARCH_COPILOT_INTELLIGENCE_ENABLED", False))


def knowledge_enabled() -> bool:
    return bool(getattr(settings, "RESEARCH_COPILOT_KNOWLEDGE_ENABLED", False))


def actions_enabled() -> bool:
    return intelligence_enabled() and bool(getattr(settings, "RESEARCH_COPILOT_ACTIONS_ENABLED", False))


def conversational_actions_enabled() -> bool:
    return intelligence_enabled() and bool(getattr(settings, "RESEARCH_COPILOT_CONVERSATIONAL_ACTIONS_ENABLED", False))
