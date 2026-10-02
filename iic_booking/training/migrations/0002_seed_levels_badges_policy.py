from django.db import migrations

HOUSEKEEPING_TASK_NAME = "training-housekeeping"

LEVELS = (
    {
        "code": "TRAINED",
        "name": "Trained",
        "rank": 10,
        "default_validity_months": 24,
        "is_active": True,
        "rights": {"can_self_operate": False, "can_be_deputed": False, "can_train": False},
        "description": "Attended hands-on training; operates only under operator supervision during normal hours.",
    },
    {
        "code": "CERT_L1",
        "name": "Certified Operator (L1)",
        "rank": 20,
        "default_validity_months": 24,
        "is_active": False,
        "rights": {"can_self_operate": True, "can_be_deputed": False, "can_train": False},
        "description": "Phase 2: self-operate in own slots where the equipment allows it.",
    },
    {
        "code": "CERT_L2",
        "name": "Certified Advanced / Trainer (L2)",
        "rank": 30,
        "default_validity_months": 12,
        "is_active": False,
        "rights": {"can_self_operate": True, "can_be_deputed": True, "can_train": True},
        "description": "Phase 2: can be deputed for extended hours and assist in training.",
    },
)


def seed(apps, schema_editor):
    CertificationLevel = apps.get_model("training", "CertificationLevel")
    BadgeDefinition = apps.get_model("training", "BadgeDefinition")
    TrainingPolicy = apps.get_model("training", "TrainingPolicy")
    PermissionDefinition = apps.get_model("users", "PermissionDefinition")
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    for row in LEVELS:
        CertificationLevel.objects.get_or_create(code=row["code"], defaults=row)
    trained = CertificationLevel.objects.get(code="TRAINED")
    BadgeDefinition.objects.get_or_create(
        code="trained",
        defaults={
            "name": "Trained",
            "description": "Completed hands-on training on an instrument.",
            "icon": "graduation-cap",
            "color": "#0f766e",
            "rule_type": "LEVEL",
            "level": trained,
        },
    )
    if not TrainingPolicy.objects.filter(scope="GLOBAL").exists():
        TrainingPolicy.objects.create(scope="GLOBAL", version=1, is_active=True, notes="Phase 1 defaults")
    PermissionDefinition.objects.get_or_create(
        code="training.manage",
        defaults={
            "name": "Manage training",
            "description": "Training & Certification: department policy overrides, appeals and demo escalations.",
        },
    )
    interval, _ = IntervalSchedule.objects.get_or_create(every=30, period="minutes")
    if not PeriodicTask.objects.filter(name=HOUSEKEEPING_TASK_NAME).exists():
        # Enabled: the task returns immediately while TRAINING_MODULE_ENABLED is off.
        PeriodicTask.objects.create(
            name=HOUSEKEEPING_TASK_NAME,
            task="training.housekeeping",
            interval=interval,
            enabled=True,
        )


def unseed(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=HOUSEKEEPING_TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("training", "0001_initial"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
