"""Research Copilot constants."""

from __future__ import annotations

# Marker the model may emit when human help is needed (stripped from user-visible reply).
ESCALATE_MARKER = "ESCALATE_HUMAN"

# Soft confidence below which we set escalate_hint and record a knowledge gap.
CONFIDENCE_ESCALATE_THRESHOLD = 0.45

# Suggested prompts by coarse role bucket (AI.1 static; AI.3 will enrich with tools).
SUGGESTED_PROMPTS = {
    "student": [
        "Show my upcoming bookings",
        "What is my wallet balance?",
        "How do I recharge my wallet?",
        "I need FESEM tomorrow — what are my options?",
        "Results of my last booking",
    ],
    "faculty": [
        "Show my upcoming bookings",
        "What is my wallet balance?",
        "How do I recharge my wallet?",
        "Manage my students and spending limits",
        "Is XRD free next Monday morning?",
    ],
    "operator": [
        "Today's bookings on my equipment",
        "Pending approvals on my equipment",
        "Urgent requests queue on my equipment",
        "Waitlist queue on my equipment",
    ],
    "dept_admin": [
        "Today's bookings on my equipment",
        "Pending approvals on my equipment",
        "Is DSA online for my department?",
        "Open reports",
    ],
    "admin": [
        "Today's bookings on my equipment",
        "Pending approvals on my equipment",
        "Open reports",
        "What can you do?",
    ],
    "external": [
        "Show my upcoming bookings",
        "What is my wallet balance?",
        "How do I recharge my wallet?",
        "I need FESEM tomorrow — what are my options?",
        "Where are my invoices?",
    ],
    "default": [
        "Show my upcoming bookings",
        "What is my wallet balance?",
        "How do I recharge my wallet?",
        "I need FESEM tomorrow — what are my options?",
        "I want to raise a support ticket.",
    ],
}
