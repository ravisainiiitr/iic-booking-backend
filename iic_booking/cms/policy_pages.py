"""Website policy pages the payment gateway requires (Razorpay India: About us, Contact us, Pricing, Terms and
conditions, Privacy policy, Refunds policy). Terms, privacy, refunds and about are CMS pages with these slugs; the
footer links each one only once it is published. Contact details and pricing already have their own places
(footer, Analysis Charges page).

``draft_content`` holds the texts drafted on 1 Nov 2026 for approval by Head IIC / SRIC. They are created as
unpublished pages (``seed_policy_pages``) so an admin reviews, edits and publishes them in the CMS.
"""

from __future__ import annotations

POLICY_PAGES = (
    ("terms-and-conditions", "Terms and Conditions"),
    ("privacy-policy", "Privacy Policy"),
    ("refund-policy", "Refund and Cancellation Policy"),
    ("about-us", "About Us"),
)
POLICY_SLUGS = tuple(slug for slug, _ in POLICY_PAGES)

ADDRESS = ("Institute Instrumentation Centre, Indian Institute of Technology Roorkee, Roorkee 247667, "
           "Uttarakhand, India")
CONTACT = f"Contact: iic@iitr.ac.in, 01332-284350, {ADDRESS}."


def _h(text: str, level: int = 2) -> dict:
    return {"type": "heading", "level": level, "text": text}


def _p(text: str) -> dict:
    return {"type": "paragraph", "text": text}


def _ul(*items: str) -> dict:
    return {"type": "list", "ordered": False, "items": list(items)}


def _ol(*items: str) -> dict:
    return {"type": "list", "ordered": True, "items": list(items)}


DRAFT_NOTE = _p("DRAFT for approval by Head IIC / SRIC. Edit this page and publish it once approved.")


def draft_content(slug: str) -> list[dict]:
    if slug == "terms-and-conditions":
        return [DRAFT_NOTE, _ol(
            "The IIC Booking Portal (equip.iitr.ac.in) is operated by the Institute Instrumentation Centre (IIC), "
            f"Indian Institute of Technology Roorkee. {ADDRESS}.",
            "Users register with accurate details. IIC may verify identity and organisation and may refuse or "
            "suspend accounts.",
            "Bookings are for analytical and instrument services at the charges published on the Analysis Charges "
            "page, plus applicable GST. The charges in force when a booking is confirmed apply.",
            "Payment is by the portal wallet (internal users) or online through Razorpay (cards, UPI, net banking). "
            "A booking is confirmed only when payment is received. Card details are handled by Razorpay; IIC does "
            "not store them.",
            "Samples must be prepared, labelled and dispatched as instructed for the instrument. IIC may decline "
            "unsafe or non-conforming samples.",
            "Results are released through the portal. IIC's liability is limited to the amount paid for the "
            "affected booking.",
            "Cancellations and refunds follow the Refund and Cancellation Policy.",
            "These terms are governed by the laws of India; courts at Roorkee / Haridwar have jurisdiction.",
        ), _p(CONTACT)]
    if slug == "privacy-policy":
        return [DRAFT_NOTE,
                _h("What we collect"),
                _p("Name, email, mobile number, organisation or department, address and GSTIN for invoices, identity "
                   "documents where required, booking and payment records, and technical logs (IP address, browser) "
                   "for security."),
                _h("Why we collect it"),
                _p("To register you, provide and bill services, send booking notifications, meet audit and tax "
                   "obligations, and protect the portal. Payments are processed by Razorpay under its own privacy "
                   "policy; we receive the payment status and reference, never card numbers."),
                _h("Who we share it with"),
                _ul("Within IIT Roorkee (IIC, SRIC and Accounts) as needed for the service.",
                    "Razorpay, for payments.",
                    "I-STEM (Government of India), for external bookings where applicable.",
                    "Authorities, where required by law."),
                _h("How long we keep it"),
                _p("As long as the account is active, and as required for financial records."),
                _h("Your choices"),
                _p("You can see and correct your profile, and ask for your account to be deleted, subject to the "
                   "records we must keep."),
                _h("Contact and grievances"),
                _p(f"[Name, designation], iic@iitr.ac.in. {ADDRESS}.")]
    if slug == "refund-policy":
        return [DRAFT_NOTE,
                _h("Cancellation by you"),
                _ul("More than 48 hours before the first slot: full refund.",
                    "Between 48 and 24 hours before: 50 % refund.",
                    "Less than 24 hours before: no refund.",
                    "[Confirm the rule in force for each user type before publishing.]"),
                _h("Cancellation by IIC"),
                _p("If IIC cancels (instrument down, operator unavailable or another disruption), you get a full "
                   "refund with no penalty."),
                _h("How refunds are paid"),
                _p("Refunds go to your portal wallet at once or, for cancellations by IIC, to your bank account "
                   "through IIT Roorkee SRIC within [N] working days. Online payments are not reversed to the card "
                   "or UPI used. Non-refundable items (such as return shipping charges) are shown before you "
                   "confirm a cancellation."),
                _h("Failed payments"),
                _p("If money was deducted but the booking was not confirmed, Razorpay returns it to the source "
                   "automatically, normally within 5-7 working days."),
                _p(CONTACT)]
    if slug == "about-us":
        return [DRAFT_NOTE,
                _p("The Institute Instrumentation Centre (IIC) at the Indian Institute of Technology Roorkee runs "
                   "advanced analytical instruments for researchers of IIT Roorkee and for academic, government and "
                   "industry users across India. The IIC Booking Portal is where users book instrument time, pay, "
                   "send samples and receive results."),
                _p(CONTACT)]
    return []
