"""
Update the intro of ``booking_charge_recalculated_email`` for the edit-refund window
(a lower charge is refunded at once before the cancellation deadline, otherwise it waits
for the Officer In Charge), without running the global ``sync_default_email_templates``.

Only the intro sentence is replaced, and only when the stored template still equals the
previous catalog default (normalised SHA-256 of subject + body_text + body_html). An
admin-customised template is left untouched. The previous content is printed to the
migration log so it can be restored by hand if needed.
"""

import hashlib

from django.db import migrations

CODE = "booking_charge_recalculated_email"

OLD_INTRO = (
    "Your booking details were updated and the charges have been recalculated. "
    "If a refund is due, use Refund in the booking details. If an extra amount is due, use Pay Now."
)
NEW_INTRO = (
    "Your booking details were updated and the charges have been recalculated. "
    "If the new charge is lower, the note below says whether the difference has already been refunded "
    "to the wallet or is waiting for the Officer In Charge's approval. If an extra amount is due, use Pay Now."
)

OLD_DEFAULT_SHA256 = "e54c5e781f3bfb83531372d98f72cfa450a004ac16cbaaaf806832ab05902b06"
NEW_DEFAULT_SHA256 = "e8643d5c2815079ca47462ef460520a6ea9b142f46a589518057aa11e599560e"


def _norm(value):
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def template_digest(subject, body_text, body_html):
    payload = "\x1f".join(_norm(v) for v in (subject, body_text, body_html))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _swap_intro(apps, *, expected_digest, target_digest, old, new, label):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    tpl = CommunicationTemplate.objects.filter(code=CODE, communication_type="email").first()
    if tpl is None:
        print(f"\n  [{CODE}] not found; nothing to do.")
        return
    current = template_digest(tpl.subject, tpl.body_text, tpl.body_html)
    if current == target_digest:
        print(f"\n  [{CODE}] already {label}; nothing to do.")
        return
    if current != expected_digest:
        print(f"\n  [{CODE}] customised (sha256={current}); left unchanged.")
        return
    if old not in tpl.body_text or old not in tpl.body_html:
        print(f"\n  [{CODE}] intro text not found verbatim; left unchanged.")
        return

    new_text = tpl.body_text.replace(old, new)
    new_html = tpl.body_html.replace(old, new)
    if template_digest(tpl.subject, new_text, new_html) != target_digest:
        print(f"\n  [{CODE}] replacement would not match the {label} default; left unchanged.")
        return

    print(f"\n  [{CODE}] backup of previous content (sha256={current}):")
    print("  ----- BEGIN subject -----")
    print(tpl.subject)
    print("  ----- END subject -----")
    print("  ----- BEGIN body_text -----")
    print(tpl.body_text)
    print("  ----- END body_text -----")
    print("  ----- BEGIN body_html -----")
    print(tpl.body_html)
    print("  ----- END body_html -----")

    tpl.body_text = new_text
    tpl.body_html = new_html
    tpl.save(update_fields=["body_text", "body_html", "updated_at"])
    print(f"  [{CODE}] intro updated ({label}).")


def forwards(apps, schema_editor):
    _swap_intro(
        apps,
        expected_digest=OLD_DEFAULT_SHA256,
        target_digest=NEW_DEFAULT_SHA256,
        old=OLD_INTRO,
        new=NEW_INTRO,
        label="refund-window intro",
    )


def backwards(apps, schema_editor):
    _swap_intro(
        apps,
        expected_digest=NEW_DEFAULT_SHA256,
        target_digest=OLD_DEFAULT_SHA256,
        old=NEW_INTRO,
        new=OLD_INTRO,
        label="previous intro",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0053_notice_oic_approval_workflow"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
