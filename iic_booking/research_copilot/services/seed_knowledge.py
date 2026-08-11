"""Seed baseline IIC knowledge articles for Research Copilot (Phase AI.2)."""

from __future__ import annotations

from iic_booking.research_copilot.models import DocumentCategory, KnowledgeDocument, SecurityLevel
from iic_booking.research_copilot.services.ingestion import upsert_document

SEED_ARTICLES: list[dict] = [
    {
        "title": "How to Book Equipment",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["booking", "slots", "faq"],
        "external_url": "/equipments",
        "content_text": """
To book equipment on the IIC Equipment Booking Portal:
1. Sign in and open Equipments.
2. Select the instrument matching your measurement need.
3. Review specifications, charges, and important instructions.
4. Choose an available slot and submit the booking.
5. Ensure wallet balance or approval workflow is satisfied.
Never invent slot availability — always check the equipment page.
""",
    },
    {
        "title": "Booking Status Meanings",
        "category": DocumentCategory.FAQ,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["status", "SAMPLE_ACCEPTED", "HOLD"],
        "external_url": "/bookings",
        "content_text": """
Common booking and sample statuses:
- PENDING: awaiting approval or operator action.
- SAMPLE_ACCEPTED: operator accepted the sample; experiment can proceed.
- HOLD: paused pending clarification, payment, or operator decision.
- COMPLETED: experiment finished; results may be available.
- CANCELLED: booking cancelled per institute cancellation policy.
""",
    },
    {
        "title": "Wallet, Recharge and Grants",
        "category": DocumentCategory.POLICY,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["wallet", "recharge", "grant", "credit"],
        "external_url": "/wallet",
        "content_text": """
Wallet guidance:
- Check balance under Wallet.
- Students typically use faculty/department wallets; join requests may be required.
- Recharge requests follow department approval workflows.
- Credit facility and grant expiry are managed per institute wallet rules.
- Refunds follow finance/support processes — escalate if needed.
The Copilot will not invent balances; open Wallet for live figures.
""",
    },
    {
        "title": "FESEM vs TEM — Quick Advisor",
        "category": DocumentCategory.EQUIPMENT,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["fesem", "tem", "advisor", "elemental mapping"],
        "content_text": """
Guidance (not a substitute for OIC advice):
- FESEM with EDS: surface morphology and elemental mapping; often suitable for metals, coatings, polymers (with care).
- TEM: internal nanostructure, crystallography, higher resolution; sample prep is more demanding (thin specimens).
- For grain size + elemental mapping on stainless steel, FESEM+EDS is commonly recommended first.
Always confirm sample preparation requirements and charges on the equipment page.
""",
    },
    {
        "title": "Remote Analysis Assistant Overview",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["remote analysis", "raa", "software"],
        "external_url": "/remote-analysis",
        "content_text": """
Remote Analysis lets users launch an analysis workstation session from a booking.
- Check installed software inventory on the workstation page.
- Session time remaining is shown in the Remote Analysis UI.
- Upload processed results back through the portal workflow.
- If connection fails, verify agent heartbeat and portal reachability; escalate with diagnostics if needed.
""",
    },
    {
        "title": "DSA and Equipment PC Zero-Touch (Department Admin)",
        "category": DocumentCategory.DEPLOYMENT,
        "security_level": SecurityLevel.DEPT_ADMIN,
        "tags": ["dsa", "equipment pc", "provisioning", "heartbeat"],
        "content_text": """
Department Sync Agent (DSA):
- Install → Portal Login → Trusted Auto-Approve → Finish.
- Verify Online status and heartbeat in Device Provisioning.
Equipment PC Wizard:
- Discover DSA → Login → Select unassigned equipment → READY.
No enrollment keys or UUIDs on the standard path.
Troubleshooting: check Lab LAN, DSA service, and portal Trusted policy.
""",
    },
    {
        "title": "Operator Sample Handling SOP (Summary)",
        "category": DocumentCategory.SOP,
        "security_level": SecurityLevel.OPERATOR,
        "tags": ["operator", "sample", "sop"],
        "content_text": """
Operator guidance (summary):
- Accept samples only after identity and booking verification.
- Update status to SAMPLE_ACCEPTED when ready to run.
- Use HOLD when clarification or payment is required.
- Upload results and complete booking per lab SOP.
Full operator manuals may be department-specific and higher security.
""",
    },
    {
        "title": "Cancellation and Urgent Booking Policy (Summary)",
        "category": DocumentCategory.POLICY,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["cancellation", "urgent", "policy"],
        "content_text": """
Cancellation: follow portal cancellation rules for your booking state; wallet adjustments may apply.
Urgent booking: use Urgent Request flows when no suitable slot exists; approvals follow institute policy.
Maintenance and disruption: unavailable equipment shows maintenance or disruption messaging on the equipment page.
""",
    },
    {
        "title": "Admin Runbook — Research Copilot Knowledge Index",
        "category": DocumentCategory.DEPLOYMENT,
        "security_level": SecurityLevel.ADMIN,
        "tags": ["admin", "rag", "index", "runbook"],
        "content_text": """
Admin-only: Knowledge Center operations.
- Upload or create documents; set security level carefully.
- Rebuild index after bulk updates.
- Review Failed documents and Knowledge Gaps for FAQ candidates.
- Never auto-publish suggested FAQs without human review.
""",
    },
]


def seed_baseline_knowledge(*, force: bool = False) -> dict:
    created = updated = skipped = 0
    for article in SEED_ARTICLES:
        existing = KnowledgeDocument.objects.filter(title=article["title"]).first()
        if existing and not force:
            skipped += 1
            continue
        upsert_document(
            title=article["title"],
            content_text=article["content_text"],
            category=article["category"],
            security_level=article["security_level"],
            tags=article.get("tags") or [],
            external_url=article.get("external_url") or "",
            source_type="article",
            source_uri=f"seed://{article['title']}",
            document_id=existing.id if existing else None,
            index_now=True,
        )
        if existing:
            updated += 1
        else:
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}
