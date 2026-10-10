from django.db import migrations

LEVELS = (
    {
        "code": "ORIENTATION",
        "name": "Orientation",
        "rank": 5,
        "default_validity_months": 12,
        "rights": {"can_self_operate": False, "can_be_deputed": False, "can_train": False},
        "description": "Safety and SOP orientation; may observe and assist, does not operate.",
    },
)

ACTIVATE = {
    "TRAINED": "Supervised user: completed hands-on training; operates only under operator supervision.",
    "CERT_L1": "Certified operator: passed the practical competency assessment; operates independently and can be "
    "allocated operator duty.",
    "CERT_L2": "Advanced operator / trainer: certified operator who may train and assess others and take extended duty.",
}

BADGES = (
    ("certified-operator", "Certified Operator", "CERT_L1", "#1d4ed8", "badge-check"),
    ("trainer", "Trainer", "CERT_L2", "#7c3aed", "award"),
)


def seed(apps, schema_editor):
    CertificationLevel = apps.get_model("training", "CertificationLevel")
    BadgeDefinition = apps.get_model("training", "BadgeDefinition")
    OperatorPolicy = apps.get_model("training", "OperatorPolicy")
    CompetencyChecklist = apps.get_model("training", "CompetencyChecklist")
    CertificationAward = apps.get_model("training", "CertificationAward")

    for row in LEVELS:
        CertificationLevel.objects.get_or_create(code=row["code"], defaults={**row, "is_active": True})
    for code, description in ACTIVATE.items():
        CertificationLevel.objects.filter(code=code).update(is_active=True, description=description)
    for code, name, level_code, color, icon in BADGES:
        level = CertificationLevel.objects.filter(code=level_code).first()
        if level is not None:
            BadgeDefinition.objects.get_or_create(
                code=code,
                defaults={"name": name, "level": level, "color": color, "icon": icon, "rule_type": "LEVEL",
                          "description": f"Holds {level.name}."},
            )
    if not OperatorPolicy.objects.filter(scope="GLOBAL").exists():
        OperatorPolicy.objects.create(scope="GLOBAL", version=1, is_active=True, notes="Default operator duty and fairness policy")
    if not CompetencyChecklist.objects.filter(equipment__isnull=True).exists():
        from iic_booking.training.models import DEFAULT_CHECKLIST_ITEMS

        CompetencyChecklist.objects.create(items=DEFAULT_CHECKLIST_ITEMS)
    for award in CertificationAward.objects.filter(certificate_no__isnull=True).only("id", "awarded_at"):
        CertificationAward.objects.filter(pk=award.pk, certificate_no__isnull=True).update(
            certificate_no=f"IIC-TRN-{award.awarded_at.year}-{award.pk:05d}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("training", "0006_operator_duty_assessment"),
    ]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
