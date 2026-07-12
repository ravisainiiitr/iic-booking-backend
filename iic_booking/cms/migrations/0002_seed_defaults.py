# Data migration: seed default menu and home page content

from django.db import migrations


def seed_menu(apps, schema_editor):
    MenuItem = apps.get_model("cms", "MenuItem")
    # Root items (priority order)
    facilities = MenuItem.objects.create(
        label="Facilities",
        link_type="internal_anchor",
        url="#equipment",
        priority=10,
        is_active=True,
    )
    our_team = MenuItem.objects.create(
        label="Our Team",
        link_type="internal_anchor",
        url="#team-head",
        priority=20,
        is_active=True,
    )
    MenuItem.objects.create(label="Outreach", link_type="internal_anchor", url="#outreach", priority=30, is_active=True)
    MenuItem.objects.create(
        label="Important Links",
        link_type="internal_anchor",
        url="#important-links",
        priority=40,
        is_active=True,
    )
    MenuItem.objects.create(
        label="Contact Us",
        link_type="trigger",
        url="",
        priority=50,
        is_active=True,
    )
    # Submenus under Our Team
    MenuItem.objects.create(
        label="Head",
        link_type="internal_anchor",
        url="#team-head",
        parent=our_team,
        priority=1,
        is_active=True,
    )
    MenuItem.objects.create(
        label="Faculty",
        link_type="internal_anchor",
        url="#team-faculty",
        parent=our_team,
        priority=2,
        is_active=True,
    )
    MenuItem.objects.create(
        label="CAC",
        link_type="internal_anchor",
        url="#team-cac",
        parent=our_team,
        priority=3,
        is_active=True,
    )
    MenuItem.objects.create(
        label="Officers",
        link_type="internal_anchor",
        url="#team-officers",
        parent=our_team,
        priority=4,
        is_active=True,
    )
    MenuItem.objects.create(
        label="Other Staff",
        link_type="internal_anchor",
        url="#team-staff",
        parent=our_team,
        priority=5,
        is_active=True,
    )
    MenuItem.objects.create(
        label="Students",
        link_type="internal_anchor",
        url="#team-students",
        parent=our_team,
        priority=6,
        is_active=True,
    )


def seed_home_content(apps, schema_editor):
    HomePageContent = apps.get_model("cms", "HomePageContent")
    defaults = [
        ("hero_title_line1", "Advanced Scientific Equipment"),
        ("hero_title_line2", "At Your Fingertips"),
        ("hero_subtitle", "Book state-of-the-art laboratory instruments online. Seamless scheduling for researchers and institutions."),
        ("cta_book_text", "Book Equipment"),
        ("cta_book_route", "/equipments"),
        ("cta_browse_text", "Browse Catalog"),
        ("cta_browse_anchor", "#equipment"),
        ("stat1_value", "50+"),
        ("stat1_label", "Equipment Types"),
        ("stat2_value", "24/7"),
        ("stat2_label", "Online Booking"),
        ("stat3_value", "1000+"),
        ("stat3_label", "Active Users"),
    ]
    for key, value in defaults:
        HomePageContent.objects.get_or_create(key=key, defaults={"value": value})


def reverse_seed_all(apps, schema_editor):
    MenuItem = apps.get_model("cms", "MenuItem")
    HomePageContent = apps.get_model("cms", "HomePageContent")
    MenuItem.objects.all().delete()
    HomePageContent.objects.all().delete()


def run_seed(apps, schema_editor):
    seed_menu(apps, schema_editor)
    seed_home_content(apps, schema_editor)


class Migration(migrations.Migration):

    dependencies = [
        ("cms", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(run_seed, reverse_seed_all),
    ]
