"""Lightweight intent detection for retrieval routing (Phase AI.2)."""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("equipment", re.compile(r"\b(equipment|instrument|fesem|sem|tem|afm|xrd|xps|book|slot|capability|sample prep)\b", re.I)),
    ("policy", re.compile(r"\b(policy|cancel|refund|urgent|approval|grant|wallet|recharge|charge|fee)\b", re.I)),
    ("status", re.compile(r"\b(status|sample_accepted|hold|where is my|results|accepted)\b", re.I)),
    ("remote_analysis", re.compile(r"\b(remote analysis|raa|guacamole|rdp|analysis workstation)\b", re.I)),
    ("dsa", re.compile(r"\b(dsa|department sync|heartbeat|sync agent|equipment pc|wizard)\b", re.I)),
    ("deployment", re.compile(r"\b(deploy|provision|install|enrollment|zero-touch)\b", re.I)),
    ("troubleshooting", re.compile(r"\b(error|fail|offline|cannot|won't|troubleshoot|fix)\b", re.I)),
    ("documentation", re.compile(r"\b(guide|manual|sop|faq|how do i|documentation)\b", re.I)),
]


def detect_intent(query: str) -> str:
    q = query or ""
    for name, pat in _PATTERNS:
        if pat.search(q):
            return name
    return "general"
