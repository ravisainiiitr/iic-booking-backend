"""Update CMS home hero copy to institute-wide Equipment Booking Portal branding."""

from django.db import migrations

UPDATES = {
    "hero_title_line1": "Institute Equipment Booking Portal",
    "hero_title_line2": "Precision instruments. Real-time booking.",
    "hero_subtitle": (
        "Reserve laboratory equipment across IIT Roorkee departments, centres, "
        "and laboratories — live availability, transparent charges, and results on your dashboard."
    ),
}

# Only overwrite when current value still looks like legacy / exclusive branding.
LEGACY_HINTS = (
    "Institute Instrumentation Centre",
    "INSTITUTE INSTRUMENTATION",
    "Advanced Scientific Equipment",
    "At Your Fingertips",
)


def forwards(apps, schema_editor):
    HomePageContent = apps.get_model("cms", "HomePageContent")
    for key, new_value in UPDATES.items():
        row = HomePageContent.objects.filter(key=key).first()
        if row is None:
            HomePageContent.objects.create(key=key, value=new_value)
            continue
        current = (row.value or "").strip()
        if not current or any(h in current for h in LEGACY_HINTS):
            row.value = new_value
            row.save(update_fields=["value"])


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("cms", "0007_alter_menuitem_page"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
