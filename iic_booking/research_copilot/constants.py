"""Research Copilot constants."""

from __future__ import annotations

# Marker the model may emit when human help is needed (stripped from user-visible reply).
ESCALATE_MARKER = "ESCALATE_HUMAN"

# Soft confidence below which we set escalate_hint and record a knowledge gap.
CONFIDENCE_ESCALATE_THRESHOLD = 0.45

# Suggested prompts by coarse role bucket (AI.1 static; AI.3 will enrich with tools).
SUGGESTED_PROMPTS = {
    "student": [
        "I need FESEM tomorrow — what are my options?",
        "What does SAMPLE_ACCEPTED mean?",
        "How do I check my wallet balance?",
        "Help me choose between TEM and FESEM.",
    ],
    "faculty": [
        "Show how students join my wallet.",
        "What equipment is under maintenance?",
        "Explain credit facility for my lab.",
        "How do I approve a recharge request?",
    ],
    "operator": [
        "What does HOLD mean on a booking?",
        "How do I accept a sample?",
        "Remote Analysis won't connect — troubleshoot.",
        "Where are operator manuals?",
    ],
    "dept_admin": [
        "Is DSA online for my department?",
        "How do I provision Equipment PC?",
        "Explain Trusted Auto-Approve.",
        "Create a support ticket for DSA sync.",
    ],
    "admin": [
        "Summarize Research Copilot capabilities.",
        "How does Device Provisioning work?",
        "Where are release notes for v2.5?",
        "Escalate a user to support.",
    ],
    "external": [
        "How do I book equipment as an external user?",
        "What documents do I need for billing?",
        "Where are my results?",
        "Talk to support.",
    ],
    "default": [
        "How do I book equipment?",
        "Where is my sample?",
        "Wallet and recharge help",
        "Talk to a human / create a ticket",
    ],
}
