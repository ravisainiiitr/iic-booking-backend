"""
Same guarded intro update as 0054, for templates rendered with the production portal URL.

The catalog embeds ``FRONTEND_URL`` in the email body, so the stored default's digest depends
on the environment. 0054's digests were for ``http://localhost:8080``; these are for
``https://equip.iitr.ac.in``. Anything else (including admin edits) is left unchanged.
"""

import importlib

from django.db import migrations

m0054 = importlib.import_module(
    "iic_booking.communication.migrations.0054_charge_recalculated_refund_window_intro"
)

PORTAL_URL = "https://equip.iitr.ac.in"
OLD_DEFAULT_SHA256 = "0640dc5054bf1a4c349c6cce06d8e683bca097ed22eeccba7ade2b582ae2e120"
NEW_DEFAULT_SHA256 = "c692a2453ab229f331c9a6a69f5a988416d761069ed4199cbf267cc291abc56f"


def forwards(apps, schema_editor):
    m0054._swap_intro(
        apps,
        expected_digest=OLD_DEFAULT_SHA256,
        target_digest=NEW_DEFAULT_SHA256,
        old=m0054.OLD_INTRO,
        new=m0054.NEW_INTRO,
        label="refund-window intro",
    )


def backwards(apps, schema_editor):
    m0054._swap_intro(
        apps,
        expected_digest=NEW_DEFAULT_SHA256,
        target_digest=OLD_DEFAULT_SHA256,
        old=m0054.NEW_INTRO,
        new=m0054.OLD_INTRO,
        label="previous intro",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0054_charge_recalculated_refund_window_intro"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
