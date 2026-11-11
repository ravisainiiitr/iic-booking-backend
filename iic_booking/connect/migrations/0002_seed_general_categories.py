"""Seed General IITR Discussions, its categories, the (inactive-by-setting) recognition badges and the settings row.

Create-only and idempotent: existing rows are never changed, and reversing removes nothing (data is kept).
Group communities are NOT created here; they come from real Equipment Group records via the sync command.
"""

from django.db import migrations

GENERAL_SLUG = "general"

CATEGORIES = [
    ("general-discussion", "General Discussion", "Anything that does not fit another category.", False),
    ("announcements", "Announcements", "Notices from the portal team and facility staff.", True),
    ("portal-support", "Portal Support", "Help with using the equipment booking portal.", False),
    ("feature-requests", "Feature Requests", "Ideas for new portal features.", False),
    ("suggestions-feedback", "Suggestions and Feedback", "Suggestions to improve facilities and services.", False),
    ("research-collaboration", "Research Collaboration", "Find collaborators, methods and shared interests.", False),
    ("training-experiences", "Training and User Experiences", "Training sessions and experiences with instruments.", False),
    ("general-instrumentation", "General Instrumentation Discussions",
     "Instrumentation topics across facilities, including instruments that are not in an equipment group.", False),
]

BADGES = [
    ("helpful-contributor", "Helpful Contributor", "Answers marked helpful by many different members.",
     {"distinct_helpful_users": 10}, 10),
    ("technical-expert", "Technical Expert", "Several accepted answers to technical questions.",
     {"accepted_answers": 5}, 20),
    ("community-mentor", "Community Mentor", "Accepted answers across several communities.",
     {"accepted_answers": 10, "communities": 3}, 30),
    ("equipment-specialist", "Equipment Specialist", "Accepted answers on one instrument's threads.",
     {"accepted_answers_on_one_equipment": 5}, 40),
    ("top-solution-provider", "Top Solution Provider", "Accepted answers plus approved knowledge articles.",
     {"accepted_answers": 20, "approved_articles": 2}, 50),
]


def seed(apps, schema_editor):
    Community = apps.get_model("connect", "Community")
    DiscussionCategory = apps.get_model("connect", "DiscussionCategory")
    RecognitionBadge = apps.get_model("connect", "RecognitionBadge")
    ConnectSettings = apps.get_model("connect", "ConnectSettings")

    general = Community.objects.filter(kind="general").first()
    if general is None:
        general = Community.objects.create(
            kind="general",
            slug=GENERAL_SLUG,
            name="General IITR Discussions",
            description="Open to IIT Roorkee students, faculty and staff: questions, ideas and experiences "
            "across the institute's facilities.",
        )
    for position, (slug, name, description, staff_only) in enumerate(CATEGORIES, start=1):
        DiscussionCategory.objects.get_or_create(
            community=general,
            slug=slug,
            defaults={
                "name": name,
                "description": description,
                "position": position * 10,
                "staff_only_posting": staff_only,
            },
        )
    for code, name, description, criteria, position in BADGES:
        RecognitionBadge.objects.get_or_create(
            code=code,
            defaults={"name": name, "description": description, "criteria": criteria, "position": position},
        )
    ConnectSettings.objects.get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [("connect", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
